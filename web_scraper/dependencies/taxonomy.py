"""
Document taxonomy and classification rules.

Design note
-----------
The naive approach is an ordered list of substring checks, first match wins.
That breaks immediately on South African report titles because the categories
are *nested*, not disjoint:

    "Integrated Annual Report 2024"            -> contains "Annual Report"
    "Interim Results Presentation H1 2025"     -> contains both "Interim Results"
                                                  and "Presentation"
    "Annual Financial Statements 2024"         -> contains "Annual"
    "Audited summarised results for the year"  -> contains neither "AFS" nor "IAR"

So classification is scored, not ordered. Each DocType has:
  - anchors : at least one must match, or the type scores zero
  - boosts  : additional evidence, each adds weight
  - vetoes  : if any matches, the type is disqualified outright

Vetoes are what encode the nesting. INTEGRATED_ANNUAL_REPORT is vetoed by
/interim|half.year|six months/ so an interim report can never win it, and
ANNUAL_FINANCIAL_STATEMENTS is vetoed by /presentation|slide|booklet/.

The classifier returns the highest-scoring type plus the runner-up, so you can
audit ambiguity instead of trusting a silent decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, NamedTuple


class DocType(str, Enum):
    INTEGRATED_ANNUAL_REPORT = "IAR"
    ANNUAL_FINANCIAL_STATEMENTS = "AFS"
    SUMMARISED_RESULTS = "SUMRES"
    INTERIM_RESULTS = "INTERIM"
    SENS_ANNOUNCEMENT = "SENS"
    IR_PRESENTATION = "PRES"
    DEBT_PROGRAMME = "DEBT"
    ESG_SUSTAINABILITY = "ESG"
    NOTICE_OF_AGM = "AGM"
    TRADING_STATEMENT = "TRADESTMT"
    UNKNOWN = "UNKNOWN"


def _rx(*patterns: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.I) for p in patterns)


@dataclass(frozen=True)
class Rule:
    doc_type: DocType
    anchors: tuple[re.Pattern, ...]
    boosts: tuple[tuple[re.Pattern, int], ...] = field(default_factory=tuple)
    vetoes: tuple[re.Pattern, ...] = field(default_factory=tuple)
    base_weight: int = 10

    def score(self, text: str) -> int:
        for v in self.vetoes:
            if v.search(text):
                return 0
        if not any(a.search(text) for a in self.anchors):
            return 0
        total = self.base_weight
        for pattern, weight in self.boosts:
            if pattern.search(text):
                total += weight
        return total


# Shared veto fragments -------------------------------------------------------

_INTERIM_VETO = _rx(
    r"\binterim\b",
    r"half[\s\-]?year",
    r"\bsix months?\b",
    r"\bH1\b",
    r"first half",
    r"\bQ[1-4]\b",
)
_PRESENTATION_VETO = _rx(
    r"\bpresentation\b", r"\bslides?\b", r"\bbooklet\b", r"\bwebcast\b", r"\broadshow\b"
)


RULES: tuple[Rule, ...] = (
    # 1. Integrated Annual Report -------------------------------------------
    Rule(
        doc_type=DocType.INTEGRATED_ANNUAL_REPORT,
        anchors=_rx(
            r"integrated[\s\-]*(annual[\s\-]*)?report",
            r"\bIAR\b",
            r"\bannual report\b",
            r"integrated[\s\-]*review",
        ),
        boosts=(
            (re.compile(r"integrated", re.I), 8),
            (re.compile(r"\bIAR\b", re.I), 6),
            (re.compile(r"value creation", re.I), 2),
            (re.compile(r"\.pdf(\?|$)", re.I), 2),
        ),
        vetoes=_INTERIM_VETO
        + _PRESENTATION_VETO
        + _rx(r"notice of (the )?annual general", r"\bAGM\b", r"\bsummar(y|ised)\b"),
    ),
    # 2. Audited Annual Financial Statements --------------------------------
    Rule(
        doc_type=DocType.ANNUAL_FINANCIAL_STATEMENTS,
        anchors=_rx(
            r"(annual|consolidated|group|audited|separate)[\s\-]*financial statements",
            r"\bAFS\b",
            r"financial statements for the year",
            r"audited (consolidated )?results for the year",
        ),
        boosts=(
            (re.compile(r"\baudited\b", re.I), 8),
            (re.compile(r"\bconsolidated\b", re.I), 4),
            (re.compile(r"\bgroup\b", re.I), 2),
            (re.compile(r"for the (financial )?year end", re.I), 4),
        ),
        vetoes=_INTERIM_VETO
        + _PRESENTATION_VETO
        + _rx(r"\bsummar(y|ised)\b", r"\bprovisional\b"),
    ),
    # 2b. Summarised / provisional annual results ---------------------------
    # A distinct JSE artefact: the condensed year-end results released with
    # the SENS announcement, weeks before the full AFS. Same numbers, fewer
    # notes. Keeping it separate stops it polluting your AFS set with
    # documents that have no footnotes to extract.
    Rule(
        doc_type=DocType.SUMMARISED_RESULTS,
        anchors=_rx(
            r"summar(y|ised|ized)\s+(audited\s+)?(consolidated\s+)?(annual\s+)?(financial\s+statements|results)",
            r"provisional\s+(audited\s+)?results",
            r"(audited\s+)?(short[\s\-]?form|abridged)\s+(announcement|results)",
            r"preliminary\s+(audited\s+)?results",
        ),
        boosts=(
            (re.compile(r"for the year end", re.I), 4),
            (re.compile(r"\baudited\b", re.I), 3),
        ),
        vetoes=_INTERIM_VETO + _PRESENTATION_VETO,
        base_weight=15,
    ),
    # 3. Interim / half-year -------------------------------------------------
    Rule(
        doc_type=DocType.INTERIM_RESULTS,
        anchors=_rx(
            r"\binterim\b",
            r"half[\s\-]?year",
            r"\bsix months? end",
            r"\bH1[\s\-]?(FY)?\d{2,4}\b",
        ),
        boosts=(
            (re.compile(r"results", re.I), 6),
            (re.compile(r"reviewed|unaudited", re.I), 4),
            (re.compile(r"condensed", re.I), 4),
            (re.compile(r"financial statements", re.I), 3),
        ),
        vetoes=_PRESENTATION_VETO,
    ),
    # 4. SENS ----------------------------------------------------------------
    Rule(
        doc_type=DocType.SENS_ANNOUNCEMENT,
        anchors=_rx(
            r"\bSENS\b",
            r"senspdf\.jse\.co\.za",
            r"stock exchange news service",
            r"cautionary announcement",
            r"\bdealings? in securities\b",
        ),
        boosts=(
            (re.compile(r"senspdf\.jse\.co\.za", re.I), 15),
            (re.compile(r"announcement", re.I), 3),
        ),
        base_weight=12,
    ),
    # 4b. Trading statements (a SENS subtype worth its own bucket) -----------
    Rule(
        doc_type=DocType.TRADING_STATEMENT,
        anchors=_rx(
            r"trading (statement|update)",
            r"operational update",
            r"business update",
            r"voluntary (trading )?(statement|update)",
            r"pre[\s\-]?close (update|presentation)",
        ),
        boosts=(
            (re.compile(r"\bHEPS\b|headline earnings", re.I), 6),
            (re.compile(r"further trading statement", re.I), 4),
        ),
        base_weight=14,  # outrank generic SENS when both match
    ),
    # 5. IR presentations ----------------------------------------------------
    Rule(
        doc_type=DocType.IR_PRESENTATION,
        anchors=_rx(
            r"\bpresentation\b",
            r"results booklet",
            r"\bslides?\b",
            r"investor day",
            r"capital markets day",
            r"\bwebcast\b",
        ),
        boosts=(
            (re.compile(r"results", re.I), 4),
            (re.compile(r"\.pptx?(\?|$)", re.I), 6),
            (re.compile(r"investor", re.I), 3),
        ),
        base_weight=12,
    ),
    # 6. Debt programme / prospectus ----------------------------------------
    Rule(
        doc_type=DocType.DEBT_PROGRAMME,
        anchors=_rx(
            r"programme memorandum",
            r"base prospectus",
            r"\bDMTN\b",
            r"domestic medium[\s\-]term note",
            r"applicable pricing supplement",
            r"debt investors?",
            r"bond (issuance|programme)",
            r"credit rating (report|announcement)",
        ),
        boosts=(
            (re.compile(r"supplement", re.I), 3),
            (re.compile(r"\bnotes?\b", re.I), 2),
        ),
        base_weight=14,
    ),
    # 7. ESG -----------------------------------------------------------------
    Rule(
        doc_type=DocType.ESG_SUSTAINABILITY,
        anchors=_rx(
            r"\bESG\b",
            r"sustainability report",
            r"climate (change )?(report|disclosure)",
            r"\bTCFD\b",
            r"\bGRI\b index",
            r"transformation report",
            r"\bB[\s\-]?BBEE\b",
        ),
        base_weight=11,
    ),
    # 8. AGM -----------------------------------------------------------------
    Rule(
        doc_type=DocType.NOTICE_OF_AGM,
        anchors=_rx(
            r"notice of (the )?annual general meeting",
            r"\bAGM\b",
            r"form of proxy",
        ),
        base_weight=13,
    ),
)


# ---------------------------------------------------------------------------
# Year extraction
# ---------------------------------------------------------------------------

_FY_PATTERNS = (
    # "FY2024", "FY24"
    re.compile(r"\bFY[\s\-]?(?P<y>\d{2,4})\b", re.I),
    # "year ended 30 June 2024"
    re.compile(
        r"(?:year|period)\s+end(?:ed|ing)\s+\d{1,2}\s+\w+\s+(?P<y>20\d{2})", re.I
    ),
    # "2023/24", "2023/2024"  -> take the later year
    re.compile(r"\b20(?P<a>\d{2})\s*[/-]\s*(?P<b>\d{2,4})\b"),
    # bare year
    re.compile(r"\b(?P<y>20[12]\d)\b"),
)


class YearGuess(NamedTuple):
    year: int | None
    confidence: str  # "explicit_fye" | "fy_label" | "bare" | "none"


def extract_year(*sources: str, min_year: int = 2023, max_year: int = 2030) -> YearGuess:
    """
    Extract the reporting year from link text, href and filename.

    IMPORTANT CAVEAT: this returns the *label* year, which is the fiscal year
    the document reports on, not a calendar year. South African fiscal year
    ends are scattered - Shoprite and Sasol are 30 June, Naspers/Prosus is
    31 March, Bidvest is 30 June, Standard Bank is 31 December. Do NOT join
    this field against calendar-year macro data without first resolving each
    company's FYE. See config.FISCAL_YEAR_END for the override table.
    """
    blob = " ".join(s for s in sources if s)

    for idx, pattern in enumerate(_FY_PATTERNS):
        m = pattern.search(blob)
        if not m:
            continue
        groups = m.groupdict()
        if "a" in groups and groups.get("a") is not None:
            b = groups["b"]
            year = int(b) if len(b) == 4 else 2000 + int(b)
        else:
            raw = groups["y"]
            year = int(raw) if len(raw) == 4 else 2000 + int(raw)
        if min_year <= year <= max_year:
            confidence = ("fy_label", "explicit_fye", "bare", "bare")[idx]
            return YearGuess(year, confidence)

    return YearGuess(None, "none")


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class Classification(NamedTuple):
    doc_type: DocType
    score: int
    runner_up: DocType
    runner_up_score: int

    @property
    def is_ambiguous(self) -> bool:
        """True when the top two candidates are close enough to warrant review."""
        return self.runner_up_score > 0 and (self.score - self.runner_up_score) <= 3


def classify(*sources: str, min_score: int = 10) -> Classification:
    """Classify a candidate document from its link text, href and filename."""
    blob = " ".join(s for s in sources if s)
    scored = sorted(
        ((rule.score(blob), rule.doc_type) for rule in RULES),
        key=lambda pair: pair[0],
        reverse=True,
    )
    top_score, top_type = scored[0]
    second_score, second_type = scored[1] if len(scored) > 1 else (0, DocType.UNKNOWN)

    if top_score < min_score:
        return Classification(DocType.UNKNOWN, 0, DocType.UNKNOWN, 0)
    return Classification(top_type, top_score, second_type, second_score)


DOWNLOADABLE_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".ppt", ".pptx", ".xls", ".xlsx", ".doc", ".docx", ".zip"}
)


def looks_downloadable(href: str) -> bool:
    lowered = href.split("?")[0].split("#")[0].lower()
    return any(lowered.endswith(ext) for ext in DOWNLOADABLE_EXTENSIONS)


def all_doc_types() -> Iterable[DocType]:
    return (dt for dt in DocType if dt is not DocType.UNKNOWN)
