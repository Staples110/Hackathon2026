"""
Line-item mapping — the interpretive layer, deliberately quarantined.

Why this is a separate module from `readers`
--------------------------------------------
The specification asks for two things that are in tension:

  (a) "100% Extractive Execution ... Verbatim Values ... retain exact
      line-item descriptions as written"
  (b) standard snake_case columns such as `total_revenue`, `net_income`

You cannot have both in one table. Deciding that a row labelled "Turnover"
belongs in a column called `total_revenue` is a judgement, not an extraction.
IFRS mandates which statements are presented, not the wording of the lines, so
"Revenue", "Turnover", "Revenue from contracts with customers" and "Sale of
merchandise" are the same concept across four issuers — and no verbatim rule
can merge them.

The resolution used here: the mapping happens, but it is confined to this
module and every mapped row carries `raw_label`, `mapped_by`, and
`mapping_confidence`. The verbatim long-format CSV is the source of truth and
is written regardless. If a mapping is wrong you can see which rule fired and
re-derive the wide table without re-reading a single PDF.

Concepts are scored by specificity, not matched first-wins, because the labels
nest: "cost of sales" contains "sales", "gross profit" contains "profit".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# concept -> ((regex, specificity), ...). Higher specificity wins a collision.
CONCEPT_RULES: dict[str, tuple[tuple[str, int], ...]] = {
    "total_revenue": (
        (r"^revenue\s+from\s+contracts?\s+with\s+customers", 10),
        (r"^total\s+(?:net\s+)?revenues?\b", 10),
        (r"^(?:net\s+)?revenues?\b", 7),
        (r"^turnover\b", 7),
        (r"^total\s+(?:net\s+)?sales\b", 8),
        (r"^sale\s+of\s+merchandise\b", 6),
    ),
    "cost_of_sales": (
        (r"^cost\s+of\s+(?:sales|revenue|goods\s+sold)\b", 10),
        (r"^cost\s+of\s+merchandise\s+sold\b", 9),
        (r"^\bCOGS\b", 8),
    ),
    "gross_profit": ((r"^gross\s+(?:profit|margin|income)\b", 10),),
    "operating_expenses": (
        (r"^total\s+operating\s+expenses\b", 10),
        (r"^operating\s+(?:expenses|costs)\b", 8),
        (r"^selling,?\s+general\s+and\s+admin", 7),
        (r"^\bSG&A\b", 7),
    ),
    "staff_costs": (
        (r"^(?:staff|employee|personnel)\s+(?:costs?|expenses?|benefits?\s+expense)", 10),
        (r"^salaries\s+and\s+wages\b", 8),
    ),
    "operating_profit": (
        (r"^operating\s+(?:profit|income)\b", 9),
        (r"^(?:profit|earnings)\s+before\s+interest\s+and\s+tax", 9),
        (r"^\bEBIT\b", 8),
    ),
    "finance_costs": (
        (r"^finance\s+(?:costs?|charges?|expenses?)\b", 10),
        (r"^interest\s+(?:paid|expense)\b", 8),
    ),
    "profit_before_tax": (
        (r"^(?:profit|income|earnings)\s+before\s+(?:income\s+)?tax", 10),
        (r"^\bPBT\b", 8),
    ),
    "income_tax_expense": (
        (r"^(?:income\s+)?tax(?:ation)?\s+expense\b", 10),
        (r"^provision\s+for\s+income\s+taxes\b", 9),
    ),
    "net_income": (
        (r"^profit\s+for\s+the\s+(?:year|period)\b", 10),
        (r"^net\s+(?:profit|income|earnings)\b", 9),
        (r"^profit\s+attributable\s+to\s+(?:owners|equity\s+holders)", 9),
    ),
    "earnings_per_share": (
        (r"^(?:basic\s+)?earnings\s+per\s+share\b", 10),
        (r"^\bEPS\b", 8),
        (r"^headline\s+earnings\s+per\s+share\b", 10),
    ),
    "inventories": (
        (r"^inventor(?:y|ies)\b", 10),
        (r"^stock\s+on\s+hand\b", 8),
    ),
    "trade_receivables": (
        (r"^trade\s+(?:and\s+other\s+)?receivables\b", 10),
        (r"^accounts?\s+receivable\b", 8),
        (r"^debtors\b", 6),
    ),
    "trade_payables": (
        (r"^trade\s+(?:and\s+other\s+)?payables\b", 10),
        (r"^accounts?\s+payable\b", 8),
        (r"^creditors\b", 6),
    ),
    "cash_and_equivalents": (
        (r"^cash\s+and\s+cash\s+equivalents\b", 10),
        (r"^cash\s+and\s+short[-\s]term\s+deposits\b", 9),
        (r"^bank\s+balances?\s+and\s+cash\b", 8),
    ),
    "total_assets": ((r"^total\s+assets\b", 10),),
    "total_liabilities": ((r"^total\s+liabilities\b", 10),),
    "total_equity": (
        (r"^total\s+equity\b", 10),
        (r"^(?:total\s+)?shareholders?'?\s+equity\b", 9),
    ),
    "long_term_debt": (
        (r"^(?:non[-\s]current|long[-\s]term)\s+(?:interest[-\s]bearing\s+)?borrowings\b", 10),
        (r"^long[-\s]term\s+debt\b", 9),
    ),
    "short_term_debt": (
        (r"^(?:current|short[-\s]term)\s+(?:portion\s+of\s+)?(?:interest[-\s]bearing\s+)?borrowings\b", 10),
        (r"^current\s+portion\s+of\s+long[-\s]term\s+debt\b", 10),
        (r"^bank\s+overdrafts?\b", 7),
    ),
    "cash_from_operations": (
        (r"^(?:net\s+)?cash\s+(?:generated\s+)?(?:from|by)\s+operat", 10),
        (r"^cash\s+flows?\s+from\s+operating\s+activities\b", 10),
    ),
    "capital_expenditure": (
        (r"^(?:additions?\s+to|purchases?\s+of|acquisition\s+of)\s+"
         r"(?:property,?\s+plant\s+and\s+equipment|PPE)\b", 10),
        (r"^capital\s+expenditure\b", 9),
    ),
    "dividends_paid": (
        (r"^dividends?\s+paid\b", 10),
        (r"^distributions?\s+(?:paid\s+)?to\s+shareholders\b", 8),
    ),
    "debt_repayments": (
        (r"^repayments?\s+of\s+(?:borrowings|long[-\s]term\s+debt)\b", 10),
    ),
}

_COMPILED = {
    concept: tuple((re.compile(p, re.I), s) for p, s in rules)
    for concept, rules in CONCEPT_RULES.items()
}


@dataclass(frozen=True)
class Mapping:
    concept: str | None
    specificity: int
    pattern: str
    runner_up: str | None

    @property
    def confidence(self) -> str:
        if self.concept is None:
            return "unmapped"
        if self.runner_up:
            return "ambiguous"
        return "high" if self.specificity >= 9 else "medium"


def map_label(label: str) -> Mapping:
    """Map a verbatim row label to a standard concept, or return unmapped."""
    if not label:
        return Mapping(None, 0, "", None)
    clean = re.sub(r"\s+", " ", label).strip(" .:*†‡0123456789")

    hits: list[tuple[int, str, str]] = []
    for concept, rules in _COMPILED.items():
        best = max(
            ((s, p.pattern) for p, s in rules if p.search(clean)),
            default=None,
        )
        if best:
            hits.append((best[0], concept, best[1]))

    if not hits:
        return Mapping(None, 0, "", None)

    hits.sort(reverse=True)
    top_spec, top_concept, top_pattern = hits[0]
    runner_up = None
    if len(hits) > 1 and hits[1][0] == top_spec:
        runner_up = hits[1][1]
    return Mapping(top_concept, top_spec, top_pattern, runner_up)


def concepts() -> list[str]:
    return list(CONCEPT_RULES.keys())
