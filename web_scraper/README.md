# JSE report acquisition & extraction pipeline

Two-phase pipeline for JSE-listed company reports, 2023–present.

```
pip install -r requirements.txt
python -m playwright install chromium

python -m jse_reports.main download --only SHP
python -m jse_reports.main extract --ticker SHP --out shp.json
```

---

## Read this before you run it

Four claims in the original spec do not survive contact with reality. Each is
handled explicitly in the code rather than silently failing.

### 1. There is no reliable automatic path from ticker → IR page

There is no South African EDGAR. The JSE publishes no documented public
developer API; market data and issuer information sit behind the Client Portal
and commercial feeds. Search-engine discovery returns PR agencies, Wikipedia,
and companies that share an abbreviation with your ticker — and the pipeline
then files 40 documents belonging to the wrong entity under your ticker,
without an error.

`discovery.py` runs a ranked cascade where every rung reports its own
confidence, and the search rung is **deliberately not implemented as an
unattended path**. The intended workflow is: seed `ir_overrides.json` by hand,
once, per ticker. Twenty tickers is an afternoon. It is not the elegant answer;
it is the one that produces a dataset you can defend.

### 2. Regex finds labels, not numbers

The spec asks for "keyword dictionaries and regex patterns that map directly
to" specific line items. Regex will find `Revenue`. It will not give you the
right figure, for four reasons the code addresses individually:

| Problem | Where handled |
|---|---|
| IFRS mandates statements, not wording — "Revenue" / "Turnover" / "Revenue from contracts with customers" are one concept | `LABEL_FAMILIES`, with per-variant specificity so `cost of sales` never matches under `sales` |
| Every row has a note reference *and* a comparative — three numbers, one is wanted | `_pick_column` |
| Scale (`R'000` vs `Rm`) is declared in a header you aren't reading; getting it wrong is a silent 1000× error | `detect_scale`, per page |
| SA number format: `1 234,5` is 1234.5; naive cleaning turns `1,234` into 1234 when the document meant 1.234 | `parse_za_number`, which flags the genuinely ambiguous cases instead of guessing |

### 3. `extract_text()` + regex destroys the data — this one is worth your time

The single most damaging bug in this class of project. Most statement pages
have no ruled table, so the obvious fallback is regex over extracted text.
This row:

```
Revenue    12      204 573      189 122
          note      FY24         FY23
```

extracts to the string `Revenue 12 204 573 189 122`. The gap *between columns*
and the space *inside* `204 573` are now the same character. Since space is a
thousands separator in SA formatting, any regex reads that as one number:
**12,204,573,189,122**. A twelve-trillion-rand revenue line that looks
structurally fine and passes every type check you have.

I hit this while testing the parser on a synthetic statement — the first run
returned exactly that number. The information needed to split the row is
geometric, not textual: inter-column gaps are 10–40pt, intra-number gaps are
2–4pt. `_rows_from_words` clusters `page.extract_words()` by x-coordinate and
never lets a regex see the joined string.

**Generalise this.** Any time you flatten a 2D layout to 1D text and then try
to recover the structure with pattern matching, you are guessing at information
you threw away one step earlier. Keep the coordinates.

### 4. User-Agent rotation makes things worse, not better

Rotating UAs from one IP with one TLS fingerprint is strictly worse than a
consistent identity. Bot detection fingerprints the TLS handshake (JA3/JA4);
a Chrome UA on an httpx handshake is a *louder* signal than any UA you'd pick.
With Playwright, a mismatched UA breaks `navigator.userAgentData` consistency
checks outright. `Settings.user_agent()` therefore returns one honest,
identifying UA. The fix for 403s is slower crawling, not disguise.

The spec also omitted the check that matters more: IR portals return HTTP 200
with an HTML login wall while the URL still ends in `.pdf`. Without
`_validate_payload`'s magic-byte check you get a directory of correctly-named
files that are 4 KB of HTML, and you find out in Phase 2 when every parse
throws.

---

## SENS

Verified August 2026: no public JSE developer API. Real-time SENS runs over the
commercial Regulatory News Gateway; end-of-day SENS is an FTP product off the
Information Dissemination Portal, licensed. ShareData / Moneyweb / ProfileData
archives require accounts and generally prohibit bulk scraping.

Publicly reachable: the per-instrument list at `jse.co.za/notes/sens/{id}`
(JS-rendered, limited retention), `senspdf.jse.co.za` cloudlinks referenced
*inside* announcement bodies, and each company's own SENS page — which is what
`harvest.py` already crawls and is the best per-ticker public source.

Practical consequence: you get good per-company coverage and **incomplete
market-wide history**. If you're doing an event study, that gap is
survivorship bias you cannot see or correct for. Use a licensed feed, or state
the limitation in your methodology.

---

## Fiscal year ends

`build_filename` writes the year the *document* claims. SA fiscal year ends are
scattered — Shoprite and Sasol are June, Naspers is March, Standard Bank is
December. A Shoprite document labelled 2024 covers July 2023–June 2024. Join
that against calendar-quarter macro data without `config.FISCAL_YEAR_END` and
every observation is misaligned by up to six months, silently. Verify each
entry in that table against the company's own AFS; do not trust my seeds.

---

## Layout

```
jse_reports/
  taxonomy.py   DocType, scored classification rules, year extraction
  config.py     Settings, FISCAL_YEAR_END, IR overrides
  discovery.py  ticker -> IR page, ranked cascade with confidence
  harvest.py    Playwright crawl: accordions, year tabs, iframes
  download.py   robots.txt, rate limiting, retries, payload validation
  sens.py       SENS sources and their limits
  storage.py    naming convention, SQLite manifest, dual dedupe
  parser.py     FinancialDocParser
  main.py       CLI
```

Classification is **scored, not ordered**. First-match-wins breaks immediately
because SA report titles are nested: "Integrated Annual Report" contains
"Annual Report"; "Interim Results Presentation" contains both "Interim Results"
and "Presentation". Each `DocType` has anchors, boosts, and vetoes; vetoes
encode the nesting. The classifier returns the runner-up too, and anything
within 3 points is flagged `ambiguous` in the manifest for review.

Dedupe runs on **both** source URL and content hash — the same PDF is routinely
served from the IR site, a SENS cloudlink, and a CDN mirror under three URLs.

---

## Validating output — do this, it is not optional

The parser returns **candidates with provenance**, not facts. Every `LineItem`
carries page number, chosen column, raw row text, confidence, and warnings.

1. `report.low_confidence()` — everything under 0.8 needs eyes.
2. Check `scale_by_page` for `units (NOT DECLARED)`. That means the page had no
   scale header and the numbers are unscaled.
3. Cross-foot: `revenue - cost_of_sales` should equal `gross_profit`. Assets
   should equal equity plus liabilities. These identities are free tests and
   they catch column-selection errors instantly.
4. Sample 20 extractions per company against the source PDF before trusting the
   other 2000.

Point 3 is the highest-value thing you can add next. Accounting identities are
a validation layer no amount of regex tuning gives you.

---

## Legal

Check each site's `robots.txt` and terms before crawling at volume;
`respect_robots` is on by default and should stay on. Some IR portals prohibit
automated access outright. The documents are public; the *access method* is
what's governed. If this feeds academic work, cite the source and the retrieval
date — the JSE public SENS retention window means a URL that resolves today may
not in a year.
