#!/usr/bin/env python3
"""Compose index.html — a fully self-contained one-pager.

Embeds records.json in a compact columnar form and inlines styles + JS.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDS = Path(__file__).resolve().parent / "records.json"
TEMPLATE = Path(__file__).resolve().parent / "template.html"
OUT = ROOT / "index.html"


def main() -> int:
    records = json.loads(RECORDS.read_text())

    # Compact columnar payload: keep all fields but drop blanks.
    # Schema: keys = column order; data = list of arrays in same order.
    keys = [
        "l",  # last_name
        "f",  # first_name
        "s",  # sanction (canonical)
        "sr",  # sanction_raw
        "d",  # date (ISO yyyy-mm-dd)
        "dr",  # date_raw
        "p",  # period
        "src",  # source pdf
        "e",  # sanction_end (ISO)
        "er",  # sanction_end_raw
        "a",  # allegation_code
        "ds",  # district
        "dh",  # do_not_hire
    ]
    map_field = {
        "l": "last_name",
        "f": "first_name",
        "s": "sanction",
        "sr": "sanction_raw",
        "d": "date",
        "dr": "date_raw",
        "p": "period",
        "src": "source",
        "e": "sanction_end",
        "er": "sanction_end_raw",
        "a": "allegation_code",
        "ds": "district",
        "dh": "do_not_hire",
    }
    rows = []
    for r in records:
        row = [r.get(map_field[k], "") or "" for k in keys]
        rows.append(row)

    payload = {"keys": keys, "rows": rows}
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    template = TEMPLATE.read_text()
    out_html = template.replace("/*__DATA_PAYLOAD__*/", payload_json)
    OUT.write_text(out_html)
    size_mb = OUT.stat().st_size / (1024 * 1024)
    print(f"wrote {OUT}  ({size_mb:.2f} MiB; {len(records):,} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
