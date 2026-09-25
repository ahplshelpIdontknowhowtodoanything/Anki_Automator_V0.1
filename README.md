# Lecture Slides → Anki Deck

Converts a PDF of lecture slides into an Anki deck (`.apkg`): every slide
becomes one flashcard, with an AI-generated stimulus on the front and the
full slide image on the back.

## Files

- `pdf_to_anki_core.py` — the conversion pipeline (single PDF).
- `batch_convert.py` — CLI to convert many PDFs at once, sequentially or in parallel.
- `app.py` — Streamlit web app: upload PDFs in a browser, download decks.
- `requirements.txt` — dependencies for both the CLI and the web app.

## Run locally (CLI)

```bash
pip install -r requirements.txt
export DEEPSEEK_API_KEY=sk-...
python pdf_to_anki_core.py lecture.pdf -o lecture.apkg
# or a whole folder:
python batch_convert.py ./my_pdfs/ -o decks/ --workers 4
```

## Run locally (web UI)

```bash
pip install -r requirements.txt
streamlit run app.py
```

Opens at `http://localhost:8501`. Each visitor enters their own DeepSeek API
key in the sidebar — no key is stored in the code or the repo.

## Deploy it as a public web app

Push this repo to GitHub, then pick one:

### Option A: Streamlit Community Cloud (simplest)
1. Push this folder to a GitHub repo.
2. Go to [share.streamlit.io](https://share.streamlit.io) → "New app" → pick
   the repo, branch, and `app.py` as the entry point.
3. Deploy. You get a public `*.streamlit.app` URL automatically.
4. Free tier sleeps after inactivity and has modest CPU/RAM — fine for this
   workload, but very large PDFs may be slow.

### Option B: Hugging Face Spaces
1. Create a new Space, SDK = Streamlit, and connect it to this GitHub repo
   (or push directly to the Space's own git remote).
2. HF Spaces free tier tends to have more headroom than Streamlit Cloud for
   CPU-bound work like PDF rendering.

Neither option needs you to add your own API key anywhere — the app asks each
visitor for theirs, so you never pay for other people's conversions.

## Things worth knowing before making this public

- **Cost/abuse**: because each visitor supplies their own key, the app itself
  costs you nothing to run, but nothing stops someone from pasting in a stolen
  or shared key either — that's on them, not something this app can prevent.
- **Speed**: one (sometimes two) LLM API calls happen per slide, so a
  50-slide deck means 50+ calls — expect real wait time on big PDFs, and
  free-tier hosts may time out very long requests.
- **Content**: slides get rendered to images and sent to DeepSeek's API for
  processing. If the lecture material isn't yours to redistribute or send to
  a third-party service, that's worth checking before opening this up to
  other students.
