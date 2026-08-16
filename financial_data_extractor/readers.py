"""
Format readers.

Every reader emits the same thing: a flat stream of `CellRecord`, each carrying
enough provenance to point at exactly where in the source it came from
(file, page or sheet, row, column). That is what makes the output auditable —
any figure in a CSV can be traced back to a coordinate in a source document.

PDF note: table extraction runs twice, once via ruled-table detection and once
via geometric word clustering. The second pass exists because most financial
statement pages have no ruled table, and the obvious fallback — regex over
`extract_text()` — destroys the data. After text extraction the row

    Revenue      12       204 573       189 122
                 note      FY24          FY23

becomes the string "Revenue 12 204 573 189 122". The gap between columns and
the space inside "204 573" are now the same character, so a parser treating
space as a thousands separator reads one number: 12,204,573,189,122. The
information needed to split it is geometric (inter-column gaps run 10-40pt,
intra-number gaps 2-4pt), so we cluster on x-coordinates and never let a
regex see the joined string.
"""

from __future__ import annotations

import csv as _csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .normalize import detect_currency, detect_period, detect_scale, looks_numeric

log = logging.getLogger(__name__)


@dataclass
class CellRecord:
    """One extracted cell, with full provenance."""
    source_file: str
    source_path: str
    container: str          # "page 3" | "sheet: Income Statement" | "row block"
    container_index: int
    row_index: int
    col_index: int
    row_label: str
    column_header: str
    raw_value: str
    extraction_method: str  # ruled_table | word_cluster | worksheet_cell | delimited_row
    context_scale: str | None = None
    context_currency: str | None = None
    context_period: str | None = None


@dataclass
class DocumentResult:
    path: Path
    cells: list[CellRecord] = field(default_factory=list)
    doc_currency: str | None = None
    doc_scale: str | None = None
    doc_period: str | None = None
    company_name: str | None = None
    pages_or_sheets: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def read_pdf(path: Path, max_pages: int | None = None) -> DocumentResult:
    import pdfplumber

    result = DocumentResult(path=path)
    with pdfplumber.open(path) as pdf:
        result.pages_or_sheets = len(pdf.pages)
        pages = pdf.pages[:max_pages] if max_pages else pdf.pages

        first_text = (pages[0].extract_text() or "")[:3000] if pages else ""
        result.company_name = _guess_company_from_header(first_text)
        result.doc_currency = detect_currency(first_text)
        s = detect_scale(first_text)
        result.doc_scale = s.label if s.stated else None
        result.doc_period = detect_period(first_text)

        for pno, page in enumerate(pages, start=1):
            text = page.extract_text() or ""
            if not text.strip():
                continue

            head = text[:1200]
            pg_scale = detect_scale(head)
            pg_currency = detect_currency(head) or result.doc_currency
            pg_period = detect_period(head) or result.doc_period
            ctx_scale = pg_scale.label if pg_scale.stated else result.doc_scale

            seen: set[str] = set()

            for table in page.extract_tables() or []:
                header = [(c or "").strip() for c in table[0]] if table else []
                for rno, row in enumerate(table):
                    cells = [(c or "").strip() for c in row]
                    if not any(cells):
                        continue
                    key = "|".join(cells)
                    if key in seen:
                        continue
                    seen.add(key)
                    label = cells[0]
                    for cno, cell in enumerate(cells[1:], start=1):
                        if not cell or not looks_numeric(cell):
                            continue
                        result.cells.append(CellRecord(
                            source_file=path.name, source_path=str(path),
                            container=f"page {pno}", container_index=pno,
                            row_index=rno, col_index=cno,
                            row_label=label,
                            column_header=header[cno] if cno < len(header) else "",
                            raw_value=cell, extraction_method="ruled_table",
                            context_scale=ctx_scale, context_currency=pg_currency,
                            context_period=pg_period,
                        ))

            for rno, (label, cells, raw) in enumerate(_rows_from_words(page)):
                if raw in seen:
                    continue
                seen.add(raw)
                for cno, cell in enumerate(cells, start=1):
                    if not looks_numeric(cell):
                        continue
                    result.cells.append(CellRecord(
                        source_file=path.name, source_path=str(path),
                        container=f"page {pno}", container_index=pno,
                        row_index=rno, col_index=cno,
                        row_label=label, column_header="",
                        raw_value=cell, extraction_method="word_cluster",
                        context_scale=ctx_scale, context_currency=pg_currency,
                        context_period=pg_period,
                    ))
    return result


def _rows_from_words(page, line_tolerance: float = 2.5):
    """Rebuild rows from word coordinates. See module docstring for why."""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    if not words:
        return

    lines: dict[float, list[dict]] = {}
    for w in words:
        key = round(w["top"] / line_tolerance) * line_tolerance
        lines.setdefault(key, []).append(w)

    for key in sorted(lines):
        row = sorted(lines[key], key=lambda w: w["x0"])
        if len(row) < 2:
            continue
        heights = [w["bottom"] - w["top"] for w in row]
        font_size = sum(heights) / len(heights)
        gap_threshold = max(4.0, font_size * 0.55)

        groups: list[list[dict]] = [[row[0]]]
        for prev, cur in zip(row, row[1:]):
            if cur["x0"] - prev["x1"] > gap_threshold:
                groups.append([cur])
            else:
                groups[-1].append(cur)

        cells = [" ".join(w["text"] for w in g) for g in groups]
        numeric_start = next((i for i, c in enumerate(cells) if looks_numeric(c)), len(cells))
        if numeric_start == 0 or numeric_start == len(cells):
            continue
        label = re.sub(r"[.\s]{3,}$", "", " ".join(cells[:numeric_start])).strip()
        if len(label) < 2:
            continue
        yield label, cells[numeric_start:], " | ".join(cells)[:400]


_COMPANY_HINT = re.compile(
    r"^(?P<name>[A-Z][A-Za-z0-9&.,'\- ]{3,70}?"
    r"(?:Limited|Ltd|PLC|plc|Inc\.?|Corporation|Corp\.?|Holdings|Group|N\.V\.|S\.A\.|AG|SE))\b"
)


def _guess_company_from_header(text: str) -> str | None:
    """Match a legal-entity suffix in the first lines. No suffix, no name."""
    for line in text.splitlines()[:15]:
        m = _COMPANY_HINT.match(line.strip())
        if m:
            return m.group("name").strip()
    return None


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

def read_excel(path: Path) -> DocumentResult:
    import pandas as pd

    result = DocumentResult(path=path)
    engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"
    sheets = pd.read_excel(path, sheet_name=None, header=None, engine=engine, dtype=object)
    result.pages_or_sheets = len(sheets)

    for sno, (sheet_name, df) in enumerate(sheets.items(), start=1):
        if df.empty:
            continue
        preamble = " ".join(
            str(v) for v in df.head(8).values.flatten() if v is not None and str(v) != "nan"
        )[:2000]
        sh_scale = detect_scale(f"{sheet_name} {preamble}")
        sh_currency = detect_currency(f"{sheet_name} {preamble}")
        sh_period = detect_period(f"{sheet_name} {preamble}")

        if result.company_name is None:
            result.company_name = _guess_company_from_header(preamble)
        result.doc_currency = result.doc_currency or sh_currency
        result.doc_scale = result.doc_scale or (sh_scale.label if sh_scale.stated else None)
        result.doc_period = result.doc_period or sh_period

        header_row = _find_header_row(df)
        headers = ([str(v) if v is not None else "" for v in df.iloc[header_row]]
                   if header_row is not None else [])

        for rno in range(df.shape[0]):
            if header_row is not None and rno <= header_row:
                continue
            row = df.iloc[rno]
            label = next((str(v).strip() for v in row
                          if v is not None and str(v) != "nan" and not looks_numeric(str(v))), "")
            for cno in range(df.shape[1]):
                v = row.iloc[cno]
                if v is None or str(v) == "nan":
                    continue
                cell = str(v).strip()
                if not cell or not looks_numeric(cell):
                    continue
                result.cells.append(CellRecord(
                    source_file=path.name, source_path=str(path),
                    container=f"sheet: {sheet_name}", container_index=sno,
                    row_index=rno, col_index=cno,
                    row_label=label,
                    column_header=headers[cno].strip() if cno < len(headers) else "",
                    raw_value=cell, extraction_method="worksheet_cell",
                    context_scale=sh_scale.label if sh_scale.stated else result.doc_scale,
                    context_currency=sh_currency or result.doc_currency,
                    context_period=sh_period or result.doc_period,
                ))
    return result


def _find_header_row(df, scan: int = 10) -> int | None:
    """First row in the top `scan` rows that is mostly non-numeric text."""
    for i in range(min(scan, df.shape[0])):
        vals = [str(v).strip() for v in df.iloc[i] if v is not None and str(v) != "nan"]
        if len(vals) < 2:
            continue
        if sum(1 for v in vals if not looks_numeric(v)) / len(vals) >= 0.6:
            return i
    return None


# ---------------------------------------------------------------------------
# Delimited text
# ---------------------------------------------------------------------------


_YEAR_LABEL = re.compile(r"^(?:FY\s*)?(?:19|20)\d{2}(?:[/-]\d{2,4})?$|^Q[1-4]\s*(?:19|20)?\d{2}$|^H[12]\s*(?:19|20)?\d{2}$", re.I)


def _is_header_row(cells: list[str]) -> bool:
    """
    A header row is text label(s) plus period labels.

    The naive test - "mostly non-numeric" - fails on the commonest header in
    finance, `Line item | 2024 | 2023`, because two of its three cells parse
    as numbers. Without this, the header is emitted as a data row and the
    years 2024 and 2023 enter the dataset as financial figures.
    """
    filled = [c.strip() for c in cells if c and c.strip()]
    if len(filled) < 2:
        return False
    if not any(not looks_numeric(c) for c in filled):
        return False
    period_like = sum(1 for c in filled if _YEAR_LABEL.match(c))
    text_like = sum(1 for c in filled if not looks_numeric(c))
    return (period_like + text_like) / len(filled) >= 0.6


def read_delimited(path: Path) -> DocumentResult:
    result = DocumentResult(path=path)
    result.pages_or_sheets = 1

    raw = path.read_text(encoding="utf-8", errors="replace")
    sample = raw[:8192]
    try:
        dialect = _csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except _csv.Error:
        dialect = _csv.excel

    rows = list(_csv.reader(raw.splitlines(), dialect))
    if not rows:
        return result

    preamble = " ".join(" ".join(r) for r in rows[:8])[:2000]
    sc = detect_scale(preamble)
    result.doc_currency = detect_currency(preamble)
    result.doc_scale = sc.label if sc.stated else None
    result.doc_period = detect_period(preamble)
    result.company_name = _guess_company_from_header(preamble)

    header_idx = next((i for i, r in enumerate(rows[:10]) if _is_header_row(r)), None)
    headers = rows[header_idx] if header_idx is not None else []

    for rno, row in enumerate(rows):
        if header_idx is not None and rno <= header_idx:
            continue
        if not any(c.strip() for c in row):
            continue
        label = next((c.strip() for c in row if c.strip() and not looks_numeric(c)), "")
        for cno, cell in enumerate(row):
            cell = cell.strip()
            if not cell or not looks_numeric(cell):
                continue
            result.cells.append(CellRecord(
                source_file=path.name, source_path=str(path),
                container="row block", container_index=1,
                row_index=rno, col_index=cno,
                row_label=label,
                column_header=headers[cno].strip() if cno < len(headers) else "",
                raw_value=cell, extraction_method="delimited_row",
                context_scale=result.doc_scale, context_currency=result.doc_currency,
                context_period=result.doc_period,
            ))
    return result


def read_text(path: Path) -> DocumentResult:
    """Plain .txt: only rows that clearly carry a label plus figures."""
    result = DocumentResult(path=path)
    result.pages_or_sheets = 1
    raw = path.read_text(encoding="utf-8", errors="replace")

    preamble = raw[:2000]
    sc = detect_scale(preamble)
    result.doc_currency = detect_currency(preamble)
    result.doc_scale = sc.label if sc.stated else None
    result.doc_period = detect_period(preamble)
    result.company_name = _guess_company_from_header(preamble)

    for rno, line in enumerate(raw.splitlines()):
        if not line.strip():
            continue
        parts = [p for p in re.split(r"\s{2,}|\t", line.strip()) if p]
        if len(parts) < 2:
            continue
        label, values = parts[0], parts[1:]
        if looks_numeric(label):
            continue
        for cno, cell in enumerate(values, start=1):
            if not looks_numeric(cell):
                continue
            result.cells.append(CellRecord(
                source_file=path.name, source_path=str(path),
                container="text body", container_index=1,
                row_index=rno, col_index=cno,
                row_label=label.strip(), column_header="",
                raw_value=cell.strip(), extraction_method="delimited_row",
                context_scale=result.doc_scale, context_currency=result.doc_currency,
                context_period=result.doc_period,
            ))
    return result


READERS = {
    ".pdf": read_pdf,
    ".xlsx": read_excel,
    ".xlsm": read_excel,
    ".xls": read_excel,
    ".csv": read_delimited,
    ".tsv": read_delimited,
    ".txt": read_text,
}


def read_any(path: Path) -> DocumentResult:
    reader = READERS.get(path.suffix.lower())
    if reader is None:
        return DocumentResult(path=path, error=f"unsupported extension {path.suffix!r}")
    try:
        return reader(path)
    except Exception as exc:  # noqa: BLE001 — one bad file must not stop the run
        return DocumentResult(path=path, error=f"{type(exc).__name__}: {exc}")
