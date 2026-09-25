#!/usr/bin/env python3
"""
pdf_slides_to_anki.py

Converts a PDF of lecture slides into an Anki deck (.apkg), where every
single slide becomes one flashcard:

    FRONT : Auto-extracted Heading (+ Subheadings) parsed from the slide's
            text layout (no PDF bookmarks/TOC required).
    BACK  : A high-resolution (300 DPI) render of the full slide image.

Dependencies:
    pip install pymupdf genanki

Usage:
    python pdf_slides_to_anki.py lecture.pdf -o lecture_deck.apkg
"""
import re
import base64
from io import BytesIO
from PIL import Image
from openai import OpenAI
import argparse
import os
import shutil
import sys
import tempfile
from collections import Counter
from html import escape

import pymupdf  # modern PyMuPDF import name
import genanki


# --------------------------------------------------------------------------
# Fixed, hardcoded IDs for the Anki model/deck.
# These must stay CONSTANT across runs -- if you change them, Anki will treat
# re-imports as a brand new model/deck instead of updating the existing one.
# (Generated once via random.randrange(1 << 30, 1 << 31) and then frozen.)
# --------------------------------------------------------------------------
MODEL_ID = 1745040194
DECK_ID = 2078460429

# 300 DPI render. PDF points are defined at 72 DPI, so zoom = target_dpi / 72.
# 300 / 72 = 4.1666... (~4.16, matches the "approximate zoom factor" spec).
RENDER_DPI = 100
ZOOM_FACTOR = RENDER_DPI / 72.0

# Bit flags used inside PyMuPDF's span["flags"] bitfield.
# (See PyMuPDF docs: TextPage.extractDICT / span flags)
FLAG_SUPERSCRIPT = 1 << 0
FLAG_ITALIC = 1 << 1
FLAG_SERIFED = 1 << 2
FLAG_MONOSPACED = 1 << 3
FLAG_BOLD = 1 << 4

# --------------------------------------------------------------------------
# How many slides get bundled into a single API call.
#
# Trade-off: bigger batches mean fewer requests, but the vision fallback
# embeds a full slide image per slide, so large batches risk hitting the
# model's context/size limits; and a single malformed response loses the
# whole batch's stimuli, not just one slide's. 4 keeps request count to a
# quarter of one-call-per-slide while keeping both of those risks small.
# Override with the PAGES_PER_BATCH env var, or pages_per_batch= directly.
# --------------------------------------------------------------------------
DEFAULT_PAGES_PER_BATCH = 4


# --------------------------------------------------------------------------
# STEP 1: Per-slide text-layout analysis (no metadata/bookmarks used)
# --------------------------------------------------------------------------
def extract_slide_text_blocks(page):
    """
    Pulls every text block on a single slide, along with layout properties
    (max font size in the block, whether any span is bold, and vertical
    position on the page) needed to distinguish heading / subheading / body.

    Uses page.get_text("dict") instead of the plain "blocks" mode because
    dict mode exposes per-span font size and style flags, which is exactly
    what we need to detect headings dynamically without any TOC metadata.
    """
    text_dict = page.get_text("dict")
    blocks_info = []

    for block in text_dict.get("blocks", []):
        # type == 0 -> text block, type == 1 -> image block. Skip images.
        if block.get("type") != 0:
            continue

        block_text_parts = []
        max_font_size = 0.0
        block_is_bold = False
        min_y = float("inf")

        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_text = span.get("text", "").strip()
                if not span_text:
                    continue

                block_text_parts.append(span_text)

                size = span.get("size", 0.0)
                if size > max_font_size:
                    max_font_size = size

                flags = span.get("flags", 0)
                font_name = span.get("font", "") or ""
                if (flags & FLAG_BOLD) or ("bold" in font_name.lower()):
                    block_is_bold = True

                y0 = span.get("bbox", (0, 0, 0, 0))[1]
                if y0 < min_y:
                    min_y = y0

        block_text = " ".join(block_text_parts).strip()
        if not block_text:
            continue  # skip empty/whitespace-only blocks

        blocks_info.append(
            {
                "text": block_text,
                "font_size": round(max_font_size, 1),
                # Rounded bucket used only for grouping same-ish sizes
                # together when computing the "body text" size mode.
                "size_bucket": round(max_font_size),
                "bold": block_is_bold,
                "y_pos": min_y if min_y != float("inf") else 0.0,
            }
        )

    return blocks_info


def classify_heading_subheadings(blocks_info):
    """
    Dynamically decides, for ONE slide, which text block is the Heading and
    which (if any) are Subheadings, purely from layout (font size / boldness
    / position) -- no PDF outline/TOC metadata is used.

    Heuristic:
      1. The block with the single largest font size on the slide is the
         Heading (ties broken by whichever sits highest on the page, since
         slide titles are almost always at the top).
      2. Among the remaining blocks, the most common font size is assumed to
         be the "body text" (bullet points, paragraphs, footnotes, etc.).
      3. Any remaining block that is either:
           - noticeably larger than the body-text size, OR
           - bold and at least as large as the body-text size
         is treated as a Subheading (e.g. section labels, sub-titles).
      4. Subheadings are returned in the slide's natural reading order
         (top-to-bottom), not sorted by size.

    Returns:
        (heading_text: str or None, subheadings: list[str])
    """
    if not blocks_info:
        return None, []

    # 1. Find the Heading: largest font size, top-most as tiebreaker.
    sorted_by_prominence = sorted(
        blocks_info, key=lambda b: (-b["font_size"], b["y_pos"])
    )
    heading_block = sorted_by_prominence[0]
    remaining = [b for b in blocks_info if b is not heading_block]

    if not remaining:
        return heading_block["text"], []

    # 2. Estimate the "body text" font size as the statistical mode among
    #    the leftover blocks (bucketed to whole points for stability).
    bucket_counts = Counter(b["size_bucket"] for b in remaining)
    body_size_bucket = bucket_counts.most_common(1)[0][0]

    # 3. Walk remaining blocks in natural reading order, classify each.
    remaining_in_reading_order = sorted(remaining, key=lambda b: b["y_pos"])

    subheadings = []
    seen_text = {heading_block["text"]}
    for b in remaining_in_reading_order:
        distinctly_larger = b["size_bucket"] > body_size_bucket
        bold_and_body_or_larger = b["bold"] and b["size_bucket"] >= body_size_bucket

        if (distinctly_larger or bold_and_body_or_larger) and b["text"] not in seen_text:
            subheadings.append(b["text"])
            seen_text.add(b["text"])

    return heading_block["text"], subheadings


# --------------------------------------------------------------------------
# STEP 2: High-resolution slide rendering
# --------------------------------------------------------------------------
def render_slide_image(page, page_index, output_dir):
    """
    Renders a single PDF page (slide) to a PNG at ~300 DPI so that small
    text, charts, and diagrams stay sharp when viewed as an Anki card.

    Returns the full local filesystem path to the saved PNG.
    """
    matrix = pymupdf.Matrix(ZOOM_FACTOR, ZOOM_FACTOR)
    #matrix = pymupdf.Matrix(0.25, 0.25)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)

    image_filename = f"slide_{page_index + 1:03d}.png"
    image_path = os.path.join(output_dir, image_filename)
    pixmap.save(image_path)

    return image_path, image_filename


# --------------------------------------------------------------------------
# STEP 3: HTML formatting for the card front
# --------------------------------------------------------------------------
def build_front_html(heading, subheadings, page_index):
    """
    Builds the front-of-card HTML from the extracted heading/subheadings.
    Falls back to "Slide X" if no text could be extracted at all.
    """
    if not heading:
        heading = f"Slide {page_index + 1}"

    html_parts = [f"<h1>{escape(heading)}</h1>"]

    if subheadings:
        # Multiple subheadings -> bullet list; a single one -> a plain <h2>.
        if len(subheadings) == 1:
            html_parts.append(f"<h2>{escape(subheadings[0])}</h2>")
        else:
            items = "".join(f"<li>{escape(s)}</li>" for s in subheadings)
            html_parts.append(f"<ul>{items}</ul>")

    return "".join(html_parts)


def build_back_html(image_filename):
    """Builds the back-of-card HTML embedding the slide image by filename."""
    return f'<img src="{escape(image_filename)}">'


# --------------------------------------------------------------------------
# STEP 4: Anki model/deck construction
# --------------------------------------------------------------------------
def create_anki_model():
    """Defines the custom Note Type with two fields: SlideHeadings, SlideImage."""
    temp = genanki.Model(
        MODEL_ID,
        "Lecture Slide Model",
        fields=[
            {"name": "SlideHeadings"},
            {"name": "SlideImage"},
        ],
        templates=[
            {
                "name": "Slide Card",
                "qfmt": "{{SlideHeadings}}",
                "afmt": '{{FrontSide}}<hr id="answer">{{SlideImage}}',
            }
        ],
        css="""
            .card {
                font-family: Arial, Helvetica, sans-serif;
                font-size: 20px;
                text-align: center;
                color: #1a1a1a;
                background-color: #ffffff;
            }
            h1 {
                font-size: 28px;
                color: #ffffff;
                margin-bottom: 6px;
            }
            h2 {
                font-size: 20px;
                font-weight: normal;
                color: #ffffff;
                margin-top: 2px;
            }
            ul {
                display: inline-block;
                text-align: left;
                font-size: 18px;
                color: #ffffff;
            }
            img {
                max-width: 100%;
                height: auto;
                margin-top: 10px;
                border: 1px solid #ddd;
            }
        """,
    )
    print(temp.fields)
    return temp


# --------------------------------------------------------------------------
# STEP 4.5: Batch prompt building / response parsing
#
# Both passes below ask the model to answer for several slides in one
# response, using a "SLIDE <n>: #stimulus#" line per slide so replies can be
# matched back to the right slide even if the model reorders them.
# --------------------------------------------------------------------------
SLIDE_LINE_RE = re.compile(r"SLIDE\s+(\d+)\s*:\s*#(.*?)#", re.IGNORECASE | re.DOTALL)

BATCH_TEXT_SYSTEM_PROMPT = (
    "Generate an Anki flashcard stimulus for EACH of the numbered lecture "
    "slides below, for medical students at the Faculty of Medicine, Siriraj "
    "Hospital.\n\n"
    "Respond with exactly one line per slide, in this exact format and "
    "nothing else:\n"
    "SLIDE <n>: #<stimulus, max 15 words, plain text>#\n\n"
    "If a slide has no usable text, or no good stimulus can be made from it, "
    "respond for that slide with:\n"
    "SLIDE <n>: #NONE#\n\n"
    "Rules for each stimulus: max 15 words, plain text; do not use the # "
    "character anywhere except to wrap the stimulus itself; if the stimulus "
    "is a question or reveals details a student should recall from the "
    "slide without looking at it, do not include unnecessary information, "
    "and do not include the answer to the question. Cover every slide "
    "number given to you, one line each."
)

BATCH_VISION_PROMPT = (
    "Each image below is one lecture slide, labeled with its slide number. "
    "For EACH image, return one line in this exact format and nothing else:\n"
    "SLIDE <n>: #<stimulus, max 15 words, plain text>#\n\n"
    "No explanation, no reasoning, no hashtags other than the required "
    "wrapping. If the stimulus is a question or reveals details a student "
    "should recall from the slide without looking at it, do not include "
    "unnecessary information, and do not include the answer to the question."
)


def _parse_batch_stimuli(response_text, expected_slide_numbers):
    """
    Parses "SLIDE <n>: #...#" lines out of a batched completion into
    {slide_number: stimulus_text}. A slide is left OUT of the dict (rather
    than mapped to "") if it's missing or explicitly "#NONE#", so the caller
    can route it to the vision fallback exactly like a missing response.
    """
    results = {}
    for match in SLIDE_LINE_RE.finditer(response_text or ""):
        slide_num = int(match.group(1))
        stimulus = match.group(2).strip()
        if slide_num in expected_slide_numbers and stimulus and stimulus.upper() != "NONE":
            results[slide_num] = stimulus
    return results


def _request_batch_text_stimuli(client, batch):
    """batch: list of (slide_number, extracted_text). Returns {slide_number: stimulus}."""
    prompt_parts = []
    for slide_num, text in batch:
        snippet = text.strip() if text.strip() else "(no extractable text on this slide)"
        prompt_parts.append(f"### SLIDE {slide_num} ###\n{snippet}")

    completion = client.chat.completions.create(
        model="deepseek-flash",
        messages=[
            {"role": "system", "content": BATCH_TEXT_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(prompt_parts)},
        ],
        stream=False,
        reasoning_effort="low",
        extra_body={"thinking": {"type": "enabled"}},
    )
    response_text = completion.choices[0].message.content or ""
    return _parse_batch_stimuli(response_text, {n for n, _ in batch})


def _request_batch_vision_stimuli(client, batch_images):
    """batch_images: list of (slide_number, image_path). Returns {slide_number: stimulus}."""
    content = [{"type": "text", "text": BATCH_VISION_PROMPT}]
    for slide_num, image_path in batch_images:
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
        content.append({"type": "text", "text": f"Slide {slide_num}:"})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
        })

    vision_response = client.chat.completions.create(
        model="deepseek-flash",
        messages=[{"role": "user", "content": content}],
        stream=False,
        reasoning_effort="low",
        extra_body={"thinking": {"type": "enabled"}},
    )
    response_text = vision_response.choices[0].message.content or ""
    return _parse_batch_stimuli(response_text, {n for n, _ in batch_images})


# --------------------------------------------------------------------------
# STEP 5: Main conversion pipeline
# --------------------------------------------------------------------------
def convert_pdf_to_anki_deck(pdf_path, output_apkg_path, deck_name=None,
                              api_key=None, base_url="https://api.deepseek.com",
                              pages_per_batch=None):
    """
    api_key: DeepSeek API key to use for this conversion. If not given,
             falls back to the DEEPSEEK_API_KEY environment variable (CLI use).
             The web app passes each visitor's own key in explicitly so no
             single key is shared across all users of a public deployment.
    pages_per_batch: how many slides go into each API call. Defaults to
             DEFAULT_PAGES_PER_BATCH (4), or the PAGES_PER_BATCH env var.
    """
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if api_key is None:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("No DeepSeek API key provided (pass api_key= or set DEEPSEEK_API_KEY).")

    if pages_per_batch is None:
        pages_per_batch = int(os.environ.get("PAGES_PER_BATCH", DEFAULT_PAGES_PER_BATCH))
    pages_per_batch = max(1, pages_per_batch)

    if deck_name is None:
        deck_name = f"{os.path.splitext(os.path.basename(pdf_path))[0]}"

    # Temporary directory to hold rendered slide PNGs before packaging.
    temp_media_dir = tempfile.mkdtemp(prefix="anki_slides_")
    media_file_paths = []
    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        doc = pymupdf.open(pdf_path)
        model = create_anki_model()
        deck = genanki.Deck(DECK_ID, deck_name)

        total_pages = doc.page_count
        print(f"Processing {total_pages} slide(s) from '{pdf_path}' "
              f"in batches of {pages_per_batch}...")

        # --- Render every slide and extract its text up front. The image is
        #     needed for every card's back regardless of which pass below
        #     ends up supplying its front-side stimulus. ---
        slide_texts = {}                # slide_num (1-based) -> extracted text
        slide_image_paths = {}          # slide_num -> (image_path, image_filename)
        subheadings_by_slide = {}

        for page_index in range(total_pages):
            slide_num = page_index + 1
            page = doc.load_page(page_index)

            blocks_info = extract_slide_text_blocks(page)
            slide_texts[slide_num] = " ".join(b["text"] for b in blocks_info)
            subheadings_by_slide[slide_num] = ["anki automator"]

            image_path, image_filename = render_slide_image(page, page_index, temp_media_dir)
            media_file_paths.append(image_path)
            slide_image_paths[slide_num] = (image_path, image_filename)

        stimuli = {}  # slide_num -> final stimulus text
        slide_numbers = list(range(1, total_pages + 1))

        # --- Pass 1: batched text-based stimulus generation ---
        for i in range(0, total_pages, pages_per_batch):
            batch_numbers = slide_numbers[i:i + pages_per_batch]
            batch = [(n, slide_texts[n]) for n in batch_numbers]
            print(f"  Text pass: slides {batch_numbers[0]}-{batch_numbers[-1]}")
            stimuli.update(_request_batch_text_stimuli(client, batch))

        # --- Pass 2: batched vision fallback, only for slides pass 1 missed ---
        missing = [n for n in slide_numbers if n not in stimuli]
        for i in range(0, len(missing), pages_per_batch):
            batch_numbers = missing[i:i + pages_per_batch]
            batch_images = [(n, slide_image_paths[n][0]) for n in batch_numbers]
            print(f"  Vision fallback: slides {batch_numbers}")
            stimuli.update(_request_batch_vision_stimuli(client, batch_images))

        # --- Assemble cards in original slide order ---
        for slide_num in slide_numbers:
            result = stimuli.get(slide_num, "")
            front_html = build_front_html(
                result, subheadings_by_slide[slide_num], slide_num - 1
            )
            _, image_filename = slide_image_paths[slide_num]
            back_html = build_back_html(image_filename)

            note = genanki.Note(model=model, fields=[front_html, back_html])
            deck.add_note(note)
            print(f"  Slide {slide_num}/{total_pages}: heading={result!r}")

        doc.close()

        # --- Package the deck: media_files needs FULL local paths here,
        #     while the HTML above only ever referenced the basenames. ---
        package = genanki.Package(deck)
        package.media_files = media_file_paths
        package.write_to_file(output_apkg_path)

        print(f"\nDone. Wrote {total_pages} card(s) to '{output_apkg_path}'.")

    finally:
        # Clean up the temp PNGs now that they've been packaged into the .apkg.
        shutil.rmtree(temp_media_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Convert a PDF of lecture slides into an Anki deck "
                    "(one flashcard per slide)."
    )
    parser.add_argument("pdf_path", help="Path to the input PDF file.")
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Path to the output .apkg file (default: <pdf_name>.apkg)",
    )
    parser.add_argument(
        "--deck-name",
        default=None,
        help="Custom name for the Anki deck (default derived from PDF filename).",
    )
    parser.add_argument(
        "--pages-per-batch",
        type=int,
        default=None,
        help=f"Slides bundled into each API call (default: {DEFAULT_PAGES_PER_BATCH}).",
    )
    args = parser.parse_args()

    output_path = args.output
    if output_path is None:
        base = os.path.splitext(os.path.basename(args.pdf_path))[0]
        output_path = f"{base}.apkg"

    try:
        convert_pdf_to_anki_deck(
            args.pdf_path, output_path, args.deck_name,
            pages_per_batch=args.pages_per_batch,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()