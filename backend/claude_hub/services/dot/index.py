"""Derived SQLite FTS5 search index over Dot memory cards.

The index is disposable: it is rebuilt from card file stamps on demand, so
hand edits to card files are picked up on the next search.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TypeVar

from .lessons import STOP_WORDS
from .store import Card, DotStore

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    path TEXT PRIMARY KEY,
    mtime NOT NULL,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    importance INTEGER NOT NULL,
    archived INTEGER NOT NULL,
    updated TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
    path UNINDEXED, title, summary, tags, body, tokenize = 'trigram'
);
"""
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_MAX_TERMS = 24
_SETTLE_NS = 2_000_000_000

_T = TypeVar("_T")


@dataclass
class SearchHit:
    id: str
    kind: str
    title: str
    summary: str
    importance: int
    updated: str
    score: float


def query_terms(text: str) -> list[str]:
    """Split free text into trigram-searchable terms (CJK runs become 3-grams)."""
    terms: list[str] = []
    seen: set[str] = set()
    for word in _WORD_RE.findall(text.lower()):
        if _CJK_RE.search(word):
            pieces = [word[i : i + 3] for i in range(0, max(len(word) - 2, 1), 2)]
        else:
            pieces = [word]
        for piece in pieces:
            # Common words match nearly every card and only push real hits out.
            if len(piece) < 3 or piece in seen or piece in STOP_WORDS:
                continue
            seen.add(piece)
            terms.append(piece)
            if len(terms) >= _MAX_TERMS:
                return terms
    return terms


def recallable(text: str) -> bool:
    """Worth an automatic recall: a real search term, or CJK (short words need LIKE).

    "hi" or "ok" alone would only match cards by accident ("this", "look").
    """
    return bool(query_terms(text)) or bool(_CJK_RE.search(text))


class DotIndex:
    """Keep ``index.sqlite`` in step with the card files and search it."""

    def __init__(self, store: DotStore) -> None:
        self.store = store
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.store.ensure()
        return self._open()

    def _healing(self, run: Callable[[], _T]) -> _T:
        """Run ``run``; on a damaged index (anywhere in the file) drop it and run again."""
        try:
            return run()
        except sqlite3.OperationalError:
            raise
        except sqlite3.DatabaseError:
            # Not a database or a damaged page: the index is rebuilt from the cards.
            logger.warning("dot: index.sqlite is broken; rebuilding it")
            with self._lock:
                for suffix in ("", "-journal", "-wal", "-shm"):
                    Path(f"{self.store.index_path}{suffix}").unlink(missing_ok=True)
            return run()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.store.index_path), timeout=10)
        try:
            conn.executescript(_SCHEMA)
        except BaseException:
            conn.close()
            raise
        return conn

    def sync(self) -> int:
        """Re-index changed card files; drop removed ones. Returns the change count."""
        return self._healing(self._sync)

    def _sync(self) -> int:
        with self._lock, closing(self._connect()) as conn, conn:
            known = {row[0]: row[1] for row in conn.execute("SELECT path, mtime FROM cards")}
            changes = 0
            seen: set[str] = set()
            for path in self.store.iter_card_paths():
                key = str(path.relative_to(self.store.home))
                seen.add(key)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
                if known.get(key) == stamp:
                    continue
                card = self.store.read_card_path(path)
                conn.execute("DELETE FROM cards WHERE path = ?", (key,))
                conn.execute("DELETE FROM cards_fts WHERE path = ?", (key,))
                if card is not None:
                    # A file written within the clock's resolution can change again
                    # with the same stamp; such a file is read again next time.
                    settled = time.time_ns() - stat.st_mtime_ns > _SETTLE_NS
                    self._insert(conn, key, stamp if settled else "", card)
                changes += 1
            for key in set(known) - seen:
                conn.execute("DELETE FROM cards WHERE path = ?", (key,))
                conn.execute("DELETE FROM cards_fts WHERE path = ?", (key,))
                changes += 1
            return changes

    @staticmethod
    def _insert(conn: sqlite3.Connection, key: str, stamp: str, card: Card) -> None:
        conn.execute(
            "INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                stamp,
                card.id,
                card.kind,
                card.title,
                card.summary,
                card.importance,
                int(card.archived),
                card.updated,
            ),
        )
        conn.execute(
            "INSERT INTO cards_fts (path, title, summary, tags, body) VALUES (?, ?, ?, ?, ?)",
            (key, card.title, card.summary, " ".join(card.tags), card.body),
        )

    def search(
        self,
        text: str,
        *,
        kind: Optional[str] = None,
        limit: int = 8,
        include_archived: bool = False,
    ) -> list[SearchHit]:
        return self._healing(lambda: self._search(text, kind, limit, include_archived))

    def _search(
        self, text: str, kind: Optional[str], limit: int, include_archived: bool
    ) -> list[SearchHit]:
        self._sync()
        terms = query_terms(text)
        filters = []
        params: list[object] = []
        if kind:
            filters.append("c.kind = ?")
            params.append(kind)
        if not include_archived:
            filters.append("c.archived = 0")
        where = (" AND " + " AND ".join(filters)) if filters else ""
        with closing(self._connect()) as conn:
            if terms:
                match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                rows = conn.execute(
                    "SELECT c.id, c.kind, c.title, c.summary, c.importance, c.updated, "
                    "bm25(cards_fts, 0.0, 5.0, 3.0, 2.0, 1.0) AS score "
                    "FROM cards_fts JOIN cards c ON c.path = cards_fts.path "
                    f"WHERE cards_fts MATCH ?{where} "
                    "ORDER BY score - c.importance * 0.2 LIMIT ?",
                    [match, *params, limit],
                ).fetchall()
            else:
                needle = text.strip()
                if not needle:
                    return []
                rows = conn.execute(
                    "SELECT c.id, c.kind, c.title, c.summary, c.importance, c.updated, 0.0 "
                    "FROM cards c WHERE (c.title LIKE ? OR c.summary LIKE ?)"
                    f"{where} ORDER BY c.importance DESC, c.updated DESC LIMIT ?",
                    [f"%{needle}%", f"%{needle}%", *params, limit],
                ).fetchall()
        return [
            SearchHit(
                id=row[0],
                kind=row[1],
                title=row[2],
                summary=row[3],
                importance=int(row[4]),
                updated=row[5],
                score=float(row[6]),
            )
            for row in rows
        ]

    def count(self) -> int:
        return self._healing(self._count)

    def _count(self) -> int:
        self._sync()
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
