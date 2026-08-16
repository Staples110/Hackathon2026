"""
Phase 2: FinancialDocParser.

Set expectations correctly before using this
--------------------------------------------
Your spec asks for regex dictionaries "mapped to extract the granular line
items". Regex will find the *labels*. It will not reliably give you the right
*number*, and the gap between those two is where financial scraping projects
die. Four reasons, all of which this module handles explicitly rather than
pretending away:

1. IFRS mandates the statements, not the wording. "Revenue", "Turnover",
   "Revenue from contracts with customers", "Sale of merchandise" are the same
   line across four SA companies. Hence LABEL_FAMILIES below - one canonical
   concept, many surface forms.

2. Every row has multiple numbers. A typical AFS row is
   `Revenue   12   204 573   189 122` - a note reference, then current year,
   then prior year, sometimes restated. Taking "the first number" gets you the
   note number. Taking "the last" gets you the comparative. `_pick_column`
   makes this decision explicit and records which column it took.

3. Scale is declared once, in a header you are not reading. `R'000` vs
   `R million` vs `Rm` is a 1000x error and it is completely silent. This is
   the single most common bug in student financial-scraping projects.
   `detect_scale` reads it per page.

4. South African number formatting. `1 234,5` is one thousand two hundred and
   thirty four point five - space thousands separator, comma decimal.
   `float("1 234,5")` raises; `float("1,234")` in a naive cleaner silently
   yields 1234 when the document meant 1.234. `parse_za_number` handles this
   and flags the genuinely ambiguous cases instead of guessing.

The honest output contract: this returns *candidates with provenance* (page,
raw row text, chosen column, confidence), not clean facts. Anything below
confidence 0.8 needs eyes on it. For a research dataset, sample 20 extractions
per company against the source PDF before you trust the other 2000.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator

import pdfplumber

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# South African number parsing
# ---------------------------------------------------------------------------

_CURRENCY = re.compile(r"[R$€£]|ZAR|USD", re.I)
_SPACES = re.compile(r"[\s\u00a0\u2009\u202f]")

NUMBER_TOKEN = re.compile(
    r"""
    \(?                       # optional opening bracket for negatives
    -?
    \d{1,3}                   # leading group
    (?:[\s\u00a0\u2009.,]\d{3})*   # thousand groups, any separator
    (?:[.,]\d{1,4})?          # decimal tail
    \)?
    %?
    """,
    re.X,
)


@dataclass
class ParsedNumber:
    value: float | None
    negative: bool
    ambiguous: bool
    raw: str
    note: str = ""


def parse_za_number(raw: str) -> ParsedNumber:
    """
    Parse a number as printed in a South African financial statement.

    Handles: space thousands separators, comma decimals, bracket negatives,
    currency prefixes, em/en dash for nil, and percentage signs.
    """
    text = raw.strip()
    if not text:
        return ParsedNumber(None, False, False, raw, "empty")

    if text in {"-", "\u2013", "\u2014", "nil", "Nil", "\u2013\u2013"}:
        return ParsedNumber(0.0, False, False, raw, "dash means nil")

    negative = (text.startswith("(") and text.endswith(")")) or text.startswith("-")
    text = text.strip("()").lstrip("-").strip()
    text = _CURRENCY.sub("", text).replace("%", "").strip()
    text = _SPACES.sub("", text)  # space is a thousands separator in SA

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
        head, _, tail = text.rpartition(",")
        if len(tail) == 3 and head:
            # "1,234" - could be 1234 (Anglo/UK style) or 1.234 (SA style).
            # SA convention says decimal, but a 3-digit tail is the classic
            # thousands shape. Flag it rather than silently pick.
            ambiguous = True
            note = "3-digit group after comma: thousands vs decimal ambiguous"
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    # dot-only: already float-parseable

    try:
        value = float(text)
    except ValueError:
        return ParsedNumber(None, negative, True, raw, "unparseable")

    return ParsedNumber(-value if negative else value, negative, ambiguous, raw, note)



def _is_numeric_cell(cell: str) -> bool:
    """True when a cell is a number, a bracketed number, or a nil dash."""
    stripped = cell.strip()
    if stripped in {"-", "\u2013", "\u2014"}:
        return True
    core = stripped.strip("()").replace("%", "").strip()
    core = _CURRENCY.sub("", core).strip()
    return bool(core) and bool(re.fullmatch(r"[\d\s\u00a0.,]+", core))

# ---------------------------------------------------------------------------
# Scale detection
# ---------------------------------------------------------------------------

SCALE_PATTERNS: tuple[tuple[re.Pattern, float, str], ...] = (
    (re.compile(r"R\s?['’]?\s?bn|R\s?billion|ZAR\s?bn", re.I), 1e9, "billions"),
    (re.compile(r"R\s?['’]?\s?m\b|R\s?million|Rm\b|ZAR\s?m\b", re.I), 1e6, "millions"),
    (re.compile(r"R\s?['’]\s?000|R\s?thousand|R'000|ZAR\s?['’]000", re.I), 1e3, "thousands"),
    (re.compile(r"\bcents?\b", re.I), 0.01, "cents"),
)


def detect_scale(text: str) -> tuple[float, str]:
    """Return (multiplier, label). Defaults to units with an explicit warning."""
    for pattern, multiplier, label in SCALE_PATTERNS:
        if pattern.search(text):
            return multiplier, label
    return 1.0, "units (NOT DECLARED - verify manually)"


# ---------------------------------------------------------------------------
# Statement + label taxonomy
# ---------------------------------------------------------------------------


class Statement(str, Enum):
    COMPREHENSIVE_INCOME = "statement_of_comprehensive_income"
    FINANCIAL_POSITION = "statement_of_financial_position"
    CASH_FLOWS = "statement_of_cash_flows"
    CHANGES_IN_EQUITY = "statement_of_changes_in_equity"


STATEMENT_HEADERS: dict[Statement, re.Pattern] = {
    Statement.COMPREHENSIVE_INCOME: re.compile(
        r"(consolidated\s+)?(statements?\s+of\s+)?"
        r"(comprehensive\s+income|profit\s+or\s+loss|income\s+statement)",
        re.I,
    ),
    Statement.FINANCIAL_POSITION: re.compile(
        r"(consolidated\s+)?(statements?\s+of\s+)?"
        r"(financial\s+position|balance\s+sheet)",
        re.I,
    ),
    Statement.CASH_FLOWS: re.compile(
        r"(consolidated\s+)?(statements?\s+of\s+)?cash\s*flows?", re.I
    ),
    Statement.CHANGES_IN_EQUITY: re.compile(
        r"(consolidated\s+)?statements?\s+of\s+changes\s+in\s+equity", re.I
    ),
}


# Canonical concept -> the surface forms SA issuers actually print.
# Each entry: (regex, specificity). Higher specificity wins on collision, so
# "cost of sales" never matches under the looser "sales" family.
LABEL_FAMILIES: dict[str, tuple[tuple[str, int], ...]] = {
    # --- Comprehensive income -------------------------------------------
    "revenue": (
        (r"^revenue\s+from\s+contracts?\s+with\s+customers", 10),
        (r"^total\s+revenue\b", 9),
        (r"^revenue\b", 7),
        (r"^turnover\b", 7),
        (r"^sale\s+of\s+merchandise", 6),
        (r"^gross\s+revenue\b", 5),
    ),
    "cost_of_sales": (
        (r"^cost\s+of\s+sales\b", 10),
        (r"^cost\s+of\s+goods\s+sold", 10),
        (r"^cost\s+of\s+merchandise\s+sold", 9),
    ),
    "gross_profit": ((r"^gross\s+(profit|margin)\b", 10),),
    "operating_expenses": (
        (r"^total\s+operating\s+expenses", 10),
        (r"^operating\s+(expenses|costs)\b", 8),
        (r"^other\s+operating\s+expenses", 6),
    ),
    "staff_costs": (
        (r"^(staff|employee|personnel)\s+(costs?|expenses?|benefits?\s+expense)", 10),
        (r"^salaries\s+and\s+wages", 8),
        (r"^employment\s+costs?", 8),
    ),
    "operating_profit": (
        (r"^operating\s+(profit|income)\b", 9),
        (r"^(profit|earnings)\s+before\s+interest\s+and\s+tax", 9),
        (r"^\bEBIT\b", 8),
    ),
    "finance_costs": (
        (r"^finance\s+(costs?|charges?|expenses?)", 10),
        (r"^interest\s+(paid|expense)", 8),
    ),
    "profit_for_the_year": (
        (r"^profit\s+for\s+the\s+(year|period)", 10),
        (r"^net\s+(profit|income)\b", 8),
    ),
    # --- Financial position ---------------------------------------------
    "inventories": (
        (r"^inventor(y|ies)\b", 10),
        (r"^stock\s+on\s+hand", 8),
        (r"^merchandise\s+inventor", 8),
    ),
    "trade_receivables": (
        (r"^trade\s+(and\s+other\s+)?receivables", 10),
        (r"^trade\s+accounts?\s+receivable", 10),
        (r"^accounts?\s+receivable\b", 7),
        (r"^debtors\b", 6),
    ),
    "trade_payables": (
        (r"^trade\s+(and\s+other\s+)?payables", 10),
        (r"^trade\s+accounts?\s+payable", 10),
        (r"^accounts?\s+payable\b", 7),
        (r"^creditors\b", 6),
    ),
    "cash_and_equivalents": (
        (r"^cash\s+and\s+cash\s+equivalents", 10),
        (r"^cash\s+and\s+short[- ]term\s+deposits", 9),
        (r"^bank\s+balances?\s+and\s+cash", 8),
    ),
    "long_term_borrowings": (
        (r"^(non[- ]current|long[- ]term)\s+(interest[- ]bearing\s+)?borrowings", 10),
        (r"^long[- ]term\s+(debt|liabilities)\b", 8),
        (r"^interest[- ]bearing\s+(debt|liabilities)\b", 7),
    ),
    "short_term_borrowings": (
        (r"^(current|short[- ]term)\s+(portion\s+of\s+)?(interest[- ]bearing\s+)?borrowings", 10),
        (r"^current\s+portion\s+of\s+long[- ]term\s+(debt|borrowings)", 10),
        (r"^bank\s+overdrafts?\b", 7),
    ),
    "total_assets": ((r"^total\s+assets\b", 10),),
    "total_equity": ((r"^total\s+equity\b", 10),),
    # --- Cash flows -------------------------------------------------------
    "capital_expenditure": (
        (r"^(additions?\s+to|acquisition\s+of|purchase\s+of)\s+(property,?\s+plant\s+and\s+equipment|PPE)", 10),
        (r"^capital\s+expenditure", 9),
        (r"^investment\s+to\s+(maintain|expand)\s+operations", 8),
    ),
    "debt_repayments": (
        (r"^repayments?\s+of\s+(borrowings|interest[- ]bearing|long[- ]term\s+debt)", 10),
        (r"^(decrease|reduction)\s+in\s+borrowings", 8),
    ),
    "dividends_paid": (
        (r"^dividends?\s+paid\b", 10),
        (r"^distributions?\s+(paid\s+)?to\s+shareholders", 8),
    ),
    "cash_from_operations": (
        (r"^(net\s+)?cash\s+(generated|flows?)\s+from\s+operat", 10),
        (r"^cash\s+generated\s+by\s+operations", 9),
    ),
}

_COMPILED_FAMILIES: dict[str, tuple[tuple[re.Pattern, int], ...]] = {
    concept: tuple((re.compile(p, re.I), s) for p, s in variants)
    for concept, variants in LABEL_FAMILIES.items()
}


# Note-level disclosures from Section 2 of the spec.
NOTE_PATTERNS: dict[str, re.Pattern] = {
    "financial_risk_management": re.compile(
        r"financial\s+(risk\s+management|instruments)\b.{0,120}?"
        r"(foreign\s+currency|market\s+risk|currency\s+risk)",
        re.I | re.S,
    ),
    "foreign_currency_exposure": re.compile(
        r"(foreign\s+currency\s+(exposure|risk)|uncovered\s+foreign|forward\s+exchange\s+contracts?)",
        re.I,
    ),
    "borrowings_and_facilities": re.compile(
        r"(borrowings?|interest[- ]bearing\s+liabilities|credit\s+facilit(y|ies))\b"
        r".{0,200}?(maturit|repayable|facility\s+limit|covenant)",
        re.I | re.S,
    ),
    "debt_maturity_schedule": re.compile(
        r"(maturity\s+(profile|analysis|schedule)|repayable\s+(within|after)|contractual\s+maturit)",
        re.I,
    ),
    "segmental_reporting": re.compile(
        r"(segment(al)?\s+(report|information|analysis)|operating\s+segments|geographical\s+segments)",
        re.I,
    ),
    "group_structure": re.compile(
        r"(principal\s+subsidiar|interest\s+in\s+subsidiar|group\s+structure|"
        r"joint\s+ventures?\s+and\s+associates)",
        re.I,
    ),
    "covenants": re.compile(
        r"(covenant|net\s+debt\s*[:/]\s*EBITDA|interest\s+cover(age)?\s+ratio|gearing\s+ratio)",
        re.I,
    ),
}


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class LineItem:
    concept: str
    matched_label: str
    value: float | None
    scale_label: str
    scaled_value: float | None
    page_number: int
    column_index: int
    raw_row: str
    confidence: float
    warnings: list[str] = field(default_factory=list)


@dataclass
class NoteHit:
    note_key: str
    page_number: int
    excerpt: str


@dataclass
class ParseReport:
    source: Path
    page_count: int
    statement_pages: dict[Statement, list[int]]
    line_items: list[LineItem]
    note_hits: list[NoteHit]
    scale_by_page: dict[int, str]

    def best(self, concept: str) -> LineItem | None:
        candidates = [li for li in self.line_items if li.concept == concept]
        return max(candidates, key=lambda li: li.confidence) if candidates else None

    def low_confidence(self, threshold: float = 0.8) -> list[LineItem]:
        return [li for li in self.line_items if li.confidence < threshold]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class FinancialDocParser:
    """Extract statement line items and note-level disclosures from a PDF."""

    def __init__(self, path: Path, column_preference: str = "first_after_label"):
        """
        column_preference:
          "first_after_label" - first numeric column after any note reference.
                                Correct for the overwhelming majority of SA
                                AFS layouts, where current year precedes prior.
          "last"              - rightmost numeric column.
        """
        self.path = Path(path)
        self.column_preference = column_preference

    def parse(self, max_pages: int | None = None) -> ParseReport:
        statement_pages: dict[Statement, list[int]] = {s: [] for s in Statement}
        line_items: list[LineItem] = []
        note_hits: list[NoteHit] = []
        scale_by_page: dict[int, str] = {}

        with pdfplumber.open(self.path) as pdf:
            pages = pdf.pages[:max_pages] if max_pages else pdf.pages
            page_count = len(pdf.pages)

            for index, page in enumerate(pages, start=1):
                text = page.extract_text() or ""
                if not text.strip():
                    continue

                multiplier, scale_label = detect_scale(text[:1500])
                scale_by_page[index] = scale_label

                current_statement = self._identify_statement(text[:800])
                if current_statement:
                    statement_pages[current_statement].append(index)

                for key, pattern in NOTE_PATTERNS.items():
                    match = pattern.search(text)
                    if match:
                        start = max(0, match.start() - 100)
                        note_hits.append(
                            NoteHit(key, index, text[start : match.end() + 600].strip())
                        )

                line_items.extend(
                    self._extract_rows(page, text, index, multiplier, scale_label)
                )

        return ParseReport(
            source=self.path,
            page_count=page_count,
            statement_pages={k: v for k, v in statement_pages.items() if v},
            line_items=line_items,
            note_hits=note_hits,
            scale_by_page=scale_by_page,
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _identify_statement(header_text: str) -> Statement | None:
        for statement, pattern in STATEMENT_HEADERS.items():
            if pattern.search(header_text):
                return statement
        return None

    def _extract_rows(
        self, page, text: str, page_number: int, multiplier: float, scale_label: str
    ) -> Iterator[LineItem]:
        """Ruled tables first, then geometric column reconstruction."""
        rows_seen: set[str] = set()

        for table in page.extract_tables() or []:
            for row in table:
                cells = [(c or "").strip() for c in row]
                if not any(cells):
                    continue
                joined = " | ".join(cells)
                if joined in rows_seen:
                    continue
                rows_seen.add(joined)
                item = self._match_row(
                    cells[0], cells[1:], joined, page_number, multiplier, scale_label, 0.05
                )
                if item:
                    yield item

        for label, cells, raw in self._rows_from_words(page):
            if raw in rows_seen:
                continue
            rows_seen.add(raw)
            item = self._match_row(
                label, cells, raw, page_number, multiplier, scale_label, -0.05
            )
            if item:
                yield item

    # -- geometric row reconstruction -------------------------------------

    def _rows_from_words(self, page, line_tolerance: float = 2.5):
        """
        Rebuild rows from word coordinates instead of from extracted text.

        THIS IS THE PART THAT MATTERS. Most financial statement pages have no
        ruled table for pdfplumber to find, so the tempting fallback is regex
        over `extract_text()`. That fails silently and catastrophically,
        because after text extraction this row:

            Revenue    12      204 573      189 122
            (note ref) (FY24)  (FY23)

        becomes the string "Revenue 12 204 573 189 122". The space between
        columns and the space inside "204 573" are now the same character.
        Any regex that treats space as a thousands separator - which it is in
        South African formatting - will happily read that as the single number
        12,204,573,189,122. A twelve-trillion-rand revenue line, sitting in
        your dataset, looking structurally fine.

        The information needed to split it is geometric, not textual: the gap
        between columns is ~10-40pt, the gap inside a number is ~2-4pt. So
        cluster words by x-gap and never let a regex see the joined string.
        """
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        if not words:
            return

        lines: dict[float, list[dict]] = {}
        for word in words:
            key = round(word["top"] / line_tolerance) * line_tolerance
            lines.setdefault(key, []).append(word)

        for key in sorted(lines):
            row = sorted(lines[key], key=lambda w: w["x0"])
            if len(row) < 2:
                continue

            heights = [w["bottom"] - w["top"] for w in row]
            font_size = sum(heights) / len(heights)
            # A gap wider than ~0.55x the font height is a column break;
            # narrower is an intra-number thousands space.
            gap_threshold = max(4.0, font_size * 0.55)

            groups: list[list[dict]] = [[row[0]]]
            for previous, current in zip(row, row[1:]):
                if current["x0"] - previous["x1"] > gap_threshold:
                    groups.append([current])
                else:
                    groups[-1].append(current)

            cells = [" ".join(w["text"] for w in group) for group in groups]

            # The label is every leading cell that is not numeric.
            numeric_start = len(cells)
            for index, cell in enumerate(cells):
                if _is_numeric_cell(cell):
                    numeric_start = index
                    break
            if numeric_start == 0 or numeric_start == len(cells):
                continue

            label = " ".join(cells[:numeric_start]).strip()
            label = re.sub(r"[.\s]{3,}$", "", label).strip()
            if len(label) < 3:
                continue
            yield label, cells[numeric_start:], " | ".join(cells)[:300]

    def _match_row(
        self,
        label: str,
        number_cells: list[str],
        raw_row: str,
        page_number: int,
        multiplier: float,
        scale_label: str,
        confidence_adjustment: float,
    ) -> LineItem | None:
        clean_label = re.sub(r"\s+", " ", label).strip(" .:*†")
        if not clean_label:
            return None

        best_concept, best_specificity = None, 0
        for concept, variants in _COMPILED_FAMILIES.items():
            for pattern, specificity in variants:
                if pattern.search(clean_label) and specificity > best_specificity:
                    best_concept, best_specificity = concept, specificity
        if best_concept is None:
            return None

        column_index, parsed, warnings = self._pick_column(number_cells)
        if parsed is None or parsed.value is None:
            return None

        confidence = 0.5 + (best_specificity / 20.0) + confidence_adjustment
        if parsed.ambiguous:
            confidence -= 0.25
            warnings.append(parsed.note)
        if scale_label.startswith("units"):
            confidence -= 0.20
            warnings.append("scale not declared on this page")
        confidence = max(0.0, min(1.0, confidence))

        return LineItem(
            concept=best_concept,
            matched_label=clean_label,
            value=parsed.value,
            scale_label=scale_label,
            scaled_value=parsed.value * multiplier,
            page_number=page_number,
            column_index=column_index,
            raw_row=raw_row[:300],
            confidence=round(confidence, 2),
            warnings=warnings,
        )

    def _pick_column(self, cells: list[str]) -> tuple[int, ParsedNumber | None, list[str]]:
        """
        Choose which numeric column is the current-year figure.

        A note reference is a bare 1-2 digit integer with no separators and no
        decimals, sitting immediately after the label. Dropping it is the whole
        job here - keep it and every 'Revenue' in your dataset is 12.
        """
        warnings: list[str] = []
        parsed: list[tuple[int, ParsedNumber]] = []

        for index, cell in enumerate(cells):
            if not cell or not cell.strip():
                continue
            candidate = parse_za_number(cell)
            if candidate.value is None:
                continue
            looks_like_note_ref = (
                re.fullmatch(r"\d{1,2}", cell.strip()) is not None and index <= 1
            )
            if looks_like_note_ref:
                warnings.append(f"dropped column {index} as note reference ({cell.strip()})")
                continue
            parsed.append((index, candidate))

        if not parsed:
            return -1, None, warnings

        if len(parsed) == 1:
            warnings.append("only one numeric column - no comparative found")

        if self.column_preference == "last":
            index, value = parsed[-1]
        else:
            index, value = parsed[0]
        return index, value, warnings


def parse_directory(directory: Path, pattern: str = "*.pdf") -> Iterator[ParseReport]:
    for pdf_path in sorted(Path(directory).rglob(pattern)):
        try:
            yield FinancialDocParser(pdf_path).parse()
        except Exception as exc:  # noqa: BLE001
            log.error("failed to parse %s: %s", pdf_path, exc)
