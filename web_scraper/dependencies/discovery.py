"""
Resolve a JSE ticker to one or more Investor Relations landing pages.

Read this before trusting anything below
----------------------------------------
"Programmatically locate each company's official IR page" from a bare ticker
is the least reliable part of this pipeline, and no amount of code fixes it.
There is no South African EDGAR. The JSE publishes no documented developer API;
market data and issuer information sit behind the Client Portal, and third
parties (ShareData, Moneyweb, ProfileData) gate their archives behind login.

So discovery here is a *ranked cascade*, and each rung reports its own
confidence rather than pretending to certainty:

  1. Manual override        confidence 1.00  - use this. Seed it once per ticker.
  2. JSE issuer directory   confidence 0.75  - gives company name + website
  3. Homepage link probe    confidence 0.60  - crawl the corporate site for
                                               an "Investor" link
  4. Search engine guess    confidence 0.30  - last resort, verify before use

Rung 4 is where naive implementations of this spec live, and it is why they
fail silently: a search engine happily returns a PR agency page, a Wikipedia
article, or a *different company with the same abbreviation*. The pipeline
then downloads 40 documents belonging to the wrong entity and files them under
your ticker. Do not run rung 4 unattended - `main.py --strict` disables it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from .config import Settings, TickerJob

log = logging.getLogger(__name__)

IR_LINK_HINTS = re.compile(
    r"investor|shareholder|financial results|reporting suite|results (and|&) reports"
    r"|annual report|sens|debt investors|reports? (and|&) publications",
    re.I,
)

# Paths that are worth probing directly on a corporate domain before crawling.
COMMON_IR_PATHS = (
    "/investors", "/investor-relations", "/investor-centre", "/investor-center",
    "/investors/", "/en/investors", "/about-us/investor-relations",
    "/investor-relations/results-and-reports", "/investors/results-reports",
)


@dataclass
class DiscoveryResult:
    ticker: str
    ir_urls: list[str]
    company_name: str | None
    confidence: float
    method: str
    notes: str = ""

    @property
    def needs_review(self) -> bool:
        return self.confidence < 0.7


class IRDiscoverer:
    def __init__(self, settings: Settings, overrides: dict[str, dict], client: httpx.AsyncClient):
        self.settings = settings
        self.overrides = {k.upper(): v for k, v in overrides.items()}
        self.client = client

    async def resolve(self, ticker: str, allow_search: bool = False) -> DiscoveryResult:
        ticker = ticker.upper()

        # Rung 1 -------------------------------------------------------------
        entry = self.overrides.get(ticker)
        if entry and entry.get("ir_urls"):
            return DiscoveryResult(
                ticker=ticker,
                ir_urls=list(entry["ir_urls"]),
                company_name=entry.get("company_name"),
                confidence=1.0,
                method="override",
            )

        # Rung 2/3 -----------------------------------------------------------
        domain = (entry or {}).get("website")
        if domain:
            urls = await self._probe_domain(domain)
            if urls:
                return DiscoveryResult(
                    ticker, urls, (entry or {}).get("company_name"), 0.6, "domain_probe"
                )

        if not allow_search:
            return DiscoveryResult(
                ticker,
                [],
                (entry or {}).get("company_name"),
                0.0,
                "unresolved",
                notes=(
                    "No override for this ticker. Add it to ir_overrides.json "
                    "with the IR URL, or re-run with --allow-search (unverified)."
                ),
            )

        # Rung 4 -------------------------------------------------------------
        return DiscoveryResult(
            ticker,
            [],
            None,
            0.3,
            "search_stub",
            notes=(
                "Search-engine discovery is deliberately not implemented as an "
                "unattended path. Plug in an API you are licensed to use "
                "(Bing/Brave/SerpAPI), then have a human confirm the domain "
                "belongs to the issuer before the crawler is pointed at it."
            ),
        )

    async def _probe_domain(self, domain: str) -> list[str]:
        """Try known IR paths, then scan the homepage for an investor link."""
        base = domain if domain.startswith("http") else f"https://{domain}"
        found: list[str] = []

        for path in COMMON_IR_PATHS:
            url = urljoin(base, path)
            try:
                resp = await self.client.head(url, follow_redirects=True)
                if resp.status_code < 400:
                    found.append(str(resp.url))
                    break
            except httpx.HTTPError:
                continue
            await asyncio.sleep(self.settings.min_delay_seconds)

        if found:
            return found

        try:
            resp = await self.client.get(base, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("homepage probe failed for %s: %s", base, exc)
            return []

        tree = HTMLParser(resp.text)
        host = urlparse(str(resp.url)).netloc
        for node in tree.css("a[href]"):
            text = (node.text() or "").strip()
            href = node.attributes.get("href", "")
            if not IR_LINK_HINTS.search(f"{text} {href}"):
                continue
            absolute = urljoin(str(resp.url), href)
            if urlparse(absolute).netloc.endswith(host.replace("www.", "")):
                found.append(absolute)
        # De-duplicate, preserve order
        return list(dict.fromkeys(found))[:3]


def read_tickers(settings: Settings) -> list[TickerJob]:
    """Read tickers.txt. Blank lines and '#' comments ignored."""
    if not settings.tickers_file.exists():
        raise FileNotFoundError(f"tickers file not found: {settings.tickers_file}")
    jobs: list[TickerJob] = []
    for raw in settings.tickers_file.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            jobs.append(TickerJob(ticker=line.upper()))
    return jobs
