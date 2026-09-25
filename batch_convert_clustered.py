#!/usr/bin/env python3
"""
batch_convert_pdfs_to_anki.py

Runs pdf_slides_to_anki.py's conversion pipeline over MULTIPLE PDF files
in one command. Accepts individual PDF paths and/or directories (any
directory given is scanned, non-recursively, for *.pdf files).

Each PDF is converted independently -- one bad/corrupt PDF will not stop
the rest of the batch. A summary of successes/failures is printed at the end.

Dependencies (same as the underlying script):
    pip install pymupdf genanki

Usage:
    # Sequential (default) -- safest, easiest to read progress output
    python batch_convert_pdfs_to_anki.py lecture1.pdf lecture2.pdf -o decks/

    # Point it at a folder of PDFs instead of listing them one by one
    python batch_convert_pdfs_to_anki.py ./my_lecture_pdfs/ -o decks/

    # Parallel -- convert up to 4 PDFs at the same time (separate processes)
    python batch_convert_pdfs_to_anki.py ./my_lecture_pdfs/ -o decks/ --workers 4
"""

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

# Make sure we can import the sibling script regardless of the caller's cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pdf_to_anki_core import convert_pdf_to_anki_deck  # noqa: E402


# --------------------------------------------------------------------------
# Input resolution: turn a mix of file paths / directory paths into a
# deduplicated, ordered list of PDF files.
# --------------------------------------------------------------------------
def resolve_input_pdfs(raw_paths):
    resolved = []
    seen = set()

    for raw_path in raw_paths:
        if not os.path.exists(raw_path):
            print(f"Warning: path does not exist, skipping: {raw_path}", file=sys.stderr)
            continue

        if os.path.isdir(raw_path):
            # Non-recursive scan of the directory for .pdf files.
            for entry in sorted(os.listdir(raw_path)):
                if entry.lower().endswith(".pdf"):
                    full_path = os.path.abspath(os.path.join(raw_path, entry))
                    if full_path not in seen:
                        resolved.append(full_path)
                        seen.add(full_path)
        elif raw_path.lower().endswith(".pdf"):
            full_path = os.path.abspath(raw_path)
            if full_path not in seen:
                resolved.append(full_path)
                seen.add(full_path)
        else:
            print(f"Warning: not a PDF, skipping: {raw_path}", file=sys.stderr)

    return resolved


def build_output_path(pdf_path, output_dir, used_names):
    """
    Derives the .apkg output path from a PDF's filename, avoiding collisions
    if two input PDFs (from different folders) happen to share a basename.
    """
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    candidate = f"{base}.apkg"
    counter = 2
    while candidate in used_names:
        candidate = f"{base}_{counter}.apkg"
        counter += 1
    used_names.add(candidate)
    return os.path.join(output_dir, candidate)


# --------------------------------------------------------------------------
# Per-file worker. Must be a top-level function (not a closure/lambda) so it
# can be pickled and sent to worker processes when running in parallel.
# --------------------------------------------------------------------------
def convert_one(pdf_path, output_apkg_path, pages_per_batch=None):
    """
    Converts a single PDF to a single .apkg. Never raises -- returns a
    result dict instead, so one failure doesn't take down the whole batch
    (or a whole ProcessPoolExecutor worker).
    """
    start = time.time()
    try:
        convert_pdf_to_anki_deck(pdf_path, output_apkg_path, pages_per_batch=pages_per_batch)
        elapsed = time.time() - start
        return {
            "pdf_path": pdf_path,
            "output_path": output_apkg_path,
            "success": True,
            "error": None,
            "elapsed": elapsed,
        }
    except Exception as exc:  # noqa: BLE001 - deliberately broad: isolate per-file failures
        elapsed = time.time() - start
        return {
            "pdf_path": pdf_path,
            "output_path": output_apkg_path,
            "success": False,
            "error": str(exc),
            "elapsed": elapsed,
        }


# --------------------------------------------------------------------------
# Batch drivers
# --------------------------------------------------------------------------
def run_sequential(jobs, pages_per_batch=None):
    results = []
    for i, (pdf_path, output_path) in enumerate(jobs, start=1):
        print(f"\n[{i}/{len(jobs)}] Converting: {pdf_path}")
        result = convert_one(pdf_path, output_path, pages_per_batch=pages_per_batch)
        results.append(result)
        if result["success"]:
            print(f"  -> OK ({result['elapsed']:.1f}s): {result['output_path']}")
        else:
            print(f"  -> FAILED: {result['error']}", file=sys.stderr)
    return results


def run_parallel(jobs, workers, pages_per_batch=None):
    results = []
    print(f"Running {len(jobs)} conversion(s) across {workers} worker process(es)...\n")

    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_to_pdf = {
            executor.submit(convert_one, pdf_path, output_path, pages_per_batch): pdf_path
            for pdf_path, output_path in jobs
        }

        for future in as_completed(future_to_pdf):
            result = future.result()
            results.append(result)
            if result["success"]:
                print(f"OK   ({result['elapsed']:.1f}s): {result['pdf_path']} -> {result['output_path']}")
            else:
                print(f"FAIL ({result['elapsed']:.1f}s): {result['pdf_path']} -- {result['error']}", file=sys.stderr)

    return results


def print_summary(results):
    succeeded = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    print("\n" + "=" * 60)
    print(f"Batch complete: {len(succeeded)} succeeded, {len(failed)} failed "
          f"(of {len(results)} total)")

    if succeeded:
        print("\nGenerated decks:")
        for r in succeeded:
            print(f"  - {r['output_path']}")

    if failed:
        print("\nFailed conversions:")
        for r in failed:
            print(f"  - {r['pdf_path']}: {r['error']}")

    print("=" * 60)


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Convert multiple lecture-slide PDFs into Anki decks in one run."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="PDF file paths and/or directories containing PDFs.",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=".",
        help="Directory to write the .apkg files into (default: current directory).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of PDFs to convert simultaneously in separate processes "
             "(default: 1, i.e. sequential). Try 2-4 depending on your CPU/RAM.",
    )
    parser.add_argument(
        "--pages-per-batch",
        type=int,
        default=None,
        help="Slides bundled into each API call, per PDF (default: 4).",
    )
    args = parser.parse_args()

    pdf_paths = resolve_input_pdfs(args.inputs)
    if not pdf_paths:
        print("No PDF files found in the given inputs.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)

    used_names = set()
    jobs = [
        (pdf_path, build_output_path(pdf_path, args.output_dir, used_names))
        for pdf_path in pdf_paths
    ]

    print(f"Found {len(jobs)} PDF(s) to convert:")
    for pdf_path, output_path in jobs:
        print(f"  {pdf_path}  ->  {output_path}")

    if args.workers > 1:
        results = run_parallel(jobs, args.workers, pages_per_batch=args.pages_per_batch)
    else:
        results = run_sequential(jobs, pages_per_batch=args.pages_per_batch)

    print_summary(results)

    # Exit non-zero if anything failed, useful for scripting/CI.
    if any(not r["success"] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()