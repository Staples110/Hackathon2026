"""Regression tests for the two components most likely to fail silently."""

import pytest

from jse_reports.parser import detect_scale, parse_za_number
from jse_reports.taxonomy import DocType, classify, extract_year


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12 204 573", 12_204_573.0),   # space thousands separator
        ("1 234,5", 1234.5),            # comma decimal
        ("(2 145)", -2145.0),           # bracket negative
        ("1,5", 1.5),                   # bare comma decimal
        ("R12 345,67", 12_345.67),      # currency prefix
        ("1.234,56", 1234.56),          # dot thousands, comma decimal
        ("1,234.56", 1234.56),          # comma thousands, dot decimal
        ("-", 0.0),                     # dash means nil
    ],
)
def test_za_number_parsing(raw, expected):
    assert parse_za_number(raw).value == pytest.approx(expected)


def test_ambiguous_comma_is_flagged_not_guessed():
    """'1,234' is 1234 (UK) or 1.234 (SA). Flag it; never silently pick."""
    result = parse_za_number("1,234")
    assert result.ambiguous is True
    assert result.note


@pytest.mark.parametrize(
    "text,multiplier",
    [("All figures in R'000", 1e3), ("Rm", 1e6), ("R million", 1e6), ("ZAR bn", 1e9)],
)
def test_scale_detection(text, multiplier):
    assert detect_scale(text)[0] == multiplier


def test_undeclared_scale_is_loud():
    _, label = detect_scale("Revenue for the year")
    assert "NOT DECLARED" in label


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Integrated Annual Report 2024", DocType.INTEGRATED_ANNUAL_REPORT),
        ("Audited consolidated annual financial statements 2024",
         DocType.ANNUAL_FINANCIAL_STATEMENTS),
        ("Reviewed condensed consolidated interim results 2024", DocType.INTERIM_RESULTS),
        ("Results presentation FY2024", DocType.IR_PRESENTATION),
        ("DMTN Programme Memorandum 2023", DocType.DEBT_PROGRAMME),
        ("Notice of Annual General Meeting 2025", DocType.NOTICE_OF_AGM),
        ("Trading statement for the year ended 30 June 2025", DocType.TRADING_STATEMENT),
        ("Summarised audited results 2024", DocType.SUMMARISED_RESULTS),
        ("Sustainability Report 2024", DocType.ESG_SUSTAINABILITY),
    ],
)
def test_classification_respects_nesting(title, expected):
    """First-match-wins would fail most of these; scoring with vetoes does not."""
    assert classify(title).doc_type is expected


def test_interim_never_wins_annual_report():
    result = classify("Interim report for the six months ended 31 December 2024")
    assert result.doc_type is not DocType.INTEGRATED_ANNUAL_REPORT


@pytest.mark.parametrize(
    "text,year",
    [
        ("Annual Report 2024", 2024),
        ("FY25 results", 2025),
        ("year ended 30 June 2024", 2024),
        ("Annual Financial Statements 2023/24", 2024),
    ],
)
def test_year_extraction(text, year):
    assert extract_year(text).year == year
