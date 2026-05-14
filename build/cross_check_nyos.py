#!/usr/bin/env python3
"""Cross-check NYOS staff (~/dev/nyos-former-staff/nyos_attrition.db) against
the parsed SBEC disciplinary action records (build/records.json).

Matching rule:
    A NYOS employee matches an SBEC record when the SBEC last name appears
    as one of the employee's name tokens AND at least one SBEC first-name
    token also appears in the employee's name tokens. Single-letter tokens
    are ignored. This is intentionally conservative: requiring two tokens
    of overlap (a surname plus a given name) avoids false positives from
    common standalone names.

Outputs a Markdown table of matches with NYOS name, status, latest title,
and the matched SBEC sanction details.
"""
import json
import os
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "build" / "records.json"
NYOS_DB = Path.home() / "dev" / "nyos-former-staff" / "nyos_attrition.db"


NAME_PARTICLES = {
    "de", "del", "dela", "la", "las", "los", "von", "van", "der",
    "di", "da", "dos", "das", "san", "santa", "le", "du", "den",
    "el", "al", "bin", "ibn", "mac", "mc", "jr", "sr",
}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "vi"}


def tokenize(s: str) -> set[str]:
    """Significant name tokens — lowercased, stripped of punctuation, with
    common particles and suffixes filtered out. Single-letter initials are
    dropped because they don't reliably identify a person."""
    s = (s or "").lower()
    s = re.sub(r"[\-,_/.()]+", " ", s)
    toks = set()
    for t in s.split():
        clean = re.sub(r"[^a-z']", "", t)
        if len(clean) <= 1:
            continue
        if clean in NAME_PARTICLES or clean in SUFFIXES:
            continue
        toks.add(clean)
    return toks


def employee_tokens(emp: dict) -> set[str]:
    """Union of significant tokens from canonical_name + name_variants."""
    toks = set()
    toks.update(tokenize(emp.get("canonical_name") or ""))
    toks.update(tokenize(emp.get("csv_2023_name") or ""))
    raw = emp.get("name_variants") or ""
    try:
        for v in json.loads(raw):
            toks.update(tokenize(v))
    except Exception:
        pass
    return toks


def sbec_tokens(rec: dict) -> tuple[set[str], set[str]]:
    return tokenize(rec.get("first_name") or ""), tokenize(rec.get("last_name") or "")


def primary_given_name(emp: dict) -> str:
    """A best-guess first-given-name token, taken from csv_2023_name if
    present (it's already in "First Last" form), otherwise from the first
    token of canonical_name."""
    csv = emp.get("csv_2023_name") or ""
    if csv:
        toks = tokenize(csv)
        if toks:
            # csv_2023_name is "First Last" — first whitespace token is the
            # given name.
            head = csv.split()[0].lower().strip(",.-")
            if head and head not in NAME_PARTICLES:
                return head
    canon = emp.get("canonical_name") or ""
    for word in canon.split():
        clean = re.sub(r"[^a-z]", "", word.lower())
        if len(clean) > 1 and clean not in NAME_PARTICLES:
            return clean
    return ""


def sbec_given_first(rec: dict) -> str:
    s = (rec.get("first_name") or "").lower()
    for word in re.split(r"\s+", s):
        clean = re.sub(r"[^a-z]", "", word)
        if len(clean) > 1 and clean not in NAME_PARTICLES:
            return clean
    return ""


def load_employees() -> list[dict]:
    conn = sqlite3.connect(NYOS_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT canonical_name, csv_2023_name, name_variants, status, "
        "latest_title, departure_year, role_category, salary "
        "FROM master_employees"
    )
    out = [dict(r) for r in cur]
    conn.close()
    return out


def main() -> int:
    records = json.loads(RECORDS.read_text())
    employees = load_employees()
    print(f"Loaded {len(records):,} SBEC records and {len(employees)} NYOS employees.\n")

    matches: list[tuple[str, dict, dict]] = []
    for emp in employees:
        e_toks = employee_tokens(emp)
        if len(e_toks) < 2:
            continue
        emp_first = primary_given_name(emp)
        for rec in records:
            first_toks, last_toks = sbec_tokens(rec)
            if not last_toks or not first_toks:
                continue
            last_hits = last_toks & e_toks
            if not last_hits:
                continue
            first_hits = (first_toks & e_toks) - last_hits
            if not first_hits:
                continue
            sbec_first = sbec_given_first(rec)
            # High confidence: NYOS primary given name matches SBEC's primary
            # given name token, AND we already matched the surname.
            if emp_first and sbec_first and emp_first == sbec_first:
                conf = "HIGH"
            else:
                conf = "Possible"
            matches.append((conf, emp, rec))

    if not matches:
        print("No matches found.")
        return 0

    # Group records by (confidence, employee)
    high_matches = [m for m in matches if m[0] == "HIGH"]
    possible_matches = [m for m in matches if m[0] != "HIGH"]

    def render_group(label: str, items: list[tuple[str, dict, dict]]) -> None:
        if not items:
            print(f"\n### {label}: none\n")
            return
        by_emp: dict[str, list[dict]] = {}
        emp_info: dict[str, dict] = {}
        for _, emp, rec in items:
            key = emp["canonical_name"]
            by_emp.setdefault(key, []).append(rec)
            emp_info[key] = emp
        rows = []
        for name, recs in sorted(by_emp.items()):
            emp = emp_info[name]
            for rec in sorted(recs, key=lambda r: r.get("date", "")):
                rows.append(
                    {
                        "NYOS name": name,
                        "Status": emp.get("status") or "",
                        "Latest title": emp.get("latest_title") or "",
                        "Dep. yr": str(emp.get("departure_year") or ""),
                        "SBEC name": f"{rec['last_name']}, {rec['first_name']}",
                        "Sanction": rec.get("sanction") or "",
                        "Date": rec.get("date") or "",
                        "District": rec.get("district") or "",
                        "Period": rec.get("period") or "",
                    }
                )
        cols = list(rows[0].keys())
        widths = {c: max(len(c), max(len(r[c]) for r in rows)) for c in cols}
        print(f"\n### {label} ({len(by_emp)} employee(s), {len(items)} record(s))\n")
        print(" | ".join(c.ljust(widths[c]) for c in cols))
        print("-+-".join("-" * widths[c] for c in cols))
        for r in rows:
            print(" | ".join(r[c].ljust(widths[c]) for c in cols))

    # Identify CLEAN matches: HIGH-confidence employees where there's exactly
    # one matched SBEC record (i.e., not a swarm of Jose-Garcia coincidences).
    by_emp_high: dict[str, list[dict]] = {}
    emp_info_high: dict[str, dict] = {}
    for _, emp, rec in high_matches:
        key = emp["canonical_name"]
        by_emp_high.setdefault(key, []).append(rec)
        emp_info_high[key] = emp
    single_clean = {k: v for k, v in by_emp_high.items() if len(v) == 1}

    print(f"## Summary: {len(matches)} candidate match(es) — {len(high_matches)} HIGH confidence ({len(single_clean)} of which are a single clean match), {len(possible_matches)} weak.")
    if single_clean:
        print("\n### Most likely true matches (1 NYOS employee → 1 SBEC record, names agree on both given & surname)\n")
        rows = []
        for name in sorted(single_clean):
            emp = emp_info_high[name]
            rec = single_clean[name][0]
            rows.append({
                "NYOS name": name,
                "Status": emp.get("status") or "",
                "Title": emp.get("latest_title") or "",
                "Dep yr": str(emp.get("departure_year") or ""),
                "SBEC name": f"{rec['last_name']}, {rec['first_name']}",
                "Sanction": rec.get("sanction") or "",
                "Date": rec.get("date") or "",
                "District": rec.get("district") or "—",
                "Period": rec.get("period") or "",
            })
        cols = list(rows[0].keys())
        widths = {c: max(len(c), max(len(r[c]) for r in rows)) for c in cols}
        print(" | ".join(c.ljust(widths[c]) for c in cols))
        print("-+-".join("-" * widths[c] for c in cols))
        for r in rows:
            print(" | ".join(r[c].ljust(widths[c]) for c in cols))

    render_group("All HIGH confidence matches (incl. multi-record swarms — likely common-name noise for those)", high_matches)
    render_group("Weaker possible matches (only middle/given token matches — usually coincidences)", possible_matches)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
