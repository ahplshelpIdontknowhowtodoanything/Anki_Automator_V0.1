"""
app.py -- Streamlit front end for pdf_to_anki_core.py

Run locally:
    pip install -r requirements.txt
    streamlit run app.py

Deployed on Streamlit Community Cloud or Hugging Face Spaces, this gives
visitors a page where they can upload lecture-slide PDFs and download an
.apkg Anki deck, using THEIR OWN DeepSeek API key (never yours).
"""
import os
import tempfile

import streamlit as st

from pdf_to_anki_core import convert_pdf_to_anki_deck, DEFAULT_PAGES_PER_BATCH

st.set_page_config(page_title="Lecture Slides → Anki Deck", page_icon="📚")
st.title("📚 Lecture Slides → Anki Deck")
st.write(
    "Upload one or more lecture-slide PDFs. Each slide becomes a flashcard: "
    "an AI-generated question/stimulus on the front, the full slide image on the back."
)

with st.sidebar:
    st.header("DeepSeek API key")
    st.caption(
        "Required. Get a key at platform.deepseek.com. Your key is used only "
        "for your own conversions in this session and is never stored or logged."
    )
    api_key = st.text_input("API key", type="password")

    st.header("Advanced")
    pages_per_batch = st.slider(
        "Slides per API call", min_value=1, max_value=8,
        value=DEFAULT_PAGES_PER_BATCH,
        help="Higher = fewer API calls, but a bigger vision-fallback payload "
             "per call and more cards lost if one response is malformed.",
    )

uploaded_files = st.file_uploader(
    "Lecture PDFs", type=["pdf"], accept_multiple_files=True
)

if st.button("Convert to Anki decks", type="primary", disabled=not uploaded_files):
    if not api_key:
        st.error("Please enter your DeepSeek API key in the sidebar first.")
        st.stop()

    for uploaded in uploaded_files:
        with st.status(f"Converting {uploaded.name}...", expanded=False) as status:
            with tempfile.TemporaryDirectory() as tmp_dir:
                pdf_path = os.path.join(tmp_dir, uploaded.name)
                with open(pdf_path, "wb") as f:
                    f.write(uploaded.getbuffer())

                base_name = os.path.splitext(uploaded.name)[0]
                output_path = os.path.join(tmp_dir, f"{base_name}.apkg")

                try:
                    convert_pdf_to_anki_deck(
                        pdf_path, output_path, api_key=api_key,
                        pages_per_batch=pages_per_batch,
                    )
                    with open(output_path, "rb") as f:
                        apkg_bytes = f.read()
                    status.update(label=f"{uploaded.name} done", state="complete")
                    st.download_button(
                        label=f"⬇️ Download {base_name}.apkg",
                        data=apkg_bytes,
                        file_name=f"{base_name}.apkg",
                        mime="application/octet-stream",
                        key=f"dl-{uploaded.name}",
                    )
                except Exception as exc:  # noqa: BLE001
                    status.update(label=f"{uploaded.name} failed", state="error")
                    st.error(f"{uploaded.name}: {exc}")

st.divider()
st.caption(
    "Each slide triggers one (or two) AI API calls, so large decks take a "
    "while and use your API credits — roughly proportional to slide count."
)
