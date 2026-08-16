"""
Harvest candidate document links from JavaScript-rendered IR portals.

Why Playwright is genuinely needed here (and where it is not)
------------------------------------------------------------
Most SA IR portals are one of three shapes:

  (a) Static HTML list of PDF links      -> httpx + selectolax is enough
  (b) Angular/React accordion by year    -> needs a real browser; the year
                                            panels are collapsed and the hrefs
                                            only exist after a click
  (c) Third-party IR widget in an iframe -> needs a browser AND frame traversal
      (Investis, Q4 Inc, EQS, Sharedata embeds are all common on JSE sites)

This module handles all three. Shape (c) is the one people forget: the links
are not in the top-level document at all, so `page.query_selector_all("a")`
returns nothing and the crawler reports "no documents found" for a company
whose IR page is visibly full of PDFs. `_collect_from_all_frames` fixes that.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, Frame, Page, TimeoutError as PWTimeout, async_playwright

from .config import Settings
from .taxonomy import looks_downloadable

log = logging.getLogger(__name__)

# Buttons/tabs worth clicking to reveal collapsed year panels.
EXPANDERS = (
    "text=/^(20(2[3-9]|3[0-9]))$/",          # bare year tabs
    "text=/FY\\s?-?\\s?20?\\d{2}/i",
    "text=/load more|show more|view all|see all/i",
    "[aria-expanded='false']",
    ".accordion-toggle, .accordion-header, details > summary",
)

COOKIE_DISMISS = (
    "text=/reject (all )?(non[- ]essential|optional)?/i",
    "text=/only necessary|essential cookies only/i",
    "text=/^decline$/i",
)


@dataclass(frozen=True)
class CandidateLink:
    url: str
    text: str
    source_page: str

    def key(self) -> str:
        return self.url.split("#")[0]


class PortalHarvester:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> "PortalHarvester":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.settings.headless)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def harvest(self, start_urls: list[str], same_site_only: bool = True) -> list[CandidateLink]:
        """BFS crawl from the IR landing pages, collecting downloadable links."""
        assert self._browser is not None, "use PortalHarvester as an async context manager"

        context = await self._browser.new_context(
            user_agent=None,  # keep Chromium's own UA; see Settings.user_agent docstring
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=False,
        )
        # Block heavy assets - cuts crawl time roughly in half on image-led IR sites.
        await context.route(
            "**/*.{png,jpg,jpeg,gif,svg,webp,woff,woff2,ttf,mp4,webm}",
            lambda route: asyncio.ensure_future(route.abort()),
        )

        seen_pages: set[str] = set()
        candidates: dict[str, CandidateLink] = {}
        allowed_hosts = {urlparse(u).netloc for u in start_urls}
        queue: list[tuple[str, int]] = [(u, 0) for u in start_urls]

        page = await context.new_page()
        page.set_default_timeout(self.settings.page_load_timeout_ms)

        try:
            while queue and len(seen_pages) < self.settings.max_pages_per_site:
                url, depth = queue.pop(0)
                normalised = url.split("#")[0]
                if normalised in seen_pages:
                    continue
                seen_pages.add(normalised)

                try:
                    await page.goto(normalised, wait_until="domcontentloaded")
                except PWTimeout:
                    log.warning("timeout loading %s", normalised)
                    continue
                except Exception as exc:  # noqa: BLE001 - one bad page must not kill the run
                    log.warning("failed to load %s: %s", normalised, exc)
                    continue

                await self._dismiss_cookies(page)
                await self._expand_everything(page)

                links = await self._collect_from_all_frames(page)
                for href, text in links:
                    absolute = urljoin(page.url, href)
                    if looks_downloadable(absolute):
                        candidates.setdefault(
                            absolute.split("#")[0],
                            CandidateLink(absolute, text.strip(), page.url),
                        )
                    elif depth < self.settings.max_crawl_depth:
                        host = urlparse(absolute).netloc
                        if same_site_only and host not in allowed_hosts:
                            continue
                        if self._is_worth_following(absolute, text):
                            queue.append((absolute, depth + 1))

                await asyncio.sleep(self.settings.min_delay_seconds)
        finally:
            await context.close()

        log.info("harvested %d candidate links from %d pages", len(candidates), len(seen_pages))
        return list(candidates.values())

    # -- internals ---------------------------------------------------------

    async def _dismiss_cookies(self, page: Page) -> None:
        """Decline non-essential cookies where a control exists; never accept-all."""
        for selector in COOKIE_DISMISS:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=3_000)
                    return
            except Exception:  # noqa: BLE001
                continue

    async def _expand_everything(self, page: Page, rounds: int = 3) -> None:
        """Click year tabs / accordions repeatedly until nothing new appears."""
        for _ in range(rounds):
            clicked = 0
            for selector in EXPANDERS:
                try:
                    locator = page.locator(selector)
                    count = min(await locator.count(), 25)
                except Exception:  # noqa: BLE001
                    continue
                for index in range(count):
                    try:
                        item = locator.nth(index)
                        if await item.is_visible():
                            await item.click(timeout=2_000, no_wait_after=True)
                            clicked += 1
                    except Exception:  # noqa: BLE001
                        continue
            if not clicked:
                return
            await page.wait_for_timeout(800)

    async def _collect_from_all_frames(self, page: Page) -> list[tuple[str, str]]:
        """Collect (href, text) from the main document AND every child frame."""
        results: list[tuple[str, str]] = []
        frames: list[Frame] = list(page.frames)  # includes main frame
        for frame in frames:
            try:
                found = await frame.eval_on_selector_all(
                    "a[href], [data-href], [data-file], [data-url]",
                    """els => els.map(e => [
                        e.getAttribute('href') || e.getAttribute('data-href')
                          || e.getAttribute('data-file') || e.getAttribute('data-url') || '',
                        (e.innerText || e.textContent || '').trim().slice(0, 300)
                    ])""",
                )
                results.extend((h, t) for h, t in found if h)
            except Exception as exc:  # noqa: BLE001 - detached frames are normal
                log.debug("frame scrape skipped: %s", exc)
        return results

    @staticmethod
    def _is_worth_following(url: str, text: str) -> bool:
        blob = f"{url} {text}".lower()
        good = (
            "report", "result", "financial", "annual", "interim", "sens",
            "presentation", "publication", "debt", "investor", "archive",
            "20 23", "2023", "2024", "2025", "2026",
        )
        bad = ("mailto:", "javascript:", "/careers", "/contact", "/privacy", "login", "#")
        return any(g in blob for g in good) and not any(b in blob for b in bad)
