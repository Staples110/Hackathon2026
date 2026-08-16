"""CLI entry point. Two phases, run independently."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from .config import Settings, load_overrides
from .discovery import IRDiscoverer, read_tickers
from .download import Downloader, build_client, write_atomic
from .harvest import CandidateLink, PortalHarvester
from .parser import FinancialDocParser
from .storage import (
    DocumentRecord,
    Manifest,
    build_filename,
    original_filename_from_url,
    target_path,
)
from .taxonomy import DocType, classify, extract_year

log = logging.getLogger("jse_reports")


# ---------------------------------------------------------------------------
# Phase 1
# ---------------------------------------------------------------------------
async def run_download(settings: Settings, allow_search: bool, only: set[str] | None) -> None:
    settings.ensure_dirs()
    overrides = load_overrides(settings.overrides_file)
    manifest = Manifest(settings.state_dir / "manifest.sqlite")
    jobs = read_tickers(settings)
    if only:
        jobs = [j for j in jobs if j.ticker in only]

    client = await build_client(settings)
    downloader = Downloader(settings, client)
    discoverer = IRDiscoverer(settings, overrides, client)

    unresolved: list[str] = []
    
    # Bounded concurrency controls
    max_concurrent_tickers = 4
    max_concurrent_downloads = 8
    ticker_sem = asyncio.Semaphore(max_concurrent_tickers)
    download_sem = asyncio.Semaphore(max_concurrent_downloads)

    async def _process_candidate_bounded(ticker: str, candidate: CandidateLink) -> bool:
        async with download_sem:
            return await _process_candidate(
                ticker, candidate, settings, downloader, manifest
            )

    async def _process_job(harvester: PortalHarvester, job) -> None:
        async with ticker_sem:
            discovery = await discoverer.resolve(job.ticker, allow_search=allow_search)
            if not discovery.ir_urls:
                log.error("[%s] unresolved: %s", job.ticker, discovery.notes)
                unresolved.append(job.ticker)
                return
            
            if discovery.needs_review:
                log.warning(
                    "[%s] IR page found via %s (confidence %.2f) - VERIFY before trusting: %s",
                    job.ticker, discovery.method, discovery.confidence, discovery.ir_urls,
                )

            candidates = await harvester.harvest(discovery.ir_urls)
            log.info("[%s] %d candidate links", job.ticker, len(candidates))

            # Concurrently download all candidates for this ticker
            results = await asyncio.gather(
                *[_process_candidate_bounded(job.ticker, candidate) for candidate in candidates],
                return_exceptions=True,
            )

            stored = sum(1 for r in results if r is True)
            skipped = sum(1 for r in results if r is False)
            
            log.info("[%s] stored %d, skipped %d", job.ticker, stored, skipped)
            _report_coverage(job.ticker, manifest, settings)

    try:
        async with PortalHarvester(settings) as harvester:
            # Concurrently process all jobs subject to max_concurrent_tickers
            await asyncio.gather(*[_process_job(harvester, job) for job in jobs])
    finally:
        await client.aclose()
        ambiguous = manifest.ambiguous_documents()
        if ambiguous:
            log.warning("%d documents classified ambiguously - review these:", len(ambiguous))
            for row in ambiguous[:20]:
                log.warning(
                    "  %s  %s (runner-up %s)  %s",
                    row["ticker"], row["doc_type"], row["runner_up"], row["link_text"][:80],
                )
        if unresolved:
            log.error(
                "No IR page for: %s. Add them to %s.",
                ", ".join(unresolved), settings.overrides_file,
            )
        manifest.close()

async def run_download_(settings: Settings, allow_search: bool, only: set[str] | None) -> None:
    settings.ensure_dirs()
    overrides = load_overrides(settings.overrides_file)
    manifest = Manifest(settings.state_dir / "manifest.sqlite")
    jobs = read_tickers(settings)
    if only:
        jobs = [j for j in jobs if j.ticker in only]

    client = await build_client(settings)
    downloader = Downloader(settings, client)
    discoverer = IRDiscoverer(settings, overrides, client)

    unresolved: list[str] = []

    try:
        async with PortalHarvester(settings) as harvester:
            for job in jobs:
                discovery = await discoverer.resolve(job.ticker, allow_search=allow_search)
                if not discovery.ir_urls:
                    log.error("[%s] unresolved: %s", job.ticker, discovery.notes)
                    unresolved.append(job.ticker)
                    continue
                if discovery.needs_review:
                    log.warning(
                        "[%s] IR page found via %s (confidence %.2f) - VERIFY before trusting: %s",
                        job.ticker, discovery.method, discovery.confidence, discovery.ir_urls,
                    )

                candidates = await harvester.harvest(discovery.ir_urls)
                log.info("[%s] %d candidate links", job.ticker, len(candidates))

                stored = skipped = 0
                for candidate in candidates:
                    if await _process_candidate(
                        job.ticker, candidate, settings, downloader, manifest
                    ):
                        stored += 1
                    else:
                        skipped += 1

                log.info("[%s] stored %d, skipped %d", job.ticker, stored, skipped)
                _report_coverage(job.ticker, manifest, settings)
    finally:
        await client.aclose()
        ambiguous = manifest.ambiguous_documents()
        if ambiguous:
            log.warning("%d documents classified ambiguously - review these:", len(ambiguous))
            for row in ambiguous[:20]:
                log.warning(
                    "  %s  %s (runner-up %s)  %s",
                    row["ticker"], row["doc_type"], row["runner_up"], row["link_text"][:80],
                )
        if unresolved:
            log.error(
                "No IR page for: %s. Add them to %s.",
                ", ".join(unresolved), settings.overrides_file,
            )
        manifest.close()


async def _process_candidate(
    ticker: str,
    candidate: CandidateLink,
    settings: Settings,
    downloader: Downloader,
    manifest: Manifest,
) -> bool:
    if manifest.has_url(candidate.url):
        return False

    original = original_filename_from_url(candidate.url)
    classification = classify(candidate.text, candidate.url, original)
    if classification.doc_type is DocType.UNKNOWN:
        manifest.mark_visited(candidate.url, ticker, "skipped", "unclassified")
        return False

    year_guess = extract_year(candidate.text, candidate.url, original)
    if year_guess.year and not (settings.start_year <= year_guess.year <= settings.end_year):
        manifest.mark_visited(candidate.url, ticker, "skipped", f"year {year_guess.year} out of range")
        return False

    result = await downloader.fetch(candidate.url)
    if not result.ok or result.content is None:
        manifest.mark_visited(candidate.url, ticker, "failed", result.reason)
        log.debug("[%s] fetch failed %s: %s", ticker, candidate.url, result.reason)
        return False

    from .storage import sha256_bytes

    content_hash = sha256_bytes(result.content)
    if manifest.has_hash(content_hash):
        manifest.mark_visited(candidate.url, ticker, "duplicate", "content hash already stored")
        return False

    filename = build_filename(
        ticker, year_guess.year, classification.doc_type, original, result.detected_extension
    )
    path = target_path(settings.download_root, ticker, filename)
    write_atomic(path, result.content)

    manifest.record(
        DocumentRecord(
            content_hash=content_hash,
            ticker=ticker,
            doc_type=classification.doc_type,
            year=year_guess.year,
            year_confidence=year_guess.confidence,
            source_url=candidate.url,
            link_text=candidate.text,
            stored_path=path,
            byte_size=len(result.content),
            content_type=result.content_type,
            classify_score=classification.score,
            ambiguous=classification.is_ambiguous,
            runner_up=classification.runner_up.value,
        )
    )
    manifest.mark_visited(candidate.url, ticker, "stored", str(path))
    return True


def _report_coverage(ticker: str, manifest: Manifest, settings: Settings) -> None:
    """Say what is MISSING, not just what was fetched. Silence is the enemy."""
    have = {(row["doc_type"], row["year"]) for row in manifest.coverage(ticker)}
    expected_types = (DocType.INTEGRATED_ANNUAL_REPORT, DocType.ANNUAL_FINANCIAL_STATEMENTS,
                      DocType.INTERIM_RESULTS)
    gaps = [
        f"{dt.value}/{year}"
        for year in range(settings.start_year, settings.end_year + 1)
        for dt in expected_types
        if (dt.value, year) not in have and year <= 2026
    ]
    if gaps:
        log.warning("[%s] coverage gaps: %s", ticker, ", ".join(gaps[:15]))


# ---------------------------------------------------------------------------
# Phase 2
# ---------------------------------------------------------------------------


def run_extract(settings: Settings, ticker: str | None, output: Path) -> None:
    root = settings.download_root / ticker.upper() if ticker else settings.download_root
    payload = []
    for pdf_path in sorted(root.rglob("*.pdf")):
        try:
            report = FinancialDocParser(pdf_path).parse()
        except Exception as exc:  # noqa: BLE001
            log.error("parse failed %s: %s", pdf_path.name, exc)
            continue

        payload.append(
            {
                "file": str(pdf_path),
                "pages": report.page_count,
                "statement_pages": {k.value: v for k, v in report.statement_pages.items()},
                "scale_by_page": report.scale_by_page,
                "line_items": [
                    {
                        "concept": li.concept,
                        "label": li.matched_label,
                        "value": li.value,
                        "scale": li.scale_label,
                        "scaled_value": li.scaled_value,
                        "page": li.page_number,
                        "column": li.column_index,
                        "confidence": li.confidence,
                        "warnings": li.warnings,
                        "raw": li.raw_row,
                    }
                    for li in report.line_items
                ],
                "notes": [
                    {"note": n.note_key, "page": n.page_number, "excerpt": n.excerpt[:800]}
                    for n in report.note_hits
                ],
            }
        )
        low = report.low_confidence()
        log.info(
            "%s: %d line items (%d low-confidence), %d note hits",
            pdf_path.name, len(report.line_items), len(low), len(report.note_hits),
        )

    output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log.info("wrote %s", output)


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jse-reports")
    parser.add_argument("--tickers", type=Path, default=Path("tickers.txt"))
    parser.add_argument("--downloads", type=Path, default=Path("./downloads"))
    parser.add_argument("--overrides", type=Path, default=Path("ir_overrides.json"))
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    dl = sub.add_parser("download", help="Phase 1: locate, classify and download")
    dl.add_argument("--allow-search", action="store_true",
                    help="Permit unverified search-engine IR discovery (not recommended)")
    dl.add_argument("--only", nargs="*", help="Restrict to these tickers")
    dl.add_argument("--no-headless", action="store_true")

    ex = sub.add_parser("extract", help="Phase 2: parse downloaded PDFs")
    ex.add_argument("--ticker")
    ex.add_argument("--out", type=Path, default=Path("extracted.json"))

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    settings = Settings(
        tickers_file=args.tickers,
        download_root=args.downloads,
        overrides_file=args.overrides,
    )

    if args.command == "download":
        settings.headless = not args.no_headless
        asyncio.run(run_download(settings, args.allow_search, set(args.only or []) or None))
    else:
        run_extract(settings, args.ticker, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
