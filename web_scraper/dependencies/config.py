"""Runtime configuration and the two lookup tables that carry the real load."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    tickers_file: Path = Path("tickers.txt")
    download_root: Path = Path("./downloads")
    state_dir: Path = Path("./.state")
    overrides_file: Path = Path("ir_overrides.json")

    start_year: int = 2023
    end_year: int = 2030

    # Politeness. These are not decoration - JSE IR sites are small estates
    # behind Cloudflare and will rate-limit or ban a fast crawler.
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 5.0
    per_host_concurrency: int = 2
    request_timeout_seconds: float = 60.0
    max_retries: int = 4
    backoff_base_seconds: float = 2.0

    respect_robots: bool = True
    max_file_bytes: int = 250 * 1024 * 1024  # skip absurd files
    headless: bool = True
    page_load_timeout_ms: int = 45_000

    # Depth of the in-site crawl from the IR landing page.
    max_crawl_depth: int = 2
    max_pages_per_site: int = 40

    contact_email: str = "you@example.com"  # goes in the UA string, see below

    def user_agent(self) -> str:
        """
        One honest, stable User-Agent - not a rotating pool.

        Rationale: rotating UAs while reusing one IP and one TLS fingerprint is
        strictly worse than a consistent identity. Modern bot detection
        fingerprints the TLS handshake and the JA3/JA4 hash; a Chrome UA on a
        Python httpx handshake is a louder signal than any UA you could pick.
        With Playwright you must keep the UA matched to the actual browser
        build or you break navigator.userAgentData consistency checks.

        If you are hammered by 403s, the fix is slower crawling and honest
        identification, not disguise.
        """
        return (
            "Mozilla/5.0 (compatible; JSEReportCollector/1.0; "
            f"academic research; +mailto:{self.contact_email})"
        )

    def ensure_dirs(self) -> None:
        self.download_root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Fiscal year ends - the join key nobody remembers to build
# ---------------------------------------------------------------------------
# Month number of the financial year end. A document labelled "2024" for
# Shoprite covers July 2023 - June 2024. If you later join extracted line items
# against calendar-quarter macro series without this table, every number is
# silently misaligned by up to six months.

FISCAL_YEAR_END: dict[str, int] = {
    "SHP": 6,   # Shoprite - 30 June (approx: nearest Sunday to 30 June)
    "SOL": 6,   # Sasol
    "BVT": 6,   # Bidvest
    "NPN": 3,   # Naspers
    "PRX": 3,   # Prosus
    "MTN": 12,  # MTN Group
    "SBK": 12,  # Standard Bank
    "FSR": 6,   # FirstRand
    "AGL": 12,  # Anglo American
    "IMP": 6,   # Impala Platinum
    "WHL": 6,   # Woolworths (approx: nearest Sunday to end June)
    "TFG": 3,   # The Foschini Group
    "CFR": 3,   # Richemont
    "APN": 6,   # Aspen
    "VOD": 3,   # Vodacom
    "CLS": 2,   # Clicks - end Aug... verify per company, do not trust blindly
}


# ---------------------------------------------------------------------------
# IR overrides - the file that makes this pipeline actually work
# ---------------------------------------------------------------------------
# Automated IR discovery from a bare ticker is unreliable (see discovery.py).
# Every ticker you care about should eventually end up here, seeded once by
# hand and then never guessed again. Discovery is the fallback, not the plan.

DEFAULT_OVERRIDES: dict[str, dict] = {
    "SHP": {
        "company_name": "Shoprite Holdings Limited",
        "ir_urls": ["https://www.shopriteholdings.co.za/investor-centre.html"],
    },
    "SOL": {
        "company_name": "Sasol Limited",
        "ir_urls": ["https://www.sasol.com/investor-centre"],
    },
    "MTN": {
        "company_name": "MTN Group Limited",
        "ir_urls": ["https://www.mtn.com/investors/"],
    },
}


def load_overrides(path: Path) -> dict[str, dict]:
    if not path.exists():
        return dict(DEFAULT_OVERRIDES)
    merged = dict(DEFAULT_OVERRIDES)
    merged.update(json.loads(path.read_text(encoding="utf-8")))
    return merged


# ---------------------------------------------------------------------------
# Aggregators used when a company's own site is unusable
# ---------------------------------------------------------------------------

SENS_PDF_HOST = "senspdf.jse.co.za"
JSE_SENS_INSTRUMENT_URL = "https://www.jse.co.za/notes/sens/{instrument_id}"


@dataclass
class TickerJob:
    ticker: str
    company_name: str | None = None
    ir_urls: list[str] = field(default_factory=list)
    jse_instrument_id: str | None = None
