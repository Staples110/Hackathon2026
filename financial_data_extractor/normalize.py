"""
Value normalisation.

Everything here is deterministic and side-effect free: the same input string
always produces the same output. That is the property that makes the
"zero-hallucination" requirement actually checkable — you can re-run the
extractor on the same corpus and diff the CSVs.

Design rule enforced throughout: a value is either PARSED or FLAGGED. There is
no third path where the code guesses. `parsed_value` is None whenever the raw
string could not be resolved unambiguously, and `parse_note` says why.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Currency detection
# --------------------------------------------------------------------------

CURRENCY_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bZAR\b|\bR\s?(?=[\d'’])|\brand\b", re.I), "ZAR"),
    (re.compile(r"\bUSD\b|\bUS\$|\$(?=[\d\s])|\bdollars?\b", re.I), "USD"),
    (re.compile(r"\bEUR\b|€|\beuros?\b", re.I), "EUR"),
    (re.compile(r"\bGBP\b|£|\bpounds? sterling\b", re.I), "GBP"),
    (re.compile(r"\bAUD\b|\bA\$", re.I), "AUD"),
    (re.compile(r"\bJPY\b|¥", re.I), "JPY"),
    (re.compile(r"\bCNY\b|\bRMB\b", re.I), "CNY"),
    (re.compile(r"\bNGN\b|₦", re.I), "NGN"),
    (re.compile(r"\bKES\b|\bKSh\b", re.I), "KES"),
    (re.compile(r"\bINR\b|₹", re.I), "INR"),
)


def detect_currency(text: str) -> str | None:
    """Return an ISO code, or None. None means 'not stated', never a default."""
    if not text:
        return None
    for pattern, code in CURRENCY_PATTERNS:
        if pattern.search(text):
            return code
    return None


# --------------------------------------------------------------------------
# Scale detection
# --------------------------------------------------------------------------
# Ordered most-specific first. "R'000 million" does not occur, but "US$ million"
# and "R million" both do, so billion must be tested before million and million
# before thousand or the shorter token wins on a substring.

SCALE_PATTERNS: tuple[tuple[re.Pattern, str, float], ...] = (
    (re.compile(r"\b(?:in\s+)?(?:trillions?|tn)\b", re.I), "trillions", 1e12),
    (re.compile(r"\b(?:in\s+)?(?:billions?|bn)\b|['’]?000\s*000\s*000", re.I), "billions", 1e9),
    (re.compile(r"\b(?:in\s+)?millions?\b|\bm\b(?=\s*\)|\s*$)|['’]000\s*000|\bmn\b", re.I), "millions", 1e6),
    (re.compile(r"['’]000\b|\bthousands?\b|\bk\b(?=\s*\)|\s*$)", re.I), "thousands", 1e3),
    (re.compile(r"\bcents?\b", re.I), "cents", 0.01),
    (re.compile(r"\bunits?\b|\bactual\b|\bfull\s+amounts?\b", re.I), "units", 1.0),
)


@dataclass(frozen=True)
class ScaleInfo:
    label: str | None
    multiplier: float | None
    stated: bool

    @property
    def as_cell(self) -> str:
        return self.label if self.label else "N/A"


def detect_scale(text: str) -> ScaleInfo:
    """
    Detect a declared magnitude scale.

    Returns stated=False when nothing is found. The caller MUST NOT then assume
    units: an undeclared scale is the single most damaging silent error in
    financial extraction, because R'000 vs R million is a 1000x difference that
    passes every type check. Undeclared means the normalised column stays empty.
    """
    if not text:
        return ScaleInfo(None, None, False)
    for pattern, label, mult in SCALE_PATTERNS:
        if pattern.search(text):
            return ScaleInfo(label, mult, True)
    return ScaleInfo(None, None, False)


# --------------------------------------------------------------------------
# Number parsing
# --------------------------------------------------------------------------

_SPACE_CHARS = re.compile(r"[\s\u00a0\u2009\u202f\u2007]")
_CURRENCY_SYMBOLS = re.compile(r"[R$€£¥₦₹]|ZAR|USD|EUR|GBP|AUD|JPY|CNY|US\$|A\$", re.I)
# Footnote markers the spec asks us to strip: superscripts, daggers, asterisks,
# bracketed single letters, and trailing digit-in-parens like "(1)".
_FOOTNOTE = re.compile(r"[*†‡§¶^]+|\((?:[a-z]|[ivx]+)\)$|(?<=\d)\s*\(\d\)$", re.I)
_NIL_TOKENS = frozenset({"-", "–", "—", "‐", "nil", "none", "n/a", "na", "", "—-"})


@dataclass
class ParsedValue:
    raw: str
    value: float | None
    is_negative: bool = False
    is_percentage: bool = False
    ambiguous: bool = False
    parse_note: str = ""
    stripped_markers: list[str] = field(default_factory=list)


def parse_number(raw: str) -> ParsedValue:
    """
    Parse a financial figure as printed, across the separator conventions that
    actually appear in the same corpus.

    Handled:
      1 234 567     space thousands separator (SA, FR, ZA statements)
      1,234,567     comma thousands separator (US, UK)
      1.234.567     dot thousands separator (DE, IT, ES, BR)
      1 234,56      comma decimal
      1,234.56      dot decimal
      (2 145)       accounting negative
      -2145         sign negative
      12.5%         percentage
      –             en-dash meaning nil
      1 234*        footnote marker

    NOT handled by guessing: "1,234" in a corpus that mixes conventions is
    genuinely ambiguous — 1234 or 1.234. It is returned with ambiguous=True and
    a note, not silently resolved. Every ambiguous row lands in the review
    queue rather than in your dataset.
    """
    if raw is None:
        return ParsedValue("", None, parse_note="null input")

    original = str(raw)
    text = unicodedata.normalize("NFKC", original).strip()

    if text.lower() in _NIL_TOKENS:
        return ParsedValue(original, None, parse_note="nil/blank token — not zero")

    stripped: list[str] = []
    footnotes = _FOOTNOTE.findall(text)
    if footnotes:
        stripped.extend(str(f) for f in footnotes if f)
        text = _FOOTNOTE.sub("", text).strip()

    is_pct = "%" in text
    if is_pct:
        text = text.replace("%", "").strip()

    currency_hits = _CURRENCY_SYMBOLS.findall(text)
    if currency_hits:
        stripped.extend(currency_hits)
        text = _CURRENCY_SYMBOLS.sub("", text).strip()

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    if text.startswith("-") or text.startswith("−") or text.startswith("–"):
        negative = True
        text = text[1:].strip()
    if text.endswith("-"):  # trailing-minus convention in some exports
        negative = True
        text = text[:-1].strip()

    text = _SPACE_CHARS.sub("", text)

    if not text:
        return ParsedValue(original, None, parse_note="no numeric content after cleaning",
                           stripped_markers=stripped)
    if not re.fullmatch(r"[\d.,]+", text):
        return ParsedValue(original, None, parse_note=f"non-numeric residue: {text!r}",
                           stripped_markers=stripped)

    ambiguous = False
    note = ""
    has_comma, has_dot = "," in text, "." in text

    if has_comma and has_dot:
        # Whichever appears last is the decimal separator.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        parts = text.split(",")
        tail = parts[-1]
        if len(parts) > 2:
            text = text.replace(",", "")          # 1,234,567 — unambiguous
        elif len(tail) == 3 and len(parts[0]) <= 3:
            ambiguous = True
            note = ("single comma with 3-digit tail: thousands separator or "
                    "decimal comma cannot be determined from this cell alone")
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")          # 1,5 -> 1.5
    elif has_dot:
        parts = text.split(".")
        if len(parts) > 2:
            text = text.replace(".", "")           # 1.234.567 — unambiguous
        elif len(parts[-1]) == 3 and len(parts[0]) <= 3:
            ambiguous = True
            note = ("single dot with 3-digit tail: thousands separator or "
                    "decimal point cannot be determined from this cell alone")
            text = text.replace(".", "")

    try:
        value = float(text)
    except ValueError:
        return ParsedValue(original, None, parse_note=f"unparseable: {text!r}",
                           stripped_markers=stripped)

    return ParsedValue(
        raw=original,
        value=-value if negative else value,
        is_negative=negative,
        is_percentage=is_pct,
        ambiguous=ambiguous,
        parse_note=note,
        stripped_markers=stripped,
    )


def looks_numeric(cell: str) -> bool:
    """Cheap gate used to decide whether a cell is a figure or a label."""
    if cell is None:
        return False
    t = unicodedata.normalize("NFKC", str(cell)).strip()
    if t.lower() in _NIL_TOKENS:
        return True
    t = _FOOTNOTE.sub("", t)
    t = _CURRENCY_SYMBOLS.sub("", t).replace("%", "")
    t = t.strip().strip("()").lstrip("-−–").strip()
    t = _SPACE_CHARS.sub("", t)
    return bool(t) and bool(re.fullmatch(r"[\d.,]+", t))


# --------------------------------------------------------------------------
# Reporting period detection
# --------------------------------------------------------------------------

PERIOD_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\b(?P<p>Q[1-4])\s*(?:FY)?\s*(?P<y>20\d{2})\b", re.I),
    re.compile(r"\b(?P<p>H[12])\s*(?:FY)?\s*(?P<y>20\d{2})\b", re.I),
    re.compile(r"\bFY\s*(?P<y>20\d{2}|\d{2})\b", re.I),
    re.compile(r"(?:year|period|six months|half[- ]year)\s+end(?:ed|ing)\s+"
               r"\d{1,2}\s+\w+\s+(?P<y>20\d{2})", re.I),
    re.compile(r"\b(?:as at|as of)\s+\d{1,2}\s+\w+\s+(?P<y>20\d{2})\b", re.I),
)


def detect_period(text: str) -> str | None:
    """Return a reporting-period label verbatim-ish, or None. Never invents one."""
    if not text:
        return None
    for pattern in PERIOD_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        groups = m.groupdict()
        year = groups.get("y")
        if year and len(year) == 2:
            year = f"20{year}"
        prefix = groups.get("p")
        return f"{prefix.upper()} {year}" if prefix else f"FY{year}"
    return None
