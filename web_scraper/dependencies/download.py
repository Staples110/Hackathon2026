"""
Fetching layer: robots compliance, per-host rate limiting, retries, dedupe.

The check most scrapers skip
---------------------------
`_validate_payload`. IR portals routinely return HTTP 200 with an HTML login
wall, a Cloudflare challenge, or a "document not available" page while the URL
still ends in `.pdf`. Without a magic-byte check you end up with a directory of
files named `SHP_2024_AFS_annual-report.pdf` that are actually 4 KB of HTML,
and you only discover it in Phase 2 when pdfplumber throws on all of them.
Verify the bytes, not the extension.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .config import Settings
from .storage import sha256_bytes

log = logging.getLogger(__name__)

MAGIC = {
    b"%PDF": ".pdf",
    b"PK\x03\x04": ".zip",      # also .docx/.xlsx/.pptx - refined below
    b"\xd0\xcf\x11\xe0": ".doc",  # legacy OLE: .doc/.xls/.ppt
}


@dataclass
class FetchResult:
    ok: bool
    content: bytes | None
    content_type: str
    detected_extension: str
    status: int
    reason: str = ""


class RateLimiter:
    """One token bucket per host, plus a concurrency cap per host."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._last_hit: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Semaphore] = {}
        self._guard = asyncio.Lock()

    async def acquire(self, url: str) -> asyncio.Semaphore:
        host = urlparse(url).netloc
        async with self._guard:
            sem = self._locks.setdefault(
                host, asyncio.Semaphore(self.settings.per_host_concurrency)
            )
        await sem.acquire()

        delay = random.uniform(
            self.settings.min_delay_seconds, self.settings.max_delay_seconds
        )
        elapsed = time.monotonic() - self._last_hit[host]
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last_hit[host] = time.monotonic()
        return sem


class RobotsCache:
    def __init__(self, client: httpx.AsyncClient, user_agent: str):
        self.client = client
        self.user_agent = user_agent
        self._cache: dict[str, RobotFileParser | None] = {}

    async def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        if root not in self._cache:
            self._cache[root] = await self._load(root)
        parser = self._cache[root]
        if parser is None:
            return True  # no robots.txt served -> not a prohibition
        return parser.can_fetch(self.user_agent, url)

    async def _load(self, root: str) -> RobotFileParser | None:
        try:
            resp = await self.client.get(f"{root}/robots.txt", timeout=15.0)
            if resp.status_code >= 400:
                return None
            parser = RobotFileParser()
            parser.parse(resp.text.splitlines())
            return parser
        except httpx.HTTPError:
            return None


class Downloader:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.limiter = RateLimiter(settings)
        self.robots = RobotsCache(client, settings.user_agent())

    async def fetch(self, url: str) -> FetchResult:
        if self.settings.respect_robots and not await self.robots.allowed(url):
            return FetchResult(False, None, "", "", 0, "blocked by robots.txt")

        sem = await self.limiter.acquire(url)
        try:
            return await self._fetch_with_retries(url)
        finally:
            sem.release()

    async def _fetch_with_retries(self, url: str) -> FetchResult:
        last_reason = ""
        for attempt in range(1, self.settings.max_retries + 1):
            try:
                resp = await self.client.get(url, follow_redirects=True)
            except httpx.HTTPError as exc:
                last_reason = f"transport error: {exc}"
                await self._backoff(attempt)
                continue

            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                retry_after = resp.headers.get("retry-after")
                wait = float(retry_after) if (retry_after or "").isdigit() else None
                last_reason = f"HTTP {resp.status_code}"
                await self._backoff(attempt, override=wait)
                continue

            if resp.status_code >= 400:
                return FetchResult(False, None, "", "", resp.status_code, f"HTTP {resp.status_code}")

            content = resp.content
            if len(content) > self.settings.max_file_bytes:
                return FetchResult(False, None, "", "", resp.status_code, "file too large")

            ctype = resp.headers.get("content-type", "").split(";")[0].strip()
            ok, ext, reason = self._validate_payload(content, ctype, url)
            return FetchResult(ok, content if ok else None, ctype, ext, resp.status_code, reason)

        return FetchResult(False, None, "", "", 0, last_reason or "exhausted retries")

    async def _backoff(self, attempt: int, override: float | None = None) -> None:
        wait = override if override is not None else (
            self.settings.backoff_base_seconds ** attempt + random.uniform(0, 1.5)
        )
        log.debug("backing off %.1fs (attempt %d)", wait, attempt)
        await asyncio.sleep(min(wait, 120.0))

    @staticmethod
    def _validate_payload(content: bytes, content_type: str, url: str) -> tuple[bool, str, str]:
        """Confirm the bytes match the promised type. Returns (ok, ext, reason)."""
        head = content[:8]

        for magic, ext in MAGIC.items():
            if head.startswith(magic):
                if ext == ".zip":
                    # OOXML containers start with the same PK header.
                    lowered = url.lower()
                    for candidate in (".pptx", ".xlsx", ".docx"):
                        if candidate in lowered:
                            return True, candidate, ""
                return True, ext, ""

        lowered_head = content[:512].lower()
        if b"<html" in lowered_head or b"<!doctype html" in lowered_head:
            marker = (
                "login wall / challenge page"
                if any(t in lowered_head for t in (b"login", b"sign in", b"cloudflare", b"captcha"))
                else "HTML returned where a document was expected"
            )
            return False, ".html", marker

        if content_type in {"application/pdf", "application/octet-stream"} and len(content) > 1024:
            return True, ".pdf", "content-type trusted, no magic bytes"

        return False, "", f"unrecognised payload (content-type={content_type!r})"


async def build_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": settings.user_agent(),
            "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-ZA,en;q=0.9",
        },
        timeout=httpx.Timeout(settings.request_timeout_seconds),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        follow_redirects=True,
    )


def write_atomic(path: Path, content: bytes) -> str:
    """Write to a temp file then rename, so a killed run leaves no half files."""
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(content)
    tmp.replace(path)
    return sha256_bytes(content)
