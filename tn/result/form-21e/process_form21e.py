#!/usr/bin/env python3
"""
Tamil Nadu 2026 Legislative Assembly - Form 21E PDF to JSON Converter.

Extracts candidate vote counts from Form 21E PDFs and combines with
reference data (candidates, AC summary, districts) to produce normalized JSON.

Usage:
    python process_form21e.py --input ac_pdfs/ --output ac_json/
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import pdfplumber
from pdf2image import convert_from_path
from PIL import Image
import numpy as np
import pytesseract

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
RESULT_DIR = SCRIPT_DIR.parent
CANDIDATE_CSV = RESULT_DIR / "ac_candidate.csv"
SUMMARY_CSV = RESULT_DIR / "ac_summary.csv"
DISTRICT_CSV = RESULT_DIR / "district.csv"

# Summary field order as they appear in the votes column / page text
SUMMARY_KEYS = ["total_electors", "valid_votes_polled", "nota_votes", "rejected_votes", "tendered_votes"]

# ---------------------------------------------------------------------------
# Reference data loaders
# ---------------------------------------------------------------------------

def load_districts():
    """Load district data keyed by uppercase district name."""
    districts = {}
    with open(DISTRICT_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = r["name_en"].strip().upper()
            districts[key] = {
                "district_id": int(r["dcode1"].strip()),
                "district_name": r["name_en"].strip(),
            }
    return districts


def load_ac_summary():
    """Load AC summary keyed by integer ac_code."""
    summary = {}
    with open(SUMMARY_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            summary[int(r["ac_code"].strip())] = r
    return summary


def load_candidates():
    """Load candidates grouped by integer ac_code, sorted by sl_no."""
    cands = defaultdict(list)
    with open(CANDIDATE_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cands[int(r["ac_code"].strip())].append(r)
    for ac in cands:
        cands[ac].sort(key=lambda x: int(x["sl_no"]))
    return cands


# ---------------------------------------------------------------------------
# PDF type detection
# ---------------------------------------------------------------------------

def is_text_pdf(pdf_path):
    """Return True if at least one page has a usable text layer."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if len(t.strip()) > 80:
                    return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Number / summary parsing helpers
# ---------------------------------------------------------------------------

def clean_int(token):
    """Parse integer from token, stripping commas/pipes/periods/brackets. Returns None on failure."""
    cleaned = re.sub(r"[,|.\s()\[\]]", "", str(token))
    if cleaned.isdigit():
        return int(cleaned)
    return None


_SUMMARY_PATTERNS = [
    ("total_electors",    re.compile(r"total\s+numbers?\s+of\s+electors?", re.I)),
    # Fuzzy "polled" matches OCR variants: polled / polied / pol1ed / p0lled etc.
    ("valid_votes_polled", re.compile(r"valid\s+votes?\s+p[o0][il][il]e?d", re.I)),
    ("nota_votes",        re.compile(r"none\s+of\s+the\s+above|nota", re.I)),
    ("rejected_votes",    re.compile(r"rejected\s+votes?", re.I)),
    ("tendered_votes",    re.compile(r"tendered\s+votes?", re.I)),
]

# Minimum plausible values for key summary fields (rejects OCR noise and serial numbers)
_SUMMARY_MIN = {
    "total_electors": 50000,
    "valid_votes_polled": 5000,
}


def parse_summary_from_text(text):
    """Extract summary values from OCR/pdfplumber text using regex."""
    summary = {}
    lines = text.splitlines()
    # Build a parallel list of "joined" lines: each entry is current + next joined,
    # so multi-line labels (e.g. "valid votes\npolled:") are matched as one string.
    joined = []
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        joined.append(line + " " + nxt)

    for i, line in enumerate(lines):
        for key, pat in _SUMMARY_PATTERNS:
            if key in summary:
                continue
            # Match against both the single line and the joined (line + next line)
            if pat.search(line) or pat.search(joined[i]):
                min_val = _SUMMARY_MIN.get(key, 0)
                # Search current line + next 3 for a plausible number.
                # Period is included as a thousands-separator (Indian: 1.61,574).
                # min_val guards against serial numbers picking up as summary values.
                for search_line in lines[i:i + 4]:
                    nums = re.findall(r"\b(\d[\d,.]*)\b", search_line)
                    for n in nums:
                        val = clean_int(n)
                        if val is not None and val >= min_val:
                            summary[key] = val
                            break
                    if key in summary:
                        break
    return summary


# ---------------------------------------------------------------------------
# Text-layer PDF extraction (pdfplumber)
# ---------------------------------------------------------------------------

_SKIP_WORDS = {
    "number", "of", "votes", "polled", "serial", "no.", "no",
    "(1)", "(2)", "(3)", "(4)", "total", "constituency.", "constituency",
    "assembly", "return", "election", "form", "form-21e", "form21e",
    "statutory", "rules", "order", "conduct", "elections", "see", "rule",
    "64", "legislative", "party", "affiliation", "candidate", "name",
    "page", "1", "2", "3",
}


_POLLED_RE = re.compile(r"p[o0][il][il]e?d", re.I)


def extract_right_column_numbers(pdf_path):
    """
    For text-layer PDFs: extract vote numbers from the right column using
    pdfplumber word coordinates.  Column anchor is detected from the
    'polled' header word position (handles OCR garbling like 'poIIed').
    Falls back to 75% of page width if not found.
    """
    numbers = []
    header_seen = False
    anchor_x = None

    with pdfplumber.open(pdf_path) as pdf:
        # Collect all words per page first (avoid re-opening)
        all_page_words = []
        for page in pdf.pages:
            all_page_words.append((page.width, page.extract_words() or []))

    # Pass 1: find votes-column x-anchor from the 'polled' header word
    for pw, words in all_page_words:
        for w in sorted(words, key=lambda w: w["top"]):
            tl = w["text"].lower().rstrip(".")
            if "polled" in tl or _POLLED_RE.search(tl):
                # 25pt left margin from the header word; never below 65% of width
                anchor_x = max(w["x0"] - 25, pw * 0.65)
                break
        if anchor_x is not None:
            break

    # Pass 2: extract numbers right of the anchor
    for pw, words in all_page_words:
        threshold_x = anchor_x if anchor_x is not None else pw * 0.75
        right_words = sorted(
            [w for w in words if w["x0"] >= threshold_x],
            key=lambda w: w["top"],
        )
        for w in right_words:
            token = w["text"].strip()
            token_lower = token.lower().rstrip(".")
            if not header_seen:
                if "polled" in token_lower or _POLLED_RE.search(token_lower):
                    header_seen = True
                continue
            if token_lower in _SKIP_WORDS:
                continue
            val = clean_int(token)
            if val is not None:
                numbers.append(val)

    return numbers


# ---------------------------------------------------------------------------
# Scanned PDF extraction (pytesseract)
# ---------------------------------------------------------------------------

def _parse_col_numbers(text):
    """Extract integers from votes column OCR text, skipping pre-header content."""
    lower = text.lower()
    marker = lower.rfind("polled")
    relevant = text[marker + len("polled"):] if marker >= 0 else text
    numbers = []
    for token in re.split(r"\s+", relevant):
        val = clean_int(token)
        if val is not None:
            numbers.append(val)
    return numbers


def _binarize(arr, threshold):
    """Convert grayscale array to binary image: pixels < threshold → black."""
    binary = np.where(arr < threshold, 0, 255).astype(np.uint8)
    return Image.fromarray(binary)


def _score_candidate_nums(nums, total_electors):
    """
    Score how many numbers look like plausible candidate votes.
    Penalizes results where the maximum vote is suspiciously small
    (indicates leading digits were truncated by too-narrow crop).
    """
    if not nums:
        return 0
    valid = [n for n in nums if n > 0 and (not total_electors or n < total_electors)]
    count = len(valid)
    if count == 0:
        return 0
    # In Tamil Nadu elections the top candidate typically gets ≥ 5% of total electors.
    # If max(valid) is below that threshold, the numbers are likely digit-truncated.
    if total_electors and max(valid) < total_electors * 0.05:
        return count * 0.05  # severe penalty → choose wider crop
    return count


def ocr_votes_column(img, total_electors=None):
    """
    Extract vote numbers from the rightmost column of a scanned form page.

    Tries the 80% crop with brightness-appropriate threshold first.
    Falls back to 75% then 70% only when fewer than 5 plausible numbers found.
    Returns (numbers_list, raw_text) so caller can extract summary from same OCR.
    """
    img_w, img_h = img.size
    gray = img.convert("L")
    arr = np.array(gray)
    mean_val = float(arr.mean())
    threshold = 160 if mean_val > 170 else 80

    best_nums = []
    best_score = -1
    best_text = ""

    for crop_start in [0.80, 0.75, 0.70, 0.65]:
        col_arr = np.array(gray.crop((int(img_w * crop_start), 0, img_w, img_h)))
        img_bin = _binarize(col_arr, threshold)
        text = pytesseract.image_to_string(img_bin, config="--psm 6 --oem 3")
        nums = _parse_col_numbers(text)
        score = _score_candidate_nums(nums, total_electors)
        if score > best_score:
            best_score = score
            best_nums = nums
            best_text = text
        if best_score >= 5:
            break

    return best_nums, best_text


def ocr_summary_region(img):
    """
    OCR the bottom half of the last page (full width) for summary label extraction.
    Much faster than full-page OCR while still capturing total/valid/nota/rejected rows.
    """
    img_w, img_h = img.size
    bottom = img.crop((0, int(img_h * 0.50), img_w, img_h))
    gray = bottom.convert("L")
    arr = np.array(gray)
    threshold = 160 if arr.mean() > 170 else 80
    return pytesseract.image_to_string(
        _binarize(arr, threshold), config="--psm 6 --oem 3"
    )


def extract_scanned_pdf(pdf_path, total_electors=None):
    """
    For scanned PDFs: rasterize pages at 150 DPI, OCR the votes column per page.
    Appends a bottom-half OCR of the last page for summary-label extraction.
    Returns (all_numbers, summary_text).
    """
    images = convert_from_path(str(pdf_path), dpi=150)
    all_numbers = []
    col_texts = []

    for img in images:
        nums, col_text = ocr_votes_column(img, total_electors=total_electors)
        all_numbers.extend(nums)
        col_texts.append(col_text)

    combined_text = "\n".join(col_texts)
    if images:
        combined_text += "\n" + ocr_summary_region(images[-1])

    return all_numbers, combined_text


def split_candidates_from_summary(all_numbers, n_cands, total_electors):
    """
    Split raw OCR number stream into candidate votes and trailing summary values.

    Uses 65% of total_electors as the upper bound.  This catches summary-level
    values (total_electors ≈ 100%, valid_votes ≈ 70-85%) while accepting even
    very large candidate vote totals (winner rarely exceeds 55% of all electors).
    """
    candidate_votes = []
    cutoff = int(total_electors * 0.65) if total_electors else None
    for num in all_numbers:
        if len(candidate_votes) >= n_cands:
            break
        if cutoff and num >= cutoff:
            break
        candidate_votes.append(num)
    return candidate_votes


# ---------------------------------------------------------------------------
# Core per-AC transformation
# ---------------------------------------------------------------------------

def transform_ac(ac_num, districts, ac_summary, all_candidates, pdf_path):
    """
    Build the JSON document for one Assembly Constituency.
    Returns (doc, warnings_list) or (None, [error_str]).
    """
    warnings = []

    # --- Reference data ---
    ac_info = ac_summary.get(ac_num)
    if not ac_info:
        return None, [f"AC{ac_num:03d} not in ac_summary.csv"]

    candidates = all_candidates.get(ac_num, [])
    n_cands = len(candidates)
    if n_cands == 0:
        return None, [f"AC{ac_num:03d} has no candidates in ac_candidate.csv"]

    # --- District ---
    dist_key = ac_info["district"].strip().upper()
    district = districts.get(dist_key) or {
        "district_id": None,
        "district_name": ac_info["district"].strip(),
    }

    # --- Extract votes from PDF ---
    if not pdf_path.exists():
        return None, [f"PDF not found: {pdf_path}"]

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            n_pages = len(pdf.pages)
        if n_pages == 0:
            return None, [f"AC{ac_num:03d}: PDF has 0 pages"]
    except Exception as e:
        return None, [f"AC{ac_num:03d}: cannot open PDF: {e}"]

    # Total electors from reference data (used as upper-bound guard)
    def safe_int(val, default=None):
        if val is None:
            return default
        try:
            return int(str(val).replace(",", "").strip())
        except (ValueError, TypeError):
            return default

    ref_total_electors = safe_int(ac_info.get("total"))

    summary_from_pdf = {}
    try:
        if is_text_pdf(pdf_path):
            all_numbers = extract_right_column_numbers(pdf_path)
            all_text = ""
            with pdfplumber.open(str(pdf_path)) as pdf:
                for page in pdf.pages:
                    all_text += (page.extract_text() or "") + "\n"
            summary_from_pdf = parse_summary_from_text(all_text)
        else:
            all_numbers, full_text = extract_scanned_pdf(
                pdf_path, total_electors=ref_total_electors
            )
            summary_from_pdf = parse_summary_from_text(full_text)
    except Exception as e:
        return None, [f"AC{ac_num:03d}: extraction failed: {e}"]

    # --- Split numbers into candidate votes and summary values ---
    # Use total_electors as a hard upper bound: stop when a summary-scale number appears
    candidate_votes = split_candidates_from_summary(
        all_numbers, n_cands, ref_total_electors
    )
    trailing_numbers = all_numbers[len(candidate_votes):]

    if len(candidate_votes) < n_cands:
        warnings.append(
            f"AC{ac_num:03d}: expected {n_cands} candidate votes, got {len(candidate_votes)}"
        )
        # Pad with None
        candidate_votes += [None] * (n_cands - len(candidate_votes))

    # Fill summary from trailing numbers if regex didn't catch all fields.
    # Apply minimum-value guards to avoid assigning noise/serial-numbers.
    for i, key in enumerate(SUMMARY_KEYS):
        if key not in summary_from_pdf and i < len(trailing_numbers):
            val = trailing_numbers[i]
            min_val = _SUMMARY_MIN.get(key, 0)
            if val >= min_val:
                summary_from_pdf[key] = val

    # Validate extracted valid_votes_polled against known total_electors.
    # Reject values outside the plausible range: 15%–100% of total electors.
    vv = summary_from_pdf.get("valid_votes_polled")
    if vv is not None and ref_total_electors:
        if vv > ref_total_electors or vv < ref_total_electors * 0.15:
            summary_from_pdf.pop("valid_votes_polled", None)

    # Fallback: derive valid_votes_polled from candidate vote sum when the
    # sum is plausible.  Primary condition requires all candidates to have
    # votes (exact match); secondary (looser) condition accepts partial
    # extraction when cand_sum is at least 25% of total electors.
    if "valid_votes_polled" not in summary_from_pdf:
        cand_sum_tmp = sum(v for v in candidate_votes if v is not None)
        if ref_total_electors and cand_sum_tmp < ref_total_electors:
            all_have_votes = all(v is not None for v in candidate_votes)
            if all_have_votes and cand_sum_tmp > 5000:
                summary_from_pdf["valid_votes_polled"] = cand_sum_tmp
            elif cand_sum_tmp >= ref_total_electors * 0.25:
                summary_from_pdf["valid_votes_polled"] = cand_sum_tmp

    # --- Determine winner ---
    vote_pairs = [
        (cand, vote)
        for cand, vote in zip(candidates, candidate_votes)
        if vote is not None
    ]
    sorted_by_votes = sorted(vote_pairs, key=lambda x: x[1], reverse=True)
    winner_sl = None
    margin = None
    if sorted_by_votes:
        winner_sl = int(sorted_by_votes[0][0]["sl_no"])
        first = sorted_by_votes[0][1]
        second = sorted_by_votes[1][1] if len(sorted_by_votes) > 1 else 0
        margin = first - second

    # --- Validation ---
    # In Form 21E, "valid_votes_polled" = sum of candidate votes only (NOTA excluded)
    valid_votes = summary_from_pdf.get("valid_votes_polled")
    cand_sum = sum(v for v in candidate_votes if v is not None)
    if valid_votes is not None and cand_sum != valid_votes:
        diff = abs(cand_sum - valid_votes)
        # Tolerate small OCR rounding errors (< 0.1% of total)
        tol = max(10, int(valid_votes * 0.001))
        if diff > tol:
            warnings.append(
                f"AC{ac_num:03d}: cand_sum({cand_sum}) != valid_votes_polled({valid_votes}) "
                f"(diff={diff})"
            )

    # --- Build candidates JSON ---
    cands_json = []
    for cand, votes in zip(candidates, candidate_votes):
        sl = int(cand["sl_no"])
        is_winner = sl == winner_sl
        cands_json.append({
            "candidate_id": int(cand["id"].strip()),
            "sl_no": sl,
            "name": cand["name_en"].strip(),
            "party": cand["party_en"].strip(),
            "symbol": cand.get("symbol_en", "").strip() or None,
            "total_secured_votes": votes,
            "evm_votes": None,
            "postal_votes": None,
            "winner": is_winner,
            "margin": margin if is_winner else None,
        })

    # NOTA entry
    cands_json.append({
        "candidate_id": 0,
        "sl_no": 0,
        "name": "NOTA",
        "party": "NOTA",
        "symbol": "None of the Above",
        "total_secured_votes": summary_from_pdf.get("nota_votes"),
        "evm_votes": None,
        "postal_votes": None,
        "winner": False,
        "margin": None,
    })

    # --- AC-level stats (registered electors from ac_summary) ---
    stats = {
        "male": safe_int(ac_info.get("male")),
        "female": safe_int(ac_info.get("female")),
        "third_gender": safe_int(ac_info.get("third_gender"), 0),
        "total_electors": ref_total_electors,
        "total_polling_stations": safe_int(ac_info.get("polling_stations")),
        "valid_votes_polled": summary_from_pdf.get("valid_votes_polled"),
        "nota_votes": summary_from_pdf.get("nota_votes"),
        "rejected_votes": summary_from_pdf.get("rejected_votes"),
        "tendered_votes": summary_from_pdf.get("tendered_votes"),
    }

    doc = {
        "state": "Tamil Nadu",
        "election_year": 2026,
        "district": district,
        "assembly_constituency": {
            "ac_code": ac_num,
            "ac_name": ac_info["ac_name"].strip(),
            "stats": stats,
            "candidates": cands_json,
        },
    }
    return doc, warnings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Form 21E PDF → JSON converter")
    parser.add_argument("--input", required=True, help="Directory containing AC PDFs")
    parser.add_argument("--output", required=True, help="Output directory for JSON files")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    if not input_dir.is_absolute():
        input_dir = SCRIPT_DIR / input_dir
    if not output_dir.is_absolute():
        output_dir = SCRIPT_DIR / output_dir

    output_dir.mkdir(parents=True, exist_ok=True)

    # Load reference data
    districts = load_districts()
    ac_summary = load_ac_summary()
    all_candidates = load_candidates()

    # Determine AC list (1-234, AC111 missing)
    ac_list = list(range(1, 235))

    results_all = []
    errors = []
    success = skip = error = 0

    for ac_num in ac_list:
        ac_tag = f"AC{ac_num:03d}"
        pdf_path = input_dir / f"{ac_tag}.pdf"

        if not pdf_path.exists():
            msg = f"{ac_tag}: PDF not found"
            errors.append({"ac_code": ac_num, "error": msg})
            print(f"  SKIP {ac_tag}: PDF not found", file=sys.stderr)
            skip += 1
            continue

        n_cands = len(all_candidates.get(ac_num, []))
        ac_name = ac_summary.get(ac_num, {}).get("ac_name", "")
        print(f"  [{ac_num:03d}/234] {ac_tag} — {ac_name} ({n_cands} candidates) …", end="", flush=True)

        try:
            doc, warnings = transform_ac(
                ac_num, districts, ac_summary, all_candidates, pdf_path
            )
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            msg = f"{ac_tag}: unhandled exception: {exc}"
            errors.append({"ac_code": ac_num, "error": msg, "traceback": tb})
            print(f" ERR ({exc})", flush=True)
            error += 1
            continue

        if doc is None:
            msg = warnings[0] if warnings else f"{ac_tag}: transform returned None"
            errors.append({"ac_code": ac_num, "error": msg})
            print(f" SKIP ({msg})", flush=True)
            skip += 1
            continue

        if warnings:
            for w in warnings:
                errors.append({"ac_code": ac_num, "warning": w})
                print(f"\n    WARN: {w}", file=sys.stderr)

        out_path = output_dir / f"{ac_tag}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))

        results_all.append(doc)
        n_done = success + skip + error + 1
        winner_name = next(
            (c["name"] for c in doc["assembly_constituency"]["candidates"] if c.get("winner")),
            "?"
        )
        print(f" ✓  winner={winner_name}", flush=True)
        success += 1

    # Write combined JSON
    combined_path = output_dir / "form21e_all.json"
    with open(combined_path, "w", encoding="utf-8") as f:
        json.dump(results_all, f, ensure_ascii=False, separators=(",", ":"))

    # Write errors JSON
    errors_path = output_dir / "errors.json"
    with open(errors_path, "w", encoding="utf-8") as f:
        json.dump(errors, f, ensure_ascii=False, indent=2)

    print(
        f"\nDone: {success} OK, {skip} skipped, {error} errors",
        file=sys.stderr,
    )
    print(f"Output: {output_dir}", file=sys.stderr)
    if errors:
        print(f"Errors/warnings logged to: {errors_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
