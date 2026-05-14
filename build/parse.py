#!/usr/bin/env python3
"""Parse all SBEC Disciplinary Action Report PDFs into a single records.json.

Strategy: pdftotext -layout, then split each non-empty line on runs of >=2
spaces, then classify the resulting cells by content:
  - dates match d{1,2}/d{1,2}/d{2,4}
  - sanctions match a known vocabulary
  - allegation codes match patterns like 2.1-VSCH, 10-IRWSM, Contract
  - everything else is treated as name / district / do-not-hire

This is more robust than character-position slicing because per-page
header spacing varies (yes, really).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPORTS_DIR = Path(__file__).resolve().parent.parent / "dar-reports"
OUT_PATH = Path(__file__).resolve().parent / "records.json"

DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")
ALLEG_RE = re.compile(r"^(?:\d{1,2}(?:\.\d+)?-[A-Z]+|Contract|CONTRACT)$")


def _looks_like_initials(token: str) -> bool:
    """True for tokens like 'JB', 'O.L.', 'J.W.', 'J.B.'."""
    t = token.strip(".,")
    if not t:
        return False
    # Period-separated initials: J.B., O.L.
    if "." in token and all(part == "" or len(part.strip(".,")) <= 1 for part in re.split(r"\.", token)):
        return True
    # 1-2 char all-caps: JB, AB
    if 1 <= len(t) <= 2 and t.isalpha() and t == t.upper():
        return True
    return False


def _cap_token(tok: str) -> str:
    """Capitalize one space-free token, preserving Mc/Mac, O', and hyphens."""
    if not tok:
        return tok
    # Jr./Sr./Esq. check before initials (would otherwise be eaten by the 2-char rule)
    up_strip = tok.upper().rstrip(".")
    if up_strip in {"JR", "SR", "ESQ"}:
        return tok[0].upper() + tok[1:].lower()
    if _looks_like_initials(tok):
        return tok  # leave initials alone
    # Roman-numeral suffixes (II..XII), preserve uppercase
    if re.fullmatch(r"(?:M{0,3})(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})", tok.upper()) and tok.upper() and len(tok) <= 5:
        if tok.upper() in {"I", "V", "X", "L", "C", "D", "M"} and len(tok) == 1:
            # Ambiguous single letter (could be initial) — leave as-is
            return tok.upper()
        return tok.upper()
    # Hyphenated: process each side
    if "-" in tok:
        return "-".join(_cap_token(p) for p in tok.split("-"))
    # Apostrophe (O'Brien, D'Amico)
    if "'" in tok and len(tok) > 2:
        head, sep, tail = tok.partition("'")
        return _cap_token(head) + "'" + (tail[:1].upper() + tail[1:].lower() if tail else "")
    # Mc / Mac prefix
    up = tok.upper()
    if up.startswith("MC") and len(tok) > 2 and tok[2:].isalpha():
        return "Mc" + tok[2].upper() + tok[3:].lower()
    if up.startswith("MAC") and len(tok) > 3 and tok[3:].isalpha() and not up.startswith("MACH"):
        return "Mac" + tok[3].upper() + tok[4:].lower()
    return tok[:1].upper() + tok[1:].lower()


DNH_LEAK_RE = re.compile(
    r"(?:\s*Not\s+Eligible\s+for\s+Hire\s*)?(?:\s*22A\.\d+\(d\)\(\d+\)\s*)?(?:\s*N/A\s*)?$",
    re.I,
)
DNH_LEAK_LEAD_RE = re.compile(r"^\s*Not\s+Eligible\s+for\s+Hire\s+", re.I)


def normalize_district(s: str) -> str:
    """Clean district strings that picked up Do-Not-Hire fragments via
    PDF wrap-column merging."""
    if not s:
        return ""
    out = DNH_LEAK_LEAD_RE.sub("", s).strip()
    # Strip trailing dnh-like fragments. Apply repeatedly because a
    # district can pick up several leakage tokens.
    while True:
        new = DNH_LEAK_RE.sub("", out).strip()
        if new == out:
            break
        out = new
    # Collapse multiple spaces from joins.
    out = re.sub(r"\s+", " ", out)
    return out


def normalize_name(s: str) -> str:
    """Return a Title-Case version of an all-caps name; leave already
    mixed-case names alone. Preserves Mc/Mac, O', hyphens, and initials.
    """
    s = re.sub(r"\s+", " ", (s or "").strip())
    if not s:
        return s
    # Only rewrite if the entire string is uppercase (no lowercase letters).
    has_alpha = any(c.isalpha() for c in s)
    if not has_alpha:
        return s
    if any(c.islower() for c in s):
        return s
    return " ".join(_cap_token(t) for t in s.split(" "))

SANCTION_CANON = {
    # raw upper key -> canonical title
    "INSCRIBED REPRIMAND": "Inscribed Reprimand",
    "REPRIMAND": "Reprimand",
    "SUSPENDED": "Suspension",
    "SUSPENSION": "Suspension",
    "PROBATED SUSPENSION": "Probated Suspension",
    "PROBATIONARY SUSPENSION": "Probated Suspension",
    "REVOKED": "Revocation",
    "REVOCATION": "Revocation",
    "PERMANENT REVOCATION": "Permanent Revocation",
    "PERMANENTLY REVOKED": "Permanent Revocation",
    "VOLUNTARY SURRENDER": "Voluntary Surrender",
    "PERMANENT SURRENDER": "Permanent Surrender",
    "PERMANENT VOLUNTARY SURRENDER": "Permanent Surrender",
    "CERTIFICATE DENIED": "Certificate Denied",
    "CERTIFICATION DENIED": "Certificate Denied",
    "CERTIFICATE CANCELLED": "Cancelled",
    "CANCELLED": "Cancelled",
    "CANCELLED FOR MISCONDUCT": "Cancelled for Misconduct",
    "RESTRICTION": "Restriction",
    "RESTRICTED": "Restriction",
    "REINSTATED": "Reinstated",
    "INDEFINITE SUSPENSION": "Indefinite Suspension",
    "ADMINISTRATIVE PENALTY": "Administrative Penalty",
    "RELINQUISHED": "Relinquished",
}


def normalize_date(s: str) -> str:
    m = DATE_RE.match(s.strip())
    if not m:
        return ""
    mm, dd, yy = m.groups()
    yy = int(yy)
    if yy < 100:
        yy = 2000 + yy if yy < 50 else 1900 + yy
    return f"{yy:04d}-{int(mm):02d}-{int(dd):02d}"


def is_date(s: str) -> bool:
    return bool(DATE_RE.match(s.strip()))


def canon_sanction(s: str) -> str:
    s_clean = re.sub(r"\s+", " ", s.strip())
    key = s_clean.upper()
    if key in SANCTION_CANON:
        return SANCTION_CANON[key]
    # Try without trailing punctuation
    key2 = key.rstrip(".,")
    if key2 in SANCTION_CANON:
        return SANCTION_CANON[key2]
    # Fall back: title-case the input
    return s_clean


def is_sanction(s: str) -> bool:
    key = re.sub(r"\s+", " ", s.strip()).upper().rstrip(".,")
    return key in SANCTION_CANON


def run_pdftotext(path: Path) -> str:
    return subprocess.check_output(
        ["pdftotext", "-layout", str(path), "-"], text=True, errors="replace"
    )


SKIP_PHRASES = (
    "SBEC Disciplinary Action Report",
    "SBEC Disciplinary Actions",
    "Sanctions issued",
    "By Last Name",
    "For Period",
    "Period ",
    "Report Date",
    "Page ",
    "Last Name",
    "Column Legend",
    "Allegation Code Description",
    "additional clarification",
    # Rich-format header fragments that wrap across multiple lines:
    "Sanction/Disposition",
    "Sanction Begin",
    "Sanction End",
    "Allegation Code",
    "Do Not Hire",
    "School District",
    "Charter",
    "applicable)",
    "Status (if",
    "Date (if",
)


def should_skip(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if re.match(r"^\d+\s+of\s+\d+$", s):
        return True
    if re.match(r"^FY\s*\d", s):
        return True
    for ph in SKIP_PHRASES:
        if ph in s:
            return True
    return False


def parse_simple(text: str, source: str, period: str) -> list[dict]:
    """Parse 4-column reports (any ordering of Sanction & Date)."""
    records = []
    for raw in text.splitlines():
        if should_skip(raw):
            continue
        parts = re.split(r" {2,}", raw.strip())
        # Drop empty trailing/leading cells
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) < 4:
            continue
        # Find date and sanction among the cells
        date_idx = None
        sanc_idx = None
        for i, p in enumerate(parts):
            if date_idx is None and is_date(p):
                date_idx = i
            if sanc_idx is None and is_sanction(p):
                sanc_idx = i
        if date_idx is None or sanc_idx is None:
            continue
        # The remaining two cells should be last and first name
        name_cells = [
            (i, p) for i, p in enumerate(parts) if i not in (date_idx, sanc_idx)
        ]
        if len(name_cells) < 2:
            continue
        # Take the first two cells (in PDF order) as Last, First
        last = name_cells[0][1]
        first = name_cells[1][1]
        date_raw = parts[date_idx]
        sanc_raw = parts[sanc_idx]
        records.append(
            {
                "last_name": normalize_name(last),
                "first_name": normalize_name(first),
                "sanction_raw": sanc_raw,
                "sanction": canon_sanction(sanc_raw),
                "date": normalize_date(date_raw),
                "date_raw": date_raw,
                "source": source,
                "period": period,
            }
        )
    return records


def cells_with_positions(line: str) -> list[tuple[int, str]]:
    """Return [(start_col, text), ...] for cells separated by >=2 spaces."""
    cells = []
    for m in re.finditer(r"\S+(?:[ ]\S+)*(?:[ ](?!\s)\S+)*", line):
        # The above is fragile; use a simpler approach below.
        pass
    cells = []
    # Walk line manually: tokenize by runs of 2+ spaces
    i = 0
    n = len(line)
    while i < n:
        if line[i] == " ":
            i += 1
            continue
        # start of a cell
        start = i
        # Cell continues until we hit 2+ spaces
        j = i
        while j < n:
            if line[j] == " ":
                # Look ahead: is this a single space inside a cell or 2+ spaces between cells?
                k = j
                while k < n and line[k] == " ":
                    k += 1
                gap = k - j
                if gap >= 2:
                    break
                else:
                    j = k
                    continue
            j += 1
        cells.append((start, line[start:j].rstrip()))
        i = j
    return cells


def parse_rich(text: str, source: str, period: str) -> list[dict]:
    """Parse the FY25-26 rich-column reports.

    Cell wraps appear on the line ABOVE the anchor row at the same horizontal
    position as the column they belong to. So for each anchor row (one that
    contains a date), we attach any pre-row continuation cells to the column
    whose anchor cell starts closest to (and not after) the continuation.
    """
    column_order = [
        "Last Name",
        "First Name",
        "Sanction",
        "Sanction Begin",
        "Sanction End",
        "Allegation",
        "District",
        "Do Not Hire",
    ]
    records: list[dict] = []
    pending_continuations: list[str] = []  # raw lines for token-level merging

    def is_anchor(cells: list[tuple[int, str]]) -> bool:
        return any(is_date(c) for _, c in cells)

    def column_for_anchor_cell(cell_text: str, position: int, anchor_idx_in_row: int) -> str:
        # Identify which logical column a cell in the anchor row belongs to,
        # using content + order.
        # Anchor row in canonical order: Last, First, Sanction, BeginDate,
        # [EndDate-or-N/A], Allegation, District, [DoNotHire].
        return column_order[anchor_idx_in_row] if anchor_idx_in_row < len(column_order) else ""

    def classify_anchor(cells: list[tuple[int, str]]) -> dict[str, tuple[int, str]]:
        """Return a {column_name: (start_pos, text)} map for an anchor row."""
        # Find sanction, begin_date, end_date_or_na, allegation, district, dnh
        # by scanning. Last/First are everything before the sanction.
        out: dict[str, tuple[int, str]] = {}
        # Locate sanction (first cell matching SANCTION_CANON)
        sanc_pos = next((i for i, (_, t) in enumerate(cells) if is_sanction(t)), None)
        if sanc_pos is None:
            return {}
        # Last + first = cells before sanction
        if sanc_pos >= 1:
            out["Last Name"] = cells[0]
        if sanc_pos >= 2:
            # First name may itself have been split if it contains 2 spaces (rare).
            # Use cells[1] as first; merge any additional pre-sanction cells back into first.
            first_parts = [cells[k] for k in range(1, sanc_pos)]
            out["First Name"] = (first_parts[0][0], " ".join(c for _, c in first_parts))
        out["Sanction"] = cells[sanc_pos]
        # Post-sanction
        idx = sanc_pos + 1
        # BeginDate
        while idx < len(cells) and not is_date(cells[idx][1]):
            idx += 1
        if idx < len(cells):
            out["Sanction Begin"] = cells[idx]
            idx += 1
        # EndDate or N/A
        if idx < len(cells):
            pos, t = cells[idx]
            if is_date(t) or t.upper() in ("N/A", "NA"):
                out["Sanction End"] = cells[idx]
                idx += 1
        # Allegation: by column order, this is always the cell right after
        # the optional end date. Accept any token. (Earlier versions tried to
        # match against ALLEG_RE; that misses new codes like "TV".)
        if idx < len(cells):
            out["Allegation"] = cells[idx]
            idx += 1
        # District
        if idx < len(cells):
            out["District"] = cells[idx]
            idx += 1
        # Do Not Hire
        if idx < len(cells):
            out["Do Not Hire"] = cells[idx]
            idx += 1
        return out

    def merge_continuations_into(
        anchor: dict[str, tuple[int, str]],
        continuation_lines: list[str],
    ):
        """For each continuation line, tokenize at SINGLE whitespace, then
        assign each token to the anchor column whose start position is the
        largest value <= the token's position. Append tokens (in order) to
        the resolved column's anchor text.
        """
        if not continuation_lines:
            return
        col_items = sorted([(col, anchor[col][0]) for col in anchor], key=lambda kv: kv[1])
        # For each continuation, build per-column lists of words, in token order.
        for raw in continuation_lines:
            per_col: dict[str, list[str]] = {col: [] for col, _ in col_items}
            for m in re.finditer(r"\S+", raw):
                pos = m.start()
                word = m.group()
                # Find the column with the greatest start <= pos.
                chosen = None
                for col, start in col_items:
                    if start <= pos:
                        chosen = col
                    else:
                        break
                if chosen is None:
                    chosen = col_items[0][0]
                per_col[chosen].append(word)
            # Prepend continuation words to each anchor column.
            for col, words in per_col.items():
                if not words:
                    continue
                cur_pos, cur_text = anchor[col]
                joined = " ".join(words)
                merged = (joined + " " + cur_text).strip() if cur_text else joined
                anchor[col] = (cur_pos, merged)

    def flush_record(anchor_cells: list[tuple[int, str]]):
        classified = classify_anchor(anchor_cells)
        if not classified or "Sanction Begin" not in classified or "Last Name" not in classified:
            pending_continuations.clear()
            return
        merge_continuations_into(classified, pending_continuations)
        pending_continuations.clear()
        last = classified.get("Last Name", (0, ""))[1]
        first = classified.get("First Name", (0, ""))[1]
        sanc_raw = classified.get("Sanction", (0, ""))[1]
        begin_raw = classified.get("Sanction Begin", (0, ""))[1]
        end_raw = classified.get("Sanction End", (0, ""))[1]
        alleg = classified.get("Allegation", (0, ""))[1]
        district = classified.get("District", (0, ""))[1]
        dnh = classified.get("Do Not Hire", (0, ""))[1]
        records.append(
            {
                "last_name": normalize_name(last),
                "first_name": normalize_name(first),
                "sanction_raw": sanc_raw,
                "sanction": canon_sanction(sanc_raw),
                "date": normalize_date(begin_raw),
                "date_raw": begin_raw,
                "sanction_end": normalize_date(end_raw)
                if end_raw and end_raw.upper() not in ("N/A", "NA")
                else "",
                "sanction_end_raw": end_raw,
                "allegation_code": alleg,
                "district": normalize_district(district),
                "do_not_hire": dnh,
                "source": source,
                "period": period,
            }
        )

    for raw in text.splitlines():
        if should_skip(raw):
            pending_continuations.clear()
            continue
        cells = cells_with_positions(raw)
        cells = [(p, t) for p, t in cells if t.strip()]
        if not cells:
            pending_continuations.clear()
            continue
        if is_anchor(cells):
            flush_record(cells)
        else:
            pending_continuations.append(raw)
    return records


FILE_META = {
    "sanctions-issued-1-1-1993-to-12-31-2002.pdf": ("1993-01-01 to 2002-12-31", "simple"),
    "sanctions-issued-1-1-2003-to-12-31-2007.pdf": ("2003-01-01 to 2007-12-31", "simple"),
    "sanction-report1-1-08to12-31-12.pdf": ("2008-01-01 to 2012-12-31", "simple"),
    "sanction-report1-1-13to08-31-15.pdf": ("2013-01-01 to 2015-08-31", "simple"),
    "sanction-reportfy15-16.pdf": ("FY 2015-2016", "simple"),
    "sanction-reportfy2017.pdf": ("FY 2016-2017", "simple"),
    "fy-2017-18-pdf-139-kb.pdf": ("FY 2017-2018", "simple"),
    "fy-2018-2019-pdf-373-kb.pdf": ("FY 2018-2019", "simple"),
    "fy-2019-2020-1727-kb.pdf": ("FY 2019-2020", "simple"),
    "fy2020-2021_disciplinary_report_0.pdf": ("FY 2020-2021", "simple"),
    "fy2021_2022_sbec_disciplinary_report_0.pdf": ("FY 2021-2022", "simple"),
    "fy-2022-2023-sbec-disciplinary-report.pdf": ("FY 2022-2023", "simple"),
    "fy-23-24-sbec-disciplinary-report-updt.pdf": ("FY 2023-2024", "simple"),
    "fy2024-2025-sbec-disciplinary-report.pdf": ("FY 2024-2025", "simple"),
    "fy25-26-sbec-professional-disciplinary-action-report-q1.pdf": ("FY 2025-2026, Q1", "rich"),
    "fy25-26-sbec-disciplinary-action-report-q2.pdf": ("FY 2025-2026, Q2", "rich"),
}


def main() -> int:
    all_records: list[dict] = []
    summary = []
    for fname, (period, fmt) in FILE_META.items():
        path = REPORTS_DIR / fname
        if not path.exists():
            print(f"missing: {fname}", file=sys.stderr)
            continue
        text = run_pdftotext(path)
        recs = parse_simple(text, fname, period) if fmt == "simple" else parse_rich(text, fname, period)
        summary.append((fname, len(recs)))
        all_records.extend(recs)

    # District canonicalization: when "Cypress-Fairbanks ISD" and
    # "Cypress Fairbanks ISD" both exist in the dataset, prefer the
    # hyphenated form (the wrapped-PDF rendering tends to drop the hyphen).
    district_counts: dict[str, int] = {}
    for r in all_records:
        d = r.get("district") or ""
        if d and d not in ("None Identified", "N/A"):
            district_counts[d] = district_counts.get(d, 0) + 1
    canonical_district: dict[str, str] = {}
    by_normal: dict[str, list[str]] = {}
    for d in district_counts:
        norm = d.replace("-", " ").replace("  ", " ")
        by_normal.setdefault(norm, []).append(d)
    for norm, variants in by_normal.items():
        if len(variants) > 1:
            # Prefer a variant that contains a hyphen (matches the un-wrapped PDF
            # rendering); fall back to the most common variant.
            hyphenated = [v for v in variants if "-" in v]
            chosen = hyphenated[0] if hyphenated else max(variants, key=lambda v: district_counts[v])
            for v in variants:
                canonical_district[v] = chosen
    for r in all_records:
        d = r.get("district")
        if d and d in canonical_district:
            r["district"] = canonical_district[d]

    # Normalize allegation-code casing: Q1 sometimes emits "Contract",
    # Q2 emits "CONTRACT" — treat them as the same code.
    for r in all_records:
        a = r.get("allegation_code") or ""
        if a.upper() == "CONTRACT":
            r["allegation_code"] = "CONTRACT"

    # Drop exact duplicates that occasionally appear in the source PDFs
    # (e.g. the FY 2024-2025 report lists "Hernandez, Nicholas Damian"
    # twice on consecutive lines). Match on identity-relevant fields only.
    seen_keys = set()
    deduped = []
    dup_count = 0
    for r in all_records:
        key = (
            r.get("last_name", "").lower(),
            r.get("first_name", "").lower(),
            r.get("date", ""),
            r.get("sanction", ""),
            r.get("period", ""),
        )
        if key in seen_keys:
            dup_count += 1
            continue
        seen_keys.add(key)
        deduped.append(r)
    if dup_count:
        print(f"deduplicated {dup_count} source-PDF duplicate row(s)", file=sys.stderr)
    all_records = deduped

    all_records.sort(key=lambda r: (r.get("date") or "", r.get("last_name", "").lower()), reverse=True)

    OUT_PATH.write_text(json.dumps(all_records, ensure_ascii=False))
    print(f"wrote {len(all_records)} records to {OUT_PATH}")
    print("per file:")
    for fname, n in summary:
        print(f"  {n:5d}  {fname}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
