"""
SENS acquisition.

State of the world (verified August 2026)
-----------------------------------------
The JSE publishes no documented public developer API. Real-time SENS is a
commercial product delivered over the Regulatory News Gateway; end-of-day SENS
and SENS-plus are FTP file products off the Information Dissemination Portal,
both sold under a data licence. Third-party archives (ShareData, Moneyweb,
ProfileData) require an account and their terms generally prohibit bulk
scraping.

What is publicly reachable without a licence:

  1. `https://www.jse.co.za/notes/sens/{instrument_id}` - a per-instrument SENS
     list on the public site. It is JS-rendered, so it needs Playwright, and it
     is paginated with a limited retention window.
  2. `https://senspdf.jse.co.za/documents/{YEAR}/{ISSUER}/{...}/{DDMMYYYY}.pdf`
     - the cloudlink host that SENS announcements themselves reference when a
     company attaches full financial statements. These URLs appear *inside*
     announcement text, so you discover them by parsing announcements, not by
     guessing paths.
  3. The company's own "SENS" page on its IR site. For a per-ticker pipeline
     this is usually the best public source and it is what `harvest.py`
     already crawls.

Practical consequence: source (3) is the default and gives you good coverage
per company. Sources (1)/(2) supplement it. If you need complete, timestamped,
market-wide SENS history - which is what a serious event study needs - you
need a licensed feed. Building a scraper that pretends otherwise gives you a
dataset with survivorship and retention gaps you cannot see or correct for.

Check the JSE's terms and robots.txt before pointing anything here at volume.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

from playwright.async_api import TimeoutError as PWTimeout

from .config import JSE_SENS_INSTRUMENT_URL, SENS_PDF_HOST, Settings
from .harvest import CandidateLink, PortalHarvester

log = logging.getLogger(__name__)

SENS_PDF_RE = re.compile(
    rf"https?://{re.escape(SENS_PDF_HOST)}/documents/(?P<year>\d{{4}})/[^\s\"'<>)]+\.pdf",
    re.I,
)

SENS_DATE_RE = re.compile(r"\b(?P<d>\d{2})[-/](?P<m>\d{2})[-/](?P<y>20\d{2})\b")


@dataclass
class SensItem:
    headline: str
    url: str
    published: date | None
    source: str


def extract_sens_pdf_links(text: str) -> list[str]:
    """Pull senspdf.jse.co.za cloudlinks out of announcement body text."""
    return list(dict.fromkeys(m.group(0) for m in SENS_PDF_RE.finditer(text)))


def parse_sens_date(text: str) -> date | None:
    m = SENS_DATE_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
    except ValueError:
        return None


class JSESensSource:
    """Read the public per-instrument SENS list. Requires a known instrument id."""

    def __init__(self, settings: Settings, harvester: PortalHarvester):
        self.settings = settings
        self.harvester = harvester

    async def fetch_items(self, instrument_id: str, max_items: int = 200) -> list[SensItem]:
        assert self.harvester._browser is not None, "harvester not started"
        url = JSE_SENS_INSTRUMENT_URL.format(instrument_id=instrument_id)

        context = await self.harvester._browser.new_context(
            viewport={"width": 1440, "height": 900}
        )
        page = await context.new_page()
        page.set_default_timeout(self.settings.page_load_timeout_ms)
        items: list[SensItem] = []
        try:
            await page.goto(url, wait_until="networkidle")
            for _ in range(6):  # paginate
                try:
                    more = page.locator("text=/load more|view all|next/i").first
                    if await more.count() and await more.is_visible():
                        await more.click(timeout=3_000)
                        await page.wait_for_timeout(1_200)
                    else:
                        break
                except Exception:  # noqa: BLE001
                    break

            rows = await page.eval_on_selector_all(
                "a[href]",
                """els => els.map(e => [
                    e.href,
                    (e.innerText || '').trim(),
                    (e.closest('tr, li, .row')?.innerText || '').trim().slice(0, 400)
                ])""",
            )
            for href, text, context_text in rows:
                if ".pdf" not in href.lower() and "sens" not in href.lower():
                    continue
                items.append(
                    SensItem(
                        headline=text or context_text[:200],
                        url=href,
                        published=parse_sens_date(context_text),
                        source="jse_public_sens",
                    )
                )
                if len(items) >= max_items:
                    break
        except PWTimeout:
            log.warning("SENS page timed out for instrument %s", instrument_id)
        finally:
            await context.close()

        return items


def sens_items_to_candidates(items: list[SensItem]) -> list[CandidateLink]:
    return [
        CandidateLink(url=item.url, text=f"SENS {item.headline}", source_page="jse_sens")
        for item in items
    ]
