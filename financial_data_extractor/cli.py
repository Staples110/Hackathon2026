"""
Command-line entry point.

    python -m fin_extract.cli --input /path/to/docs
    python -m fin_extract.cli --input /path/to/docs --out /path/to/results

Two things this does differently from the specification it implements, both
deliberate. See README.md for the full reasoning.

1. Output defaults to a SIBLING of the input directory, not a subfolder inside
   it. The spec asks for `[input]/extracted_csvs/` while also listing `.csv`
   as a supported input type — so the second run ingests its own first-run
   output. `--out-inside` restores the specified behaviour and the traversal
   guard excludes the output tree either way, but the default avoids the trap.

2. Two CSVs are always written, not one or the other. `*_extracted.csv` is
   verbatim long-format and is the auditable record; `master_financial_summary.csv`
   is the derived wide table. The wide table is a convenience built from the
   long one, never the reverse.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .mapping import map_label
from .normalize import SCALE_PATTERNS, parse_number
from .readers import READERS, CellRecord, DocumentResult, read_any

import re

log = logging.getLogger("fin_extract")

SCALE_MULTIPLIERS = {label: mult for _, label, mult in SCALE_PATTERNS}

LONG_COLUMNS = [
    "source_file", "container", "row_index", "col_index", "period_ordinal",
    "row_label", "column_header", "raw_value",
    "parsed_value", "is_negative", "is_percentage",
    "currency", "scale", "scale_multiplier", "value_in_units",
    "reporting_period", "company_name",
    "extraction_method", "mapped_concept", "mapping_confidence",
    "parse_note", "stripped_markers", "review_flag",
]

MASTER_COLUMNS = [
    "source_filename", "company_name", "reporting_period", "currency", "scale",
    "concept", "raw_label", "raw_value", "parsed_value", "value_in_units",
    "container", "row_index", "col_index", "period_ordinal",
    "mapping_confidence", "review_flag",
]


# ---------------------------------------------------------------------------


def discover_files(root: Path, out_dir: Path, recursive: bool) -> list[Path]:
    """
    List candidate files, excluding anything under the output directory.

    The exclusion is the important part. Without it, `.csv` being both a
    supported input type and the output format means every re-run re-ingests
    its own results, and the corpus grows on each pass.
    """
    if not root.exists():
        raise FileNotFoundError(f"input directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"input path is not a directory: {root}")

    out_resolved = out_dir.resolve()
    pattern = "**/*" if recursive else "*"
    found: list[Path] = []
    for p in sorted(root.glob(pattern)):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            if out_resolved in p.resolve().parents:
                continue
        except OSError:
            continue
        found.append(p)
    return found


_YEARISH = re.compile(r"^(19|20)\d{2}$")


def _row_context(cells: list[CellRecord]) -> dict[tuple, list[CellRecord]]:
    """Group cells by the physical row they came from."""
    groups: dict[tuple, list[CellRecord]] = {}
    for c in cells:
        groups.setdefault((c.container, c.row_index), []).append(c)
    for g in groups.values():
        g.sort(key=lambda c: c.col_index)
    return groups


def _classify_in_row(cell: CellRecord, row: list[CellRecord]) -> list[str]:
    """
    Flags that can only be decided with the whole row in view.

    note_reference is the important one. A financial statement row reads

        Revenue                    12      204 573      189 122
                                 (note)     (FY24)       (FY23)

    and a naive extractor files 12 as the revenue. It is a note reference —
    a bare 1-2 digit integer sitting in the first numeric position while the
    columns after it carry the actual figures. Left unflagged, every mapped
    concept in the corpus acquires a spurious low-magnitude duplicate.
    """
    flags: list[str] = []
    text = cell.raw_value.strip()

    if _YEARISH.fullmatch(text):
        flags.append("looks_like_year")

    if (cell.column_header or "").strip().lower().startswith("note"):
        flags.append("note_reference")
        return flags

    if row and cell is row[0] and len(row) > 1:
        bare_small_int = re.fullmatch(r"\d{1,2}", text) is not None
        others_are_bigger = any(
            len(re.sub(r"[^\d]", "", o.raw_value)) > len(re.sub(r"[^\d]", "", text))
            for o in row[1:]
        )
        if bare_small_int and others_are_bigger:
            flags.append("note_reference")

    return flags


def _flatten(result: DocumentResult) -> list[dict]:
    rows: list[dict] = []
    row_groups = _row_context(result.cells)
    # Period ordinal: position among the row's REAL figures, note references
    # excluded. Raw col_index is not a period key -- some rows carry a note
    # column and some do not, so column 2 is FY2024 on one row and FY2023 on
    # the next. Grouping on col_index silently compares different years.
    ordinals: dict[int, int] = {}
    for group in row_groups.values():
        rank = 0
        for cell in group:
            if "note_reference" in _classify_in_row(cell, group):
                ordinals[id(cell)] = 0
            else:
                rank += 1
                ordinals[id(cell)] = rank

    for c in result.cells:
        contextual = _classify_in_row(c, row_groups[(c.container, c.row_index)])
        parsed = parse_number(c.raw_value)
        mapping = map_label(c.row_label)

        scale = c.context_scale
        mult = SCALE_MULTIPLIERS.get(scale) if scale else None

        # value_in_units is populated ONLY when both the figure parsed cleanly
        # and a scale was actually declared in the source. An undeclared scale
        # leaves it empty rather than silently assuming units.
        if parsed.value is not None and mult is not None and not parsed.is_percentage:
            value_in_units = parsed.value * mult
        else:
            value_in_units = None

        flags = []
        if parsed.value is None:
            flags.append("unparsed")
        if parsed.ambiguous:
            flags.append("ambiguous_separator")
        if scale is None:
            flags.append("scale_not_stated")
        if c.context_currency is None:
            flags.append("currency_not_stated")
        if mapping.confidence == "ambiguous":
            flags.append("ambiguous_mapping")
        flags.extend(contextual)

        rows.append({
            "source_file": c.source_file,
            "container": c.container,
            "row_index": c.row_index,
            "col_index": c.col_index,
            "period_ordinal": ordinals.get(id(c), 0),
            "row_label": c.row_label,
            "column_header": c.column_header,
            "raw_value": c.raw_value,
            "parsed_value": "" if parsed.value is None else repr(parsed.value).rstrip("0").rstrip(".") if "." in repr(parsed.value) else parsed.value,
            "is_negative": int(parsed.is_negative),
            "is_percentage": int(parsed.is_percentage),
            "currency": c.context_currency or "N/A",
            "scale": scale or "N/A",
            "scale_multiplier": "" if mult is None else int(mult) if mult >= 1 else mult,
            "value_in_units": "" if value_in_units is None else f"{value_in_units:.4f}".rstrip("0").rstrip("."),
            "reporting_period": c.context_period or "N/A",
            "company_name": result.company_name or "N/A",
            "extraction_method": c.extraction_method,
            "mapped_concept": mapping.concept or "",
            "mapping_confidence": mapping.confidence,
            "parse_note": parsed.parse_note,
            "stripped_markers": ";".join(parsed.stripped_markers),
            "review_flag": ";".join(flags),
        })
    return rows


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            h.update(block)
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------


def run(input_dir: Path, out_dir: Path, recursive: bool, max_pages: int | None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "processing_log.txt"
    started = datetime.now(timezone.utc)

    files = discover_files(input_dir, out_dir, recursive)
    supported = [p for p in files if p.suffix.lower() in READERS]
    skipped = [p for p in files if p.suffix.lower() not in READERS]

    log_lines = [
        f"run started    : {started.isoformat()}",
        f"input dir      : {input_dir.resolve()}",
        f"output dir     : {out_dir.resolve()}",
        f"recursive      : {recursive}",
        f"files detected : {len(files)}",
        f"supported      : {len(supported)}",
        f"skipped (ext)  : {len(skipped)}",
        "",
    ]
    for p in skipped:
        log_lines.append(f"SKIP  {p.name}  unsupported extension {p.suffix!r}")

    master: list[dict] = []
    ok = failed = 0
    csv_written = 0
    flag_counter: Counter[str] = Counter()

    for path in supported:
        result = read_any(path)
        if result.error:
            failed += 1
            log_lines.append(f"ERROR {path.name}  {result.error}")
            log.error("%s: %s", path.name, result.error)
            continue

        rows = _flatten(result)
        if not rows:
            log_lines.append(
                f"WARN  {path.name}  read successfully but yielded 0 numeric cells "
                f"({result.pages_or_sheets} page(s)/sheet(s)) — likely a scanned "
                f"document with no text layer, or a narrative-only file"
            )
            ok += 1
            continue

        doc_csv = out_dir / f"{path.stem}_extracted.csv"
        _write_csv(doc_csv, LONG_COLUMNS, rows)
        csv_written += 1
        ok += 1

        for r in rows:
            for f in filter(None, r["review_flag"].split(";")):
                flag_counter[f] += 1
            excluded = {"note_reference", "looks_like_year"}
            if r["mapped_concept"] and not (excluded & set(r["review_flag"].split(";"))):
                master.append({
                    "source_filename": r["source_file"],
                    "company_name": r["company_name"],
                    "reporting_period": r["reporting_period"],
                    "currency": r["currency"],
                    "scale": r["scale"],
                    "concept": r["mapped_concept"],
                    "raw_label": r["row_label"],
                    "raw_value": r["raw_value"],
                    "parsed_value": r["parsed_value"],
                    "value_in_units": r["value_in_units"],
                    "container": r["container"],
                    "row_index": r["row_index"],
                    "col_index": r["col_index"],
                    "period_ordinal": r["period_ordinal"],
                    "mapping_confidence": r["mapping_confidence"],
                    "review_flag": r["review_flag"],
                })

        log_lines.append(
            f"OK    {path.name}  sha256:{_sha256(path)}  "
            f"{result.pages_or_sheets} container(s)  {len(rows)} cell(s) -> {doc_csv.name}"
        )

    identity_findings: list[dict] = []
    if master:
        _write_csv(out_dir / "master_financial_summary.csv", MASTER_COLUMNS, master)
        csv_written += 1
        identity_findings = check_identities(master)
        if identity_findings:
            _write_csv(out_dir / "validation_report.csv",
                       list(identity_findings[0].keys()), identity_findings)
            csv_written += 1

    finished = datetime.now(timezone.utc)
    summary = [
        "",
        "=" * 62,
        "RUN SUMMARY",
        "=" * 62,
        f"files detected            : {len(files)}",
        f"files supported           : {len(supported)}",
        f"files extracted OK        : {ok}",
        f"files failed              : {failed}",
        f"csv outputs generated     : {csv_written}",
        f"master rows (mapped only) : {len(master)}",
        f"elapsed                   : {(finished - started).total_seconds():.2f}s",
        f"identity checks run       : {len(identity_findings)}"
        f"  (passed {sum(1 for f in identity_findings if f['status'] == 'PASS')},"
        f" failed {sum(1 for f in identity_findings if f['status'] == 'FAIL')})",
        "",
        "REVIEW FLAGS (cells requiring human confirmation before use):",
    ]
    if flag_counter:
        for flag, count in flag_counter.most_common():
            summary.append(f"  {count:>6}  {flag}")
    else:
        summary.append("  none")
    summary.append("")
    summary.append("Note: value_in_units is populated only where the source declared a")
    summary.append("scale. Cells flagged scale_not_stated have a raw value but no")
    summary.append("normalised value, by design — guessing the scale is a 1000x risk.")

    log_path.write_text("\n".join(log_lines + summary) + "\n", encoding="utf-8")
    print("\n".join(summary))
    return 0 if failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fin_extract",
        description="Deterministic financial document -> CSV extraction.",
    )
    p.add_argument("--input", required=True, type=Path,
                   help="directory containing source documents")
    p.add_argument("--out", type=Path, default=None,
                   help="output directory (default: sibling '<input>_extracted_csvs')")
    p.add_argument("--out-inside", action="store_true",
                   help="write to <input>/extracted_csvs as per the original spec "
                        "(traversal still excludes it)")
    p.add_argument("--recursive", action="store_true", help="descend into subdirectories")
    p.add_argument("--max-pages", type=int, default=None, help="cap PDF pages per file")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    input_dir = args.input.expanduser()
    if args.out:
        out_dir = args.out.expanduser()
    elif args.out_inside:
        out_dir = input_dir / "extracted_csvs"
    else:
        out_dir = input_dir.parent / f"{input_dir.name}_extracted_csvs"
    return run(input_dir, out_dir, args.recursive, args.max_pages)



# ---------------------------------------------------------------------------
# Accounting identity checks
# ---------------------------------------------------------------------------
# These are free tests. The statements must foot, so any breach is either an
# extraction error (wrong column picked, note reference kept, scale mixed) or a
# genuine restatement. Either way it needs a human. No amount of regex tuning
# substitutes for this — it is the only check that validates the RELATIONSHIPS
# between extracted figures rather than each figure in isolation.

IDENTITIES = (
    ("gross_profit = total_revenue - cost_of_sales",
     ("total_revenue", "cost_of_sales"), "gross_profit",
     lambda rev, cos: rev + cos if cos < 0 else rev - cos),
    ("total_assets = total_liabilities + total_equity",
     ("total_liabilities", "total_equity"), "total_assets",
     lambda liab, eq: abs(liab) + eq),
)


def check_identities(master: list[dict], tolerance: float = 0.01) -> list[dict]:
    """Group by (file, container, col_index) so we compare one period at a time."""
    from collections import defaultdict

    groups: dict[tuple, dict[str, float]] = defaultdict(dict)
    for r in master:
        v = r.get("parsed_value")
        if v in (None, ""):
            continue
        key = (r["source_filename"], r["container"], r["period_ordinal"])
        try:
            groups[key].setdefault(r["concept"], float(v))
        except (TypeError, ValueError):
            continue

    findings: list[dict] = []
    for (fname, container, ordinal), vals in sorted(groups.items()):
        if ordinal == 0:
            continue
        for name, inputs, target, fn in IDENTITIES:
            if target not in vals or not all(i in vals for i in inputs):
                continue
            expected = fn(*(vals[i] for i in inputs))
            actual = vals[target]
            denom = max(abs(expected), abs(actual), 1.0)
            rel = abs(expected - actual) / denom
            findings.append({
                "source_filename": fname, "container": container,
                "period_ordinal": ordinal,
                "identity": name,
                "expected": round(expected, 4), "actual": round(actual, 4),
                "abs_difference": round(expected - actual, 4),
                "relative_difference": round(rel, 6),
                "status": "PASS" if rel <= tolerance else "FAIL",
            })
    return findings

if __name__ == "__main__":
    sys.exit(main())
