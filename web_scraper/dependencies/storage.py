"""Filesystem layout, naming convention and the dedupe manifest."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from .taxonomy import DocType

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify_filename(name: str, max_len: int = 80) -> str:
    """Normalise an original filename for use inside our naming convention."""
    name = unquote(name)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    stem = Path(name).stem
    stem = _SAFE.sub("-", stem).strip("-_.")
    stem = re.sub(r"-{2,}", "-", stem)
    return (stem[:max_len] or "document").lower()


def original_filename_from_url(url: str) -> str:
    path = urlparse(url).path
    return Path(unquote(path)).name or "document.pdf"


def build_filename(
    ticker: str,
    year: int | None,
    doc_type: DocType,
    original: str,
    extension: str,
) -> str:
    """[TICKER]_[YEAR]_[DOC_TYPE]_[ORIGINAL_FILENAME].[ext]"""
    year_part = str(year) if year else "UNDATED"
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"{ticker.upper()}_{year_part}_{doc_type.value}_{slugify_filename(original)}{ext.lower()}"


def target_path(root: Path, ticker: str, filename: str) -> Path:
    directory = root / ticker.upper()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    content_hash   TEXT PRIMARY KEY,
    ticker         TEXT NOT NULL,
    doc_type       TEXT NOT NULL,
    year           INTEGER,
    year_confidence TEXT,
    source_url     TEXT NOT NULL,
    link_text      TEXT,
    stored_path    TEXT NOT NULL,
    byte_size      INTEGER,
    content_type   TEXT,
    classify_score INTEGER,
    ambiguous      INTEGER DEFAULT 0,
    runner_up      TEXT,
    fetched_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_ticker ON documents(ticker);
CREATE INDEX IF NOT EXISTS idx_docs_type   ON documents(ticker, doc_type, year);
CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_url ON documents(source_url);

CREATE TABLE IF NOT EXISTS visited_urls (
    url        TEXT PRIMARY KEY,
    ticker     TEXT,
    status     TEXT,
    note       TEXT,
    seen_at    TEXT NOT NULL
);
"""


@dataclass
class DocumentRecord:
    content_hash: str
    ticker: str
    doc_type: DocType
    year: int | None
    year_confidence: str
    source_url: str
    link_text: str
    stored_path: Path
    byte_size: int
    content_type: str
    classify_score: int
    ambiguous: bool
    runner_up: str


class Manifest:
    """SQLite-backed record of everything seen and everything stored.

    Two distinct dedupe checks, because they catch different things:
      - source_url  : the same link encountered twice in one crawl
      - content_hash: the same PDF served from two different URLs, which is
                      extremely common (IR site + SENS cloudlink + CDN mirror)
    """

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def has_url(self, url: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM documents WHERE source_url = ? LIMIT 1", (url,)
        ).fetchone()
        return row is not None

    def has_hash(self, content_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM documents WHERE content_hash = ? LIMIT 1", (content_hash,)
        ).fetchone()
        return row is not None

    def record(self, doc: DocumentRecord) -> bool:
        """Insert. Returns False when the content hash was already present."""
        with self._tx() as conn:
            try:
                conn.execute(
                    """INSERT INTO documents (
                        content_hash, ticker, doc_type, year, year_confidence,
                        source_url, link_text, stored_path, byte_size,
                        content_type, classify_score, ambiguous, runner_up, fetched_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        doc.content_hash,
                        doc.ticker.upper(),
                        doc.doc_type.value,
                        doc.year,
                        doc.year_confidence,
                        doc.source_url,
                        doc.link_text[:500],
                        str(doc.stored_path),
                        doc.byte_size,
                        doc.content_type,
                        doc.classify_score,
                        int(doc.ambiguous),
                        doc.runner_up,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_visited(self, url: str, ticker: str, status: str, note: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO visited_urls (url, ticker, status, note, seen_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET status=excluded.status,
                                                  note=excluded.note,
                                                  seen_at=excluded.seen_at""",
                (url, ticker, status, note, datetime.now(timezone.utc).isoformat()),
            )

    def coverage(self, ticker: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            """SELECT doc_type, year, COUNT(*) AS n
               FROM documents WHERE ticker = ?
               GROUP BY doc_type, year ORDER BY year DESC, doc_type""",
            (ticker.upper(),),
        ).fetchall()

    def ambiguous_documents(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT ticker, doc_type, runner_up, link_text, stored_path "
            "FROM documents WHERE ambiguous = 1 ORDER BY ticker"
        ).fetchall()

    def close(self) -> None:
        self._conn.close()
