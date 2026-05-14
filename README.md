# tea-dar

Searchable, filterable view of the Texas SBEC Disciplinary Action Reports
(TEA / State Board for Educator Certification).

Live site: <https://wavecentral.github.io/tea-dar/>

## What it is

`index.html` is a single self-contained page that loads every record parsed
from the SBEC Disciplinary Action Report PDFs published by the Texas Education
Agency, with search, filters, and summary statistics. All data is embedded;
the page has no network dependencies.

## Repo layout

```
index.html             Pre-built page served by GitHub Pages.
dar-reports/           Source PDFs from TEA / SBEC.
build/
  parse.py             Parses the PDFs into records.json.
  build_html.py        Composes index.html from template.html + records.json.
  template.html        UI shell (styles + JS) for index.html.
  cross_check_nyos.py  Cross-checks against a separate NYOS staff dataset.
  records.json         Parsed records (output of parse.py).
nyos-sbec-crosscheck.txt   Output of cross_check_nyos.py.
```

## Rebuild from source

```
python3 build/parse.py        # PDFs -> build/records.json
python3 build/build_html.py   # records.json + template.html -> index.html
```

`parse.py` shells out to `pdftotext -layout` (Poppler).

## Data source

PDFs in `dar-reports/` are mirrored from the TEA website. SBEC disciplinary
action reports are public records.
