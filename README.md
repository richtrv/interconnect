# The Interconnect Premium

A two-page static site: a write-up (`index.html`, the landing page) and a live monitor
(`monitor.html`). The write-up carries a print stylesheet and doubles as the PDF:
File, Print.

Every constant and data point on the site is in `methodology.csv` with a source or
derivation. The pages load exactly one stylesheet (`site.css`, uPlot CSS vendored at the
top) and one script (`app.js`, uPlot vendored at the top; site logic below the marker).

## Data

`scripts/pull_premium.py` pulls Vast.ai H100 SXM listings daily under a fixed, published
filter spec and writes one aggregate row per UTC date to `data/premium.json`; a rerun
replaces that date's row. Methodology v2 collapses qualifying offers to one observation
per unique machine and stores aggregate filter provenance, never individual listings.
Days below the stated minimum machine counts are stored but marked invalid and never plotted.
`.github/workflows/daily.yml` runs it on a daily cron; no secrets required.

## Publishing

GitHub Pages from the repository root. The OG image and print chart fallbacks in
`assets/` are rendered from `scripts/og.html` and `scripts/og-c.html` with headless
Chrome. Exhibit A and the OG image are 1200x630; Exhibit B is captured at 1200x510.
Analytics: create a GoatCounter account and uncomment the tag in both HTML heads.

## Validation

Run `python -m unittest discover -s tests` and `python scripts/validate.py` before
publishing. The checks cover collector fixtures, the premium-data contract, copy rules,
published arithmetic, and the static-site size budget.
