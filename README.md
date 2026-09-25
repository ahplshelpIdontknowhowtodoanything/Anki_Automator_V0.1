# Lecture Slides → Anki Deck

Vibe-coded utility that converts a PDF of lecture slides into an Anki deck (`.apkg`): every slide
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

Neither option needs you to add your own API key anywhere — the app asks each
visitor for theirs, so you never pay for other people's conversions.
