#!/usr/bin/env python3
"""
Tamil Nadu 2026 Assembly Election - Form 20 CSV to JSON Transformer
Converts polling station wise vote data into normalized hierarchical JSON.
"""

import csv
import json
import os
import sys
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
JSON_DIR = os.path.join(BASE_DIR, "json")
FORM20_CSV_DIR = os.path.join(BASE_DIR, "csv")
PS_CSV_DIR = os.path.join(PARENT_DIR, "polling_stations", "eng", "csv")
DISTRICT_CSV = os.path.join(PARENT_DIR, "district.csv")
SUMMARY_CSV = os.path.join(PARENT_DIR, "ac_summary.csv")
CANDIDATE_CSV = os.path.join(PARENT_DIR, "ac_candidate.csv")


# ---------------------------------------------------------------------------
# Reference data loaders
# ---------------------------------------------------------------------------

def load_districts():
    """Load district data keyed by uppercase district name."""
    districts = {}
    with open(DISTRICT_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            name_upper = r["name_en"].strip().upper()
            districts[name_upper] = {
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
# Form-20 CSV helpers
# ---------------------------------------------------------------------------

def load_form20_rows(ac_num):
    """Read all rows from acXXX.csv."""
    path = os.path.join(FORM20_CSV_DIR, f"ac{ac_num:03d}.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def row_digit_sum(row):
    """Sum of all digit-named column values in a row (includes NOTA col '0')."""
    total = 0
    for k, v in row.items():
        if k.isdigit():
            try:
                total += int(v)
            except (ValueError, TypeError):
                pass
    return total


def determine_nota_col(rows, n_candidates):
    """
    Detect which column holds NOTA votes.
    - If column '0' has any non-zero value → NOTA is in '0'
    - Otherwise → NOTA is in column str(n_candidates + 1)
    """
    for r in rows:
        try:
            if int(r.get("0", 0)) > 0:
                return "0"
        except (ValueError, TypeError):
            pass
    return str(n_candidates + 1)


def get_best_row_for_ps(ps_rows):
    """
    Among duplicate rows for the same polling station, return the one
    with the highest digit-column sum (most votes = real data row).
    """
    return max(ps_rows, key=row_digit_sum)


def build_ps_vote_map(rows, max_ps_no):
    """
    Build a dict {ps_no (int) -> best_row} for ps_no 1..max_ps_no.
    Skips postal (ps=0) and summary rows (ps > max_ps_no).
    """
    grouped = defaultdict(list)
    for r in rows:
        try:
            ps_no = int(r["polling_station_no"])
        except (ValueError, TypeError):
            continue
        if ps_no < 1 or ps_no > max_ps_no:
            continue
        grouped[ps_no].append(r)

    result = {}
    for ps_no, ps_rows in grouped.items():
        result[ps_no] = get_best_row_for_ps(ps_rows)
    return result


def get_postal_row(rows):
    """Return the best row with polling_station_no == 0 (postal votes)."""
    postal = [r for r in rows if r.get("polling_station_no", "") == "0"]
    if not postal:
        return None
    return get_best_row_for_ps(postal)


def safe_int(value, default=0):
    """Convert value to int, stripping commas/whitespace. Returns default on failure."""
    if value is None:
        return default
    try:
        return int(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Polling-station name loader
# ---------------------------------------------------------------------------

def load_ps_names(ac_num):
    """Load polling station name map {ps_no (int) -> name (str)}."""
    path = os.path.join(PS_CSV_DIR, f"ac{ac_num:03d}.csv")
    if not os.path.exists(path):
        return {}
    names = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ps_no = int(r["polling_station_number"].strip())
                names[ps_no] = r["polling_station"].strip()
            except (ValueError, KeyError):
                pass
    return names


# ---------------------------------------------------------------------------
# Core transformation
# ---------------------------------------------------------------------------

def transform_ac(ac_num, districts, ac_summary, all_candidates):
    """Build the JSON document for one Assembly Constituency."""
    ac_info = ac_summary.get(ac_num)
    if not ac_info:
        return None

    # District lookup (case-insensitive)
    dist_name_upper = ac_info["district"].strip().upper()
    if dist_name_upper in districts:
        district = districts[dist_name_upper]
    else:
        # Fallback: title-case search
        matched = next(
            (v for k, v in districts.items() if k == dist_name_upper), None
        )
        district = matched or {
            "district_id": None,
            "district_name": ac_info["district"].strip(),
        }

    candidates = all_candidates.get(ac_num, [])
    n_cands = len(candidates)

    # Load form20 vote data
    rows = load_form20_rows(ac_num)
    if not rows:
        return None

    max_ps_no = safe_int(ac_info.get("polling_stations"), default=0)
    if max_ps_no == 0:
        return None

    # Determine NOTA column
    nota_col = determine_nota_col(rows, n_cands)

    # Postal votes row
    postal_row = get_postal_row(rows)

    # Best row per polling station
    ps_vote_map = build_ps_vote_map(rows, max_ps_no)

    # Load polling station names
    ps_names = load_ps_names(ac_num)

    # ------------------------------------------------------------------
    # Build polling_stations list
    # ------------------------------------------------------------------
    polling_stations = []
    for ps_no in range(1, max_ps_no + 1):
        ps_row = ps_vote_map.get(ps_no)
        ps_name = ps_names.get(ps_no)  # may be None if PS CSV missing

        if ps_row:
            cand_votes = sum(
                safe_int(ps_row.get(str(i), 0)) for i in range(1, n_cands + 1)
            )
            nota_votes = safe_int(ps_row.get(nota_col, 0))
            total_polled = cand_votes + nota_votes
        else:
            total_polled = None

        polling_stations.append(
            {
                "no": ps_no,
                "name": ps_name,
                "male": None,
                "female": None,
                "third_gender": 0,
                "total_votes": total_polled,
            }
        )

    # ------------------------------------------------------------------
    # Postal votes per candidate and NOTA
    # ------------------------------------------------------------------
    postal_by_cand = {}
    nota_postal = 0
    if postal_row:
        for cand in candidates:
            sl = cand["sl_no"]
            postal_by_cand[int(sl)] = safe_int(postal_row.get(str(sl), 0))
        nota_postal = safe_int(postal_row.get(nota_col, 0))

    # ------------------------------------------------------------------
    # Build secured votes and totals per candidate
    # ------------------------------------------------------------------
    cand_totals = {}
    cand_evm = {}
    cand_secured = {}

    for cand in candidates:
        sl = int(cand["sl_no"])
        secured = []
        evm_total = 0

        for ps_no in range(1, max_ps_no + 1):
            ps_row = ps_vote_map.get(ps_no)
            votes = safe_int(ps_row.get(str(sl), 0)) if ps_row else 0
            secured.append({"polling_station_no": ps_no, "votes": votes})
            evm_total += votes

        postal = postal_by_cand.get(sl, 0)
        total = evm_total + postal
        cand_totals[sl] = total
        cand_evm[sl] = evm_total
        cand_secured[sl] = secured

    # ------------------------------------------------------------------
    # NOTA secured votes and totals
    # ------------------------------------------------------------------
    nota_secured = []
    nota_evm_total = 0
    for ps_no in range(1, max_ps_no + 1):
        ps_row = ps_vote_map.get(ps_no)
        votes = safe_int(ps_row.get(nota_col, 0)) if ps_row else 0
        nota_secured.append({"polling_station_no": ps_no, "votes": votes})
        nota_evm_total += votes
    nota_grand_total = nota_evm_total + nota_postal

    # ------------------------------------------------------------------
    # Rejected votes total (AC level, from standard-format grand total row)
    # ------------------------------------------------------------------
    rejected_total = 0
    # Look for a summary row (ps_no = max_ps_no + 2) with rejected_votes
    for r in rows:
        try:
            ps_no = int(r["polling_station_no"])
        except (ValueError, TypeError):
            continue
        if ps_no == max_ps_no + 2:
            # Grand total row in standard format
            rv = safe_int(r.get("rejected_votes", 0))
            if rv > 0:
                rejected_total = rv
                break
    # Fallback: sum rejected_votes across all real PS rows (standard format)
    if rejected_total == 0:
        for ps_no, ps_row in ps_vote_map.items():
            rejected_total += safe_int(ps_row.get("rejected_votes", 0))
        # Add postal rejected
        if postal_row:
            rejected_total += safe_int(postal_row.get("rejected_votes", 0))

    # ------------------------------------------------------------------
    # Determine winner and margin
    # ------------------------------------------------------------------
    winner_sl = None
    margin = None
    if cand_totals:
        sorted_by_votes = sorted(cand_totals.items(), key=lambda x: x[1], reverse=True)
        winner_sl = sorted_by_votes[0][0]
        first_votes = sorted_by_votes[0][1]
        second_votes = sorted_by_votes[1][1] if len(sorted_by_votes) > 1 else 0
        margin = first_votes - second_votes

    # ------------------------------------------------------------------
    # Build candidate JSON objects
    # ------------------------------------------------------------------
    cands_json = []
    for cand in candidates:
        sl = int(cand["sl_no"])
        postal = postal_by_cand.get(sl, None) if postal_row else None
        evm = cand_evm.get(sl, 0)
        total = cand_totals.get(sl, 0)
        is_winner = (sl == winner_sl)

        cands_json.append(
            {
                "candidate_id": int(cand["id"].strip()),
                "name": cand["name_en"].strip(),
                "party": cand["party_en"].strip(),
                "symbol": cand["symbol_en"].strip() if cand.get("symbol_en") else None,
                "total_secured_votes": total,
                "postal_votes": postal,
                "evm_votes": evm,
                "rejected_votes": None,
                "nota_votes": None,
                "winner": is_winner,
                "margin": margin if is_winner else None,
                "secured_votes": cand_secured[sl],
            }
        )

    # NOTA entry
    cands_json.append(
        {
            "candidate_id": 0,
            "name": "NOTA",
            "party": "NOTA",
            "symbol": "None of the Above",
            "total_secured_votes": nota_grand_total,
            "postal_votes": nota_postal if postal_row else None,
            "evm_votes": nota_evm_total,
            "rejected_votes": rejected_total if rejected_total > 0 else None,
            "nota_votes": nota_grand_total,
            "winner": False,
            "margin": None,
            "secured_votes": nota_secured,
        }
    )

    # ------------------------------------------------------------------
    # AC-level stats (elector counts from ac_summary)
    # ------------------------------------------------------------------
    stats = {
        "male": safe_int(ac_info.get("male")),
        "female": safe_int(ac_info.get("female")),
        "third_gender": safe_int(ac_info.get("third_gender"), default=0),
        "total_votes": safe_int(ac_info.get("total")),
        "total_polling_stations": max_ps_no,
    }

    return {
        "state": "Tamil Nadu",
        "election_year": 2026,
        "district": district,
        "assembly_constituency": {
            "ac_code": ac_num,
            "ac_name": ac_info["ac_name"].strip(),
            "stats": stats,
            "polling_stations": polling_stations,
            "candidates": cands_json,
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    os.makedirs(JSON_DIR, exist_ok=True)

    districts = load_districts()
    ac_summary = load_ac_summary()
    all_candidates = load_candidates()

    # Determine which ACs to process
    if len(sys.argv) > 1:
        # Process specific AC numbers passed as arguments
        try:
            ac_list = [int(x) for x in sys.argv[1:]]
        except ValueError:
            print("Usage: convert_to_json.py [ac_number ...]", file=sys.stderr)
            sys.exit(1)
    else:
        # Process all ACs that have form20 CSV files
        ac_list = sorted(
            int(os.path.basename(f)[2:5])
            for f in os.listdir(FORM20_CSV_DIR)
            if f.startswith("ac") and f.endswith(".csv") and "_" not in f
        )

    success_count = 0
    skip_count = 0
    error_count = 0

    for ac_num in ac_list:
        form20_path = os.path.join(FORM20_CSV_DIR, f"ac{ac_num:03d}.csv")
        if not os.path.exists(form20_path):
            print(f"  SKIP AC{ac_num:03d}: no form20 CSV", file=sys.stderr)
            skip_count += 1
            continue

        try:
            doc = transform_ac(ac_num, districts, ac_summary, all_candidates)
            if doc is None:
                print(f"  SKIP AC{ac_num:03d}: transform returned None", file=sys.stderr)
                skip_count += 1
                continue

            out_path = os.path.join(JSON_DIR, f"ac{ac_num:03d}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))

            # Quick validation: verify candidate totals
            ac_data = doc["assembly_constituency"]
            for cand in ac_data["candidates"]:
                if cand["candidate_id"] == 0:
                    continue
                sv_sum = sum(sv["votes"] for sv in cand["secured_votes"])
                evm = cand["evm_votes"] or 0
                postal = cand["postal_votes"] or 0
                expected = evm + postal
                if sv_sum != evm:
                    print(
                        f"  WARN AC{ac_num:03d} cand {cand['candidate_id']}: "
                        f"secured_votes sum={sv_sum} != evm_votes={evm}",
                        file=sys.stderr,
                    )

            print(f"  OK   AC{ac_num:03d}: {ac_data['ac_name']} → ac{ac_num:03d}.json")
            success_count += 1

        except Exception as exc:
            import traceback
            print(f"  ERR  AC{ac_num:03d}: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            error_count += 1

    print(
        f"\nDone: {success_count} OK, {skip_count} skipped, {error_count} errors",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
