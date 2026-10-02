"""NITJ question paper search — Flask backend.

Run locally:  GEMINI_API_KEY=xxx python app.py
Render:       see render.yaml
"""
import csv
import difflib
import json
import os
import re
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import date

import requests
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "erp_metadata.csv")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [
    m.strip()
    for m in (os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite") + ",gemini-2.5-flash").split(",")
    if m.strip()
]
PAGE_SIZE = 30

app = Flask(__name__, static_folder="static", static_url_path="/static")

# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
STOPWORDS = {"and", "of", "the", "to", "in", "for", "a", "an", "paper", "papers",
             "pyq", "pyqs", "question", "questions", "exam", "previous", "year"}

# Common student shorthand -> words that appear in subject names.
ALIASES = {
    "dsa": ["data", "structures"],
    "ds": ["data", "structures"],
    "dbms": ["database"],
    "os": ["operating", "systems"],
    "cn": ["computer", "networks"],
    "coa": ["computer", "organization"],
    "toc": ["theory", "computation"],
    "oop": ["object", "oriented"],
    "oops": ["object", "oriented"],
    "ml": ["machine", "learning"],
    "dl": ["deep", "learning"],
    "ai": ["artificial", "intelligence"],
    "dip": ["digital", "image"],
    "dsp": ["digital", "signal"],
    "vlsi": ["vlsi"],
    "em": ["electromagnetic"],
    "emt": ["electromagnetic"],
    "ep": ["engineering", "physics"],
    "ec": ["engineering", "chemistry"],
    "bee": ["basic", "electrical"],
    "bme": ["mechanical"],
    "edg": ["engineering", "graphics"],
    "eg": ["engineering", "graphics"],
    "maths": ["mathematics"],
    "math": ["mathematics"],
}


def tokenize(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def session_key(s):
    """'July-December 2025' -> sortable int (newest = biggest)."""
    m = re.search(r"(\d{4})", s or "")
    year = int(m.group(1)) if m else 0
    return year * 2 + (1 if (s or "").lower().startswith("july") else 0)


def clean(v):
    return re.sub(r"\s+", " ", (v or "").strip())


ROWS = []
_seen_urls = set()
with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        url = clean(r.get("url"))
        if not url or url in _seen_urls:
            continue
        _seen_urls.add(url)
        code = clean(r.get("subject_code"))
        name = clean(r.get("subject_name")) or code
        dept = clean(r.get("Department/Group")) or "Not specified"
        row = {
            "session": clean(r.get("Session")),
            "course": clean(r.get("Course")),
            "batch": clean(r.get("Batch")),
            "dept": dept,
            "code": code,
            "name": name,
            "exam": clean(r.get("exam")),
            "url": url,
        }
        row["_code_l"] = re.sub(r"[^a-z0-9]", "", code.lower())
        row["_name_words"] = set(tokenize(name))
        row["_blob"] = " ".join([code, row["_code_l"], name, dept, row["course"], row["session"], row["batch"]]).lower()
        row["_words"] = set(tokenize(row["_blob"]))
        row["_skey"] = session_key(row["session"])
        ROWS.append(row)


def _facet(field, key=None, reverse=False):
    counts = defaultdict(int)
    for r in ROWS:
        counts[r[field]] += 1
    items = sorted(counts, key=key, reverse=reverse)
    return [{"value": v, "count": counts[v]} for v in items]


META = {
    "total": len(ROWS),
    "sessions": _facet("session", key=session_key, reverse=True),
    "courses": _facet("course", key=lambda v: -sum(1 for r in ROWS if r["course"] == v)),
    "batches": _facet("batch", reverse=True),
    "departments": _facet("dept"),
    "exams": [{"value": "Mid Sem"}, {"value": "End Sem"}],
    "ai": bool(GEMINI_API_KEY),
}
VALID = {
    "session": {x["value"].lower(): x["value"] for x in META["sessions"]},
    "course": {x["value"].lower(): x["value"] for x in META["courses"]},
    "batch": {x["value"].lower(): x["value"] for x in META["batches"]},
    "dept": {x["value"].lower(): x["value"] for x in META["departments"]},
    "exam": {"mid sem": "Mid Sem", "end sem": "End Sem"},
}
UNIQUE_NAMES = sorted({r["name"] for r in ROWS})
UNIQUE_NAMES_L = {n.lower(): n for n in UNIQUE_NAMES}

# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def query_tokens(phrase):
    out = []
    for t in tokenize(phrase):
        if t in STOPWORDS:
            continue
        for w in ALIASES.get(t, [t]):
            # crude plural stripping so "systems" also finds "System"
            if len(w) > 5 and w.endswith("s") and not w.endswith("ss"):
                w = w[:-1]
            out.append(w)
    return out


def score_row(row, tokens):
    score = 0
    for t in tokens:
        if len(t) >= 4:
            if t not in row["_blob"]:
                return None
        elif not any(w.startswith(t) for w in row["_words"]):
            return None
        if t == row["_code_l"]:
            score += 8
        elif row["_code_l"].startswith(t):
            score += 5
        elif t in row["_name_words"]:
            score += 4
        elif any(w.startswith(t) for w in row["_name_words"]):
            score += 3
        else:
            score += 1
    return score


def apply_filters(row, f):
    for key in ("session", "course", "batch", "dept", "exam"):
        want = f.get(key)
        if want and row[key if key != "exam" else "exam"] != want:
            return False
    return True


def run_search(phrases, filters, page):
    token_sets = [query_tokens(p) for p in phrases if p and p.strip()]
    token_sets = [t for t in token_sets if t]

    groups = OrderedDict()
    for row in ROWS:
        if not apply_filters(row, filters):
            continue
        if token_sets:
            best = None
            for tokens in token_sets:
                s = score_row(row, tokens)
                if s is not None and (best is None or s > best):
                    best = s
            if best is None:
                continue
        else:
            best = 0
        key = (row["session"], row["course"], row["batch"], row["dept"], row["code"], row["name"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "session": row["session"], "course": row["course"], "batch": row["batch"],
                "dept": row["dept"], "code": row["code"], "name": row["name"],
                "score": best, "_skey": row["_skey"], "papers": [],
            }
        g["score"] = max(g["score"], best)
        g["papers"].append({"exam": row["exam"], "url": row["url"]})

    result = sorted(groups.values(), key=lambda g: (-g["score"], -g["_skey"], g["name"].lower(), g["course"]))
    total_papers = sum(len(g["papers"]) for g in result)
    start = page * PAGE_SIZE
    chunk = result[start:start + PAGE_SIZE]
    for g in chunk:
        g["papers"].sort(key=lambda p: (0 if p["exam"] == "Mid Sem" else 1))
        g.pop("_skey", None)

    suggestions = []
    if not result and token_sets and page == 0:
        probe = " ".join(phrases).lower().strip()
        for m in difflib.get_close_matches(probe, list(UNIQUE_NAMES_L), n=4, cutoff=0.55):
            suggestions.append(UNIQUE_NAMES_L[m])

    return {
        "groups": chunk,
        "total_groups": len(result),
        "total_papers": total_papers,
        "page": page,
        "has_more": start + PAGE_SIZE < len(result),
        "suggestions": suggestions,
    }


def read_filters(src):
    f = {}
    for key in ("session", "course", "batch", "dept", "exam"):
        v = (src.get(key) or "").strip().lower()
        if v and v in VALID[key]:
            f[key] = VALID[key][v]
    return f


# --------------------------------------------------------------------------
# Gemini (natural-language -> filters)
# --------------------------------------------------------------------------
_cache = OrderedDict()
_cache_lock = threading.Lock()
_hits = defaultdict(list)
_hits_lock = threading.Lock()


def rate_limited(ip, limit=12, window=60):
    now = time.time()
    with _hits_lock:
        _hits[ip] = [t for t in _hits[ip] if now - t < window]
        if len(_hits[ip]) >= limit:
            return True
        _hits[ip].append(now)
    return False


def build_prompt():
    return (
        "You turn a student's request into a search over a database of NIT Jalandhar "
        "previous-year exam papers. Today is %s.\n\n"
        "Valid filter values (use EXACTLY these strings or null):\n"
        "session: %s\ncourse: %s\nbatch: %s\nexam: Mid Sem | End Sem\ndepartment: %s\n\n"
        "Rules:\n"
        "- 'queries' = up to 3 short subject-name search phrases (lowercase). Expand abbreviations "
        "(dsa -> 'data structures', dbms -> 'database management', os -> 'operating systems'). "
        "If the user gave a subject code (like CSPC0101), put it as a phrase. "
        "If no subject is mentioned, use an empty list.\n"
        "- Only set a filter when the user clearly implied it. 'sem 3', 'mid', 'endsem' -> exam only if mid/end is said.\n"
        "- A year like 2025 alone is ambiguous: prefer setting session only if a term is given "
        "(e.g. 'jan-june 2025', 'winter 2025' = July-December 2025, 'spring 2026' = January-June 2026); "
        "otherwise leave null. 'batch' is the admission year of the students.\n"
        "- Department aliases: cse = COMPUTER SCIENCE AND ENGINEERING, it = INFORMATION TECHNOLOGY, "
        "ece = ELECTRONICS AND COMMUNICATION ENGINEERING, ee = ELECTRICAL ENGINEERING, "
        "me = MECHANICAL ENGINEERING, ce = CIVIL ENGINEERING, ice = INSTRUMENTATION AND CONTROL ENGINEERING, "
        "ipe = INDUSTRIAL AND PRODUCTION ENGINEERING, bt = BIO TECHNOLOGY, tt = TEXTILE TECHNOLOGY, "
        "che = CHEMICAL ENGINEERING, dse = DATA SCIENCE AND ENGINEERING, first year = B.Tech First Year.\n"
        "- 'note' = one short sentence (max 14 words) saying what you searched for.\n\n"
        "Reply with ONLY JSON: "
        '{"queries": [string], "session": string|null, "course": string|null, "batch": string|null, '
        '"department": string|null, "exam": string|null, "note": string}'
    ) % (
        date.today().strftime("%d %B %Y"),
        " | ".join(x["value"] for x in META["sessions"]),
        " | ".join(x["value"] for x in META["courses"]),
        " | ".join(x["value"] for x in META["batches"]),
        " | ".join(x["value"] for x in META["departments"]),
    )


def call_gemini(user_text):
    last_err = None
    for model in GEMINI_MODELS:
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json={
                    "systemInstruction": {"parts": [{"text": build_prompt()}]},
                    "contents": [{"role": "user", "parts": [{"text": user_text[:300]}]}],
                    "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
                },
                timeout=15,
            )
            if resp.status_code != 200:
                last_err = f"{model}: HTTP {resp.status_code}"
                continue
            parts = resp.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
            text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
            return json.loads(text)
        except Exception as e:  # network, parse, schema...
            last_err = f"{model}: {type(e).__name__}"
    raise RuntimeError(last_err or "Gemini failed")


def interpret(user_text):
    key = user_text.lower().strip()
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    raw = call_gemini(user_text)
    queries = [clean(str(q)).lower() for q in (raw.get("queries") or []) if clean(str(q))][:3]
    filters = {}
    for src_key, key_name in (("session", "session"), ("course", "course"), ("batch", "batch"),
                              ("department", "dept"), ("exam", "exam")):
        v = raw.get(src_key)
        if v is not None:
            v = str(v).strip().lower()
            if v in VALID[key_name]:
                filters[key_name] = VALID[key_name][v]
    out = {"queries": queries, "filters": filters, "note": clean(str(raw.get("note") or ""))[:140]}
    with _cache_lock:
        _cache[key] = out
        while len(_cache) > 500:
            _cache.popitem(last=False)
    return out


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/meta")
def api_meta():
    return jsonify(META)


@app.get("/api/search")
def api_search():
    phrases = [q for q in request.args.getlist("q") if q.strip()][:3]
    try:
        page = max(0, int(request.args.get("page", 0)))
    except ValueError:
        page = 0
    return jsonify(run_search(phrases, read_filters(request.args), page))


@app.get("/api/ai")
def api_ai():
    if not GEMINI_API_KEY:
        return jsonify({"error": "AI search is not configured on this server."}), 503
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Type what you are looking for first."}), 400
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()
    if rate_limited(ip):
        return jsonify({"error": "Too many AI searches. Wait a minute, or use the normal search."}), 429
    try:
        return jsonify(interpret(q))
    except Exception:
        return jsonify({"error": "AI search is unavailable right now. Showing normal search instead."}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
