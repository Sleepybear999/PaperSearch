# NITJ Paper Search

Search NIT Jalandhar mid-sem / end-sem papers. Flask + one HTML page. Optional Gemini "Ask AI" turns plain-language requests ("dsa end sem cse") into filters.

## Files
- `app.py` – backend (search API + Gemini call)
- `static/index.html` – the whole interface
- `erp_metadata.csv` – your paper index (must sit next to `app.py`)
- `render.yaml`, `requirements.txt`, `.gitignore`

## Run locally
```
pip install -r requirements.txt
GEMINI_API_KEY=your_key python app.py     # open http://localhost:5000
```
Without a key the site still works; the Ask AI button is hidden.

## Deploy on Render
1. Push this folder to GitHub (keep `erp_metadata.csv` in the repo root).
2. Render → New → Blueprint (uses `render.yaml`), or New → Web Service with:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --workers 1 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT`
3. Environment variables: `GEMINI_API_KEY` (secret), optional `GEMINI_MODEL` (default `gemini-2.5-flash-lite`), `PYTHON_VERSION=3.11.9`.

To update papers, replace `erp_metadata.csv` and push; Render redeploys.
