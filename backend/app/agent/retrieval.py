from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
import tokenize
from array import array
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.app.config import settings
from backend.app.graph.models import GraphNode
from backend.app.graph.store import AnalysisSession, GraphQueryService
from backend.app.indexing import RepositorySnapshot, repository_snapshot
from backend.app.observability import log_event

# Matches identifier-like words: an ASCII letter/underscore followed by one or
# more ASCII letters/digits/underscores (i.e. any code-identifier-shaped token
# of length >= 2). Used to tokenize both queries and chunk text for lexical
# matching/scoring.
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
_QUERY_STOPWORDS = {
    "about", "after", "also", "and", "are", "become", "can", "does",
    "explain", "for", "from", "give", "how", "into", "its", "main", "of",
    "our", "the", "their", "then", "through", "to", "used", "what", "when",
    "where", "which", "why", "with",
}
logger = logging.getLogger(__name__)
INDEX_SCHEMA_VERSION = "3"  # bump to force every existing revision_id to change and rebuild


class LocalCodeVectorizer:
    """Deterministic sparse subword vectors for local fuzzy code retrieval.

    Turns arbitrary text into a fixed-dimension (768-bucket) sparse
    bag-of-subwords vector via feature hashing - a lightweight, dependency-free
    substitute for a real embedding model. Used by PersistentCodeIndex
    (ensure_vectors/vector_search) to build and compare per-chunk vectors, which
    RepositoryRetrievalIndex.search then blends with lexical FTS results. Exists
    so Waypoint's "semantic" search works entirely offline, without calling an
    external embeddings API.
    """

    dimensions = 768

    @classmethod
    def vectorize(cls, text: str) -> dict[int, float]:
        """Convert text into a normalized sparse subword-hash vector.

        Input: text - any string (source code, a chunk's contextual text plus
        content, or a user query). Tokenizes into identifier-like words, splits
        each into camelCase/snake_case subwords, adds a few cheap stemmed variants
        and character 3-grams as extra fuzzy-match features, hashes every feature
        into one of `dimensions` buckets, and L2-normalizes the resulting count
        vector (so `similarity` below reduces to cosine similarity). Called by
        PersistentCodeIndex.ensure_vectors (to vectorize each stored chunk) and
        PersistentCodeIndex.vector_search (to vectorize the incoming query) - this
        is Waypoint's stand-in for a real embedding model.
        """
        features: dict[int, float] = {}
        raw_words = _TOKEN.findall(text)
        words: list[str] = []
        for raw in raw_words:
            words.append(raw.lower())
            words.extend(
                part.lower()
                # Splits one identifier into camelCase/PascalCase/snake_case subwords:
                # a capitalized-or-lowercase run, OR a run of capitals not followed by
                # a lowercase letter (an acronym), OR a run of digits.
                for part in re.findall(
                    r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|[0-9]+",
                    raw.replace("_", " "),
                )
                if len(part) > 1
            )
        for word in words:
            variants = {word}
            # Cheap suffix-stripping "stemming": only strip a suffix when enough of
            # the word remains (len > suffix + 3) so short words aren't mangled.
            for suffix in ("ing", "tion", "ment", "ers", "ies", "ed", "es", "s"):
                if word.endswith(suffix) and len(word) > len(suffix) + 3:
                    variants.add(word[: -len(suffix)])
            padded = f"^{word}$"
            # Character 3-grams over the boundary-padded word give partial/fuzzy
            # matches (typos, partial identifiers) some signal beyond exact words.
            variants.update(
                padded[index:index + 3] for index in range(len(padded) - 2)
            )
            for feature in variants:
                # Feature hashing: map each textual feature to one of a fixed 768
                # buckets so the vector space never grows with vocabulary size.
                bucket = int.from_bytes(
                    hashlib.blake2b(
                        feature.encode("utf-8"), digest_size=4
                    ).digest(),
                    "big",
                ) % cls.dimensions
                features[bucket] = features.get(bucket, 0.0) + 1.0
        norm = math.sqrt(sum(value * value for value in features.values())) or 1.0
        return {key: value / norm for key, value in features.items()}

    @staticmethod
    def similarity(left: dict[int, float], right: dict[int, float]) -> float:
        """Compute the (cosine, since inputs are L2-normalized) similarity of two sparse vectors.

        Input: left, right - sparse vectors as returned by `vectorize` (dict of
        bucket index -> weight). Iterates over the smaller dict for efficiency and
        sums the dot product. Called by PersistentCodeIndex.vector_search to score
        every stored chunk vector against the query vector.
        """
        if len(left) > len(right):
            left, right = right, left
        return sum(value * right.get(key, 0.0) for key, value in left.items())


class PersistentCodeIndex:
    """SQLite persistence for revisioned chunks, symbols, edges, and FTS search.

    Instantiated per RepositoryRetrievalIndex (and directly by
    RepositoryIndexStore.rebuild) at `settings.state_path`. Is the on-disk
    backing store that lets a repository's search index survive process restarts
    and be reused, keyed by a content fingerprint, without re-parsing or
    re-chunking unchanged repositories.
    """

    def __init__(self, path: Path) -> None:
        """Resolve the database file path and set up the lock guarding every connection."""
        self.path = path.resolve()
        self._lock = threading.RLock()
        self._local = threading.local()
        self._vector_cache: dict[
            str, list[tuple[str, array[int], array[float]]]
        ] = {}

    def _connect(self) -> sqlite3.Connection:
        """Return this thread's cached SQLite connection, opening (and initializing) it on
        first use.

        Kept per-thread (via `threading.local`) instead of reopened on every call: FastAPI
        dispatches these synchronous store methods across a small pool of worker threads,
        so a thread-local connection is never touched by more than one thread at a time,
        while still being reused across many requests handled by that thread - unlike a
        fresh open-then-close per call, which forces SQLite to checkpoint its WAL (fold
        pending writes back into the main file) on every single call, since that
        connection was the last one open. Against `settings.state_path` - a file shared by
        every store in the app and, over a long dev session, potentially large - that
        checkpoint was costing seconds per request. Enables row_factory for dict-like row
        access, enforces foreign keys, sets WAL journal mode so readers don't block the
        writer, and runs `_initialize()` once (rather than on every call, as before) since
        it only needs to run once per connection. Called at the start of every public
        method on this class; see `close()` for the matching teardown a test needs before
        deleting the underlying file.
        """
        with self._lock:
            connection = getattr(self._local, "connection", None)
            if connection is not None:
                return connection
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=20)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            self._initialize(connection)
            self._local.connection = connection
            return connection

    def close(self) -> None:
        """Close and discard this thread's cached connection, if any.

        The process never needs to call this - a persistent connection is meant to
        outlive requests. It exists for tests that construct a store against a
        temporary directory and then delete that directory immediately afterward: on
        Windows, deleting a file while this store still holds it open raises
        PermissionError, so those tests must close the store first.
        """
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    def _initialize(self, connection: sqlite3.Connection) -> None:
        """Idempotently create every table/index/FTS5 table this index needs.

        Input: connection - an open sqlite3.Connection. Uses `CREATE TABLE IF NOT
        EXISTS` throughout so it is safe to call on every connection regardless of
        whether the schema already exists. Called first thing inside every public
        method on this class, guaranteeing the schema exists no matter the call
        order or whether the database file is brand new.
        """
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS code_repositories (
                id TEXT PRIMARY KEY,
                canonical_root TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS code_revisions (
                id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                analysis_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('building', 'complete', 'failed')),
                indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(repository_id, fingerprint),
                FOREIGN KEY (repository_id) REFERENCES code_repositories(id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS code_files (
                revision_id TEXT NOT NULL,
                path TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                PRIMARY KEY (revision_id, path),
                FOREIGN KEY (revision_id) REFERENCES code_revisions(id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS code_file_aliases (
                revision_id TEXT NOT NULL,
                path TEXT NOT NULL,
                canonical_path TEXT NOT NULL,
                reason TEXT NOT NULL,
                target_kind TEXT NOT NULL,
                PRIMARY KEY (revision_id, path),
                FOREIGN KEY (revision_id) REFERENCES code_revisions(id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS code_symbols (
                revision_id TEXT NOT NULL,
                id TEXT NOT NULL,
                path TEXT,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                signature TEXT,
                start_line INTEGER,
                end_line INTEGER,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY (revision_id, id),
                FOREIGN KEY (revision_id) REFERENCES code_revisions(id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS code_chunks (
                revision_id TEXT NOT NULL,
                id TEXT NOT NULL,
                path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                symbol_id TEXT,
                qualified_name TEXT,
                contextual_text TEXT NOT NULL,
                PRIMARY KEY (revision_id, id),
                FOREIGN KEY (revision_id) REFERENCES code_revisions(id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS code_edges (
                revision_id TEXT NOT NULL,
                id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                confidence REAL NOT NULL,
                evidence_status TEXT NOT NULL,
                path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY (revision_id, id),
                FOREIGN KEY (revision_id) REFERENCES code_revisions(id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS code_chunk_vectors (
                revision_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                vector_json TEXT NOT NULL,
                vectorizer TEXT NOT NULL,
                PRIMARY KEY (revision_id, chunk_id),
                FOREIGN KEY (revision_id) REFERENCES code_revisions(id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_code_symbols_name
                ON code_symbols(revision_id, name, qualified_name);
            CREATE INDEX IF NOT EXISTS idx_code_edges_source
                ON code_edges(revision_id, source_id, kind);
            CREATE INDEX IF NOT EXISTS idx_code_edges_target
                ON code_edges(revision_id, target_id, kind);
            CREATE VIRTUAL TABLE IF NOT EXISTS code_chunks_fts USING fts5(
                revision_id UNINDEXED,
                chunk_id UNINDEXED,
                path,
                qualified_name,
                contextual_text,
                content,
                tokenize = 'unicode61 tokenchars _'
            );
            """
        )

    @staticmethod
    def repository_id(root: Path) -> str:
        """Derive a stable 24-char hex id for a repository from its resolved root path.

        Input: root - a Path to the repository root. Lowercases the resolved
        absolute path before hashing so the same repository always maps to the
        same id regardless of process, working directory, or path casing. Called
        by RepositoryRetrievalIndex.__init__ and RepositoryIndexStore.get/rebuild
        to key the `code_repositories`/`code_revisions` tables.
        """
        return hashlib.sha256(str(root.resolve()).lower().encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def revision_id(repository_id: str, fingerprint: str) -> str:
        """Derive a stable 24-char hex id for one (repository, content, schema) combination.

        Input: repository_id - from `repository_id()`; fingerprint - the content
        hash from a RepositorySnapshot. Including INDEX_SCHEMA_VERSION means
        re-indexing the exact same repository state reuses the same revision row,
        while bumping the schema version forces every revision to be rebuilt.
        Called by RepositoryRetrievalIndex.__init__ and RepositoryIndexStore
        whenever a revision needs to be looked up or created.
        """
        return hashlib.sha256(
            f"{repository_id}:{fingerprint}:{INDEX_SCHEMA_VERSION}".encode()
        ).hexdigest()[:24]

    def load_chunks(self, revision_id: str) -> list[SourceChunk] | None:
        """Load every persisted chunk for a revision, if it finished indexing successfully.

        Input: revision_id - str id from `revision_id()`. Returns None (rather
        than partial data) when the revision doesn't exist or its status isn't
        'complete', so the caller knows to rebuild instead of trusting a
        partially-written index. Called by RepositoryRetrievalIndex.__init__ to
        restore a previously indexed repository revision instead of re-parsing
        and re-chunking the repository from scratch.
        """
        with self._lock:
            connection = self._connect()
            revision = connection.execute(
                "SELECT status FROM code_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            if revision is None or revision["status"] != "complete":
                return None
            rows = connection.execute(
                "SELECT id, path, start_line, end_line, kind, content, symbol_id, "
                "qualified_name FROM code_chunks WHERE revision_id = ? "
                "ORDER BY path, start_line, id",
                (revision_id,),
            ).fetchall()
            return [SourceChunk(**dict(row)) for row in rows]

    def publish(
        self,
        session: AnalysisSession,
        snapshot: RepositorySnapshot,
        revision_id: str,
        chunks: list[SourceChunk],
    ) -> None:
        """Atomically write one fully-built revision (files, symbols, chunks, edges, FTS) to SQLite.

        Input: session - AnalysisSession (repo root/report); snapshot -
        RepositorySnapshot (files/aliases); revision_id; chunks - the list built by
        `RepositoryRetrievalIndex._build_chunks`. Runs as a single transaction and
        only flips the revision's status to 'complete' at the very end, so a crash
        mid-write never leaves a partially-usable revision behind; also prunes
        older complete revisions of the same repository beyond the most recent 3.
        Called once by RepositoryRetrievalIndex.__init__ right after building a
        fresh index (i.e. when `load_chunks` found nothing to restore).
        """
        repository_id = self.repository_id(session.root)
        with self._lock:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            # Everything below runs inside that BEGIN IMMEDIATE; any exception here must
            # roll back explicitly (not just the one deliberate early return below) or
            # this persistent connection is left holding the write lock forever,
            # blocking every other store sharing this SQLite file.
            try:
                connection.execute(
                    "INSERT INTO code_repositories(id, canonical_root, display_name) "
                    "VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "display_name = excluded.display_name, updated_at = CURRENT_TIMESTAMP",
                    (repository_id, str(session.root), session.report.repository_name),
                )
                existing = connection.execute(
                    "SELECT status FROM code_revisions WHERE id = ?", (revision_id,)
                ).fetchone()
                if existing is not None and existing["status"] == "complete":
                    connection.rollback()
                    return
                connection.execute(
                    "INSERT OR REPLACE INTO code_revisions("
                    "id, repository_id, analysis_id, fingerprint, status, indexed_at"
                    ") VALUES (?, ?, ?, ?, 'building', CURRENT_TIMESTAMP)",
                    (revision_id, repository_id, session.id, snapshot.fingerprint),
                )
                for table in (
                    "code_files", "code_file_aliases", "code_symbols", "code_chunks", "code_edges"
                ):
                    connection.execute(f"DELETE FROM {table} WHERE revision_id = ?", (revision_id,))
                connection.execute(
                    "DELETE FROM code_chunks_fts WHERE revision_id = ?", (revision_id,)
                )
                connection.executemany(
                    "INSERT INTO code_files(revision_id, path, content_sha256, size_bytes) "
                    "VALUES (?, ?, ?, ?)",
                    ((revision_id, item.path, item.content_sha256, item.size_bytes) for item in snapshot.files),
                )
                connection.executemany(
                    "INSERT INTO code_file_aliases("
                    "revision_id, path, canonical_path, reason, target_kind"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        (
                            revision_id,
                            item.path,
                            item.canonical_path,
                            item.reason,
                            item.target_kind,
                        )
                        for item in snapshot.aliases
                    ),
                )
                connection.executemany(
                    "INSERT INTO code_symbols(revision_id, id, path, kind, name, "
                    "qualified_name, signature, start_line, end_line, metadata_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            revision_id, node.id, node.span.path if node.span else None,
                            node.kind.value, node.name, node.qualified_name, node.signature,
                            node.span.start_line if node.span else None,
                            node.span.end_line if node.span else None,
                            json.dumps(node.metadata, sort_keys=True, default=str),
                        )
                        for node in session.report.nodes
                    ),
                )
                chunk_rows = [
                    (
                        revision_id, chunk.id, chunk.path, chunk.start_line, chunk.end_line,
                        chunk.kind, chunk.content, chunk.symbol_id, chunk.qualified_name,
                        self._contextual_text(session, chunk),
                    )
                    for chunk in chunks
                ]
                connection.executemany(
                    "INSERT INTO code_chunks(revision_id, id, path, start_line, end_line, "
                    "kind, content, symbol_id, qualified_name, contextual_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    chunk_rows,
                )
                connection.executemany(
                    "INSERT INTO code_chunks_fts(revision_id, chunk_id, path, qualified_name, "
                    "contextual_text, content) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        (row[0], row[1], row[2], row[8] or "", row[9], row[6])
                        for row in chunk_rows
                    ),
                )
                connection.executemany(
                    "INSERT INTO code_edges(revision_id, id, source_id, target_id, kind, "
                    "confidence, evidence_status, path, start_line, end_line, metadata_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            revision_id, edge.id, edge.source, edge.target, edge.kind.value,
                            edge.evidence.confidence, edge.evidence.status.value,
                            edge.evidence.span.path, edge.evidence.span.start_line,
                            edge.evidence.span.end_line,
                            json.dumps(edge.metadata, sort_keys=True, default=str),
                        )
                        for edge in session.report.edges
                    ),
                )
                connection.execute(
                    "UPDATE code_revisions SET status = 'complete', indexed_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?", (revision_id,)
                )
                stale = connection.execute(
                    "SELECT id FROM code_revisions WHERE repository_id = ? AND status = 'complete' "
                    "ORDER BY indexed_at DESC", (repository_id,)
                ).fetchall()[3:]
                for row in stale:
                    connection.execute("DELETE FROM code_chunks_fts WHERE revision_id = ?", (row["id"],))
                    connection.execute("DELETE FROM code_revisions WHERE id = ?", (row["id"],))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _contextual_text(session: AnalysisSession, chunk: SourceChunk) -> str:
        """Build the synthetic "document" indexed alongside a chunk's raw content.

        Input: session - for repository_name; chunk - a SourceChunk. Produces a
        short text block (repository name, path, kind, symbol name, and
        space-separated lowercase identifier subwords extracted from the path and
        qualified name) so full-text and vector search can match on file/symbol
        names even when those exact words don't appear in the code body itself.
        Called by `publish` (to populate `code_chunks.contextual_text`/the FTS
        table) and `ensure_vectors` (as part of the text that gets vectorized).
        """
        identifier_text = " ".join(
            part.lower()
            # Same camelCase/PascalCase/snake_case/digit-run splitter as
            # LocalCodeVectorizer.vectorize, applied here to the path + symbol name.
            for part in re.findall(
                r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|[0-9]+",
                f"{chunk.path} {chunk.qualified_name or ''}".replace("_", " "),
            )
            if len(part) > 1
        )
        return "\n".join(
            (
                f"Repository: {session.report.repository_name}",
                f"Path: {chunk.path}",
                f"Kind: {chunk.kind}",
                f"Symbol: {chunk.qualified_name or 'file content'}",
                f"Identifiers: {identifier_text}",
            )
        )

    def search(self, revision_id: str, query: str, limit: int) -> list[tuple[str, float]]:
        """Run a SQLite FTS5 full-text query for one revision, ranked by bm25.

        Input: revision_id; query - raw query string (tokenized here into
        identifier-like words, OR-ed together in the MATCH expression); limit -
        capped to 1-200. Uses `bm25(code_chunks_fts, 0, 0, 2.5, 4.0, 1.5, 1.0)`,
        weighting the `path` and `qualified_name` columns above raw `content` so
        filename/symbol-name hits outrank incidental content matches; bm25's rank
        is negated so higher scores are better here. Called by
        RepositoryRetrievalIndex.search as the lexical half of its hybrid ranking.
        """
        tokens = list(dict.fromkeys(_TOKEN.findall(query.lower())))
        if not tokens:
            return []
        expression = " OR ".join(f'"{token}"' for token in tokens)
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                "SELECT chunk_id, bm25(code_chunks_fts, 0, 0, 2.5, 4.0, 1.5, 1.0) AS rank "
                "FROM code_chunks_fts WHERE code_chunks_fts MATCH ? AND revision_id = ? "
                "ORDER BY rank LIMIT ?",
                (expression, revision_id, max(1, min(limit, 200))),
            ).fetchall()
            return [(str(row["chunk_id"]), -float(row["rank"])) for row in rows]

    def ensure_vectors(
        self,
        revision_id: str,
        session: AnalysisSession,
        chunks: list[SourceChunk],
    ) -> None:
        """Make sure every chunk in `chunks` has a stored subword vector for this revision.

        Input: revision_id; session; chunks - the full chunk list for the
        revision. First copies vectors over from a prior revision's identical
        (same id and exact same content) chunks to skip recomputation, then
        vectorizes and inserts whatever's still missing via
        `LocalCodeVectorizer.vectorize`. Called once by
        RepositoryRetrievalIndex.__init__ right after chunks are loaded or built,
        so `vector_search` always has vectors ready to query against.
        """
        with self._lock:
            connection = self._connect()
            # `with connection:` (not `connection.commit()`) so the INSERT OR IGNORE
            # below always gets committed - including on the early return - instead of
            # sitting open on this persistent connection and blocking every other store
            # sharing this SQLite file until something else happens to commit it.
            with connection:
                # Reuse vectors only when both stable chunk ID and exact source content
                # match a prior revision. This avoids recomputation without stale vectors.
                connection.execute(
                    "INSERT OR IGNORE INTO code_chunk_vectors("
                    "revision_id, chunk_id, vector_json, vectorizer) "
                    "SELECT ?, current.id, vectors.vector_json, vectors.vectorizer "
                    "FROM code_chunks AS current "
                    "JOIN code_chunks AS previous ON previous.id = current.id "
                    "AND previous.content = current.content "
                    "AND previous.revision_id != current.revision_id "
                    "JOIN code_chunk_vectors AS vectors "
                    "ON vectors.revision_id = previous.revision_id "
                    "AND vectors.chunk_id = previous.id "
                    "WHERE current.revision_id = ? "
                    "AND vectors.vectorizer = 'local-subword-v3'",
                    (revision_id, revision_id),
                )
                existing = {
                    str(row[0]) for row in connection.execute(
                        "SELECT chunk_id FROM code_chunk_vectors WHERE revision_id = ? "
                        "AND vectorizer = 'local-subword-v3'",
                        (revision_id,),
                    ).fetchall()
                }
                missing = [chunk for chunk in chunks if chunk.id not in existing]
                if missing:
                    connection.executemany(
                        "INSERT OR REPLACE INTO code_chunk_vectors("
                        "revision_id, chunk_id, vector_json, vectorizer) VALUES (?, ?, ?, ?)",
                        (
                            (
                                revision_id,
                                chunk.id,
                                json.dumps(
                                    LocalCodeVectorizer.vectorize(
                                        f"{self._contextual_text(session, chunk)}\n{chunk.content}"
                                    ),
                                    separators=(",", ":"),
                                ),
                                "local-subword-v3",
                            )
                            for chunk in missing
                        ),
                    )
            if missing:
                self._vector_cache.pop(revision_id, None)

    def vector_search(
        self, revision_id: str, query: str, limit: int
    ) -> list[tuple[str, float]]:
        """Rank stored chunk vectors against the query's subword vector.

        Input: revision_id; query - raw query string, vectorized here via
        `LocalCodeVectorizer.vectorize`; limit - capped to 1-200. Loads every
        stored vector for the revision, scores each with
        `LocalCodeVectorizer.similarity`, and keeps only matches above a small
        0.02 noise-floor threshold. Called by RepositoryRetrievalIndex.search as
        the "semantic" half of its hybrid ranking, alongside the FTS lexical
        results from `search()` above.
        """
        query_vector = LocalCodeVectorizer.vectorize(query)
        if not query_vector:
            return []
        with self._lock:
            vectors = self._vector_cache.get(revision_id)
            if vectors is None:
                connection = self._connect()
                rows = connection.execute(
                    "SELECT chunk_id, vector_json FROM code_chunk_vectors "
                    "WHERE revision_id = ? AND vectorizer = 'local-subword-v3'",
                    (revision_id,),
                ).fetchall()
                vectors = []
                for row in rows:
                    raw = json.loads(str(row["vector_json"]))
                    ordered = sorted((int(key), float(value)) for key, value in raw.items())
                    vectors.append((
                        str(row["chunk_id"]),
                        array("H", (key for key, _value in ordered)),
                        array("f", (value for _key, value in ordered)),
                    ))
                # One index normally serves one immutable revision. Bound the cache
                # anyway so a long-lived administrative process cannot retain every
                # revision it has ever queried.
                if len(self._vector_cache) >= 3:
                    self._vector_cache.pop(next(iter(self._vector_cache)))
                self._vector_cache[revision_id] = vectors
        ranked: list[tuple[str, float]] = []
        for chunk_id, indices, values in vectors:
            score = 0.0
            for key, query_value in query_vector.items():
                position = bisect_left(indices, key)
                if position < len(indices) and indices[position] == key:
                    score += query_value * values[position]
            if score > 0.02:
                ranked.append((chunk_id, score))
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked[: max(1, min(limit, 200))]

    def status(self, revision_id: str) -> dict[str, Any]:
        """Report a revision's fingerprint/status/indexed_at plus row counts per table.

        Input: revision_id. Returns `{"revision_id": ..., "status": "missing"}`
        when the revision doesn't exist, otherwise a diagnostic summary including
        counts of files, symbols, chunks, edges, vectors, and aliases. Called by
        RepositoryRetrievalIndex.status, which is exposed to the API/MCP layer
        (backend/app/api/routes.py:get_index, backend/app/mcp_server.py) as the
        index-health / `get_index_status` tool.
        """
        with self._lock:
            connection = self._connect()
            revision = connection.execute(
                "SELECT fingerprint, status, indexed_at FROM code_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                return {"revision_id": revision_id, "status": "missing"}
            counts = {}
            for label, table in (
                ("files", "code_files"), ("symbols", "code_symbols"),
                ("chunks", "code_chunks"), ("edges", "code_edges"),
                ("vectors", "code_chunk_vectors"), ("aliases", "code_file_aliases"),
            ):
                counts[label] = int(connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE revision_id = ?",
                    (revision_id,),
                ).fetchone()[0])
            return {
                "revision_id": revision_id,
                "fingerprint": str(revision["fingerprint"]),
                "status": str(revision["status"]),
                "indexed_at": str(revision["indexed_at"]),
                **counts,
            }

    def delete_revision(self, revision_id: str) -> None:
        """Delete a revision's FTS rows and its `code_revisions` row (cascading to the rest).

        Input: revision_id. Foreign-key cascades remove the matching files,
        symbols, chunks, edges, and vectors automatically. Called by
        RepositoryIndexStore.rebuild before re-indexing a repository, to force a
        clean rebuild rather than reusing stale persisted data.
        """
        with self._lock:
            connection = self._connect()
            # `with connection:` (not `connection.commit()`) so a mid-write exception
            # rolls back instead of leaving this persistent connection stuck holding a
            # write lock that would block every other store sharing this SQLite file.
            with connection:
                connection.execute(
                    "DELETE FROM code_chunks_fts WHERE revision_id = ?", (revision_id,)
                )
                connection.execute(
                    "DELETE FROM code_revisions WHERE id = ?", (revision_id,)
                )


@dataclass(frozen=True, slots=True)
class SourceChunk:
    """One retrievable unit of source: either a parsed symbol's span or a fixed-size file slice.

    Persisted in the `code_chunks` table (via PersistentCodeIndex) and returned
    by search/read. Built by RepositoryRetrievalIndex._build_chunks and consumed
    everywhere search results are ranked or handed back to the agent/API layer.
    """

    id: str
    path: str
    start_line: int
    end_line: int
    kind: str
    content: str
    symbol_id: str | None = None
    qualified_name: str | None = None

    def public(self, score: float | None = None) -> dict[str, Any]:
        """Convert this chunk into the plain dict shape returned to API/MCP/agent callers.

        Input: score - optional relevance score to attach (rounded to 4 decimals);
        omitted when the caller isn't ranking results (e.g. a direct chunk lookup).
        Truncates `content` to a 1600-char excerpt. Called by
        RepositoryRetrievalIndex.search for every result it returns.
        """
        value: dict[str, Any] = {
            "chunk_id": self.id,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "kind": self.kind,
            "symbol_id": self.symbol_id,
            "qualified_name": self.qualified_name,
            "excerpt": self.content[:1600],
        }
        if score is not None:
            value["score"] = round(score, 4)
        return value


class RepositoryRetrievalIndex:
    """Persistent, source-span-aware lexical and graph retrieval for one revision.

    The main retrieval surface Waypoint's agent tools call for repository
    search/read/symbol-lookup; wraps a PersistentCodeIndex for one specific
    repository revision plus the in-memory graph nodes for that analysis.
    Instantiated by RepositoryIndexStore.get/rebuild (never directly by other
    callers), and reached everywhere via `self.index` on RepositoryAgentService,
    ModelComparisonService, the onboarding/issue agents, and the MCP server tools
    in backend/app/mcp_server.py.
    """

    def __init__(
        self,
        session: AnalysisSession,
        database_path: Path | None = None,
        snapshot: RepositorySnapshot | None = None,
    ) -> None:
        """Build or restore the full retrieval index for one repository revision.

        Input: session - AnalysisSession with the parsed graph report;
        database_path - override for the SQLite file (defaults to
        `settings.state_path`); snapshot - pass a pre-computed RepositorySnapshot
        to avoid re-scanning the filesystem. Takes a filesystem snapshot, derives
        a stable revision id from the repo path and content fingerprint, and
        either restores previously persisted chunks from SQLite or builds and
        publishes them fresh, then ensures every chunk has a search vector.
        Called only by RepositoryIndexStore.get/rebuild - never instantiated
        directly elsewhere - so each unique (repository, content) pair gets
        exactly one index built or restored.
        """
        self.session = session
        self.nodes = {node.id: node for node in session.report.nodes}
        self._lines: dict[str, list[str]] = {}
        # Shared by every agent service using this exact repository revision.
        # Replacing the revision index automatically invalidates these results.
        self.semantic_result_cache: dict[str, dict[str, Any]] = {}
        self.semantic_result_cache_lock = threading.RLock()
        self.snapshot = snapshot or repository_snapshot(
            session.root, settings.max_repository_files
        )
        repository_id = PersistentCodeIndex.repository_id(session.root)
        self.revision_id = PersistentCodeIndex.revision_id(
            repository_id, self.snapshot.fingerprint
        )
        self.storage = PersistentCodeIndex(database_path or settings.state_path)
        self.aliases = self.snapshot.alias_map
        self._aliases_by_canonical: dict[str, list[str]] = {}
        for alias in self.snapshot.aliases:
            self._aliases_by_canonical.setdefault(alias.canonical_path, []).append(alias.path)
        restored = self.storage.load_chunks(self.revision_id)
        if restored is not None:
            self.chunks = restored
            log_event(
                logger,
                logging.INFO,
                "retrieval.index_restored",
                "Persistent repository retrieval index restored",
                analysis_id=session.id,
                revision_id=self.revision_id,
                chunk_count=len(self.chunks),
            )
        else:
            self.chunks = self._build_chunks()
            self.storage.publish(session, self.snapshot, self.revision_id, self.chunks)
            log_event(
                logger,
                logging.INFO,
                "retrieval.index_published",
                "Persistent repository retrieval index published",
                analysis_id=session.id,
                revision_id=self.revision_id,
                file_count=len(self.snapshot.files),
                chunk_count=len(self.chunks),
                node_count=len(session.report.nodes),
                edge_count=len(session.report.edges),
            )
        self.storage.ensure_vectors(
            self.revision_id, self.session, self.chunks
        )

    def close(self) -> None:
        """Close this index's underlying `PersistentCodeIndex` connection.

        Delegates to `self.storage.close()` - see that method's docstring; this only
        matters for tests that delete their temporary state directory immediately after
        use.
        """
        self.storage.close()

    def _safe_lines(self, source_path: str) -> list[str]:
        """Read and cache one source file's lines, enforcing every safety constraint.

        Input: source_path - a path as recorded in the graph/snapshot (resolved
        through aliases via `resolve_path` first). Verifies the path belongs to
        this analysis, stays inside the repository root, isn't a symlink, and
        doesn't exceed `settings.max_python_file_bytes`; Python files are read via
        `tokenize.open` so PEP 263 encoding declarations are respected. Caches by
        canonical path so repeated reads of the same file are free. Called by
        `_build_chunks` and `read`. This is the single choke point that guards
        against path traversal, oversized files, and undecodable files before any
        content is ever chunked or shown to the model.
        """
        canonical = self.resolve_path(source_path)
        if canonical in self._lines:
            return self._lines[canonical]
        if canonical not in self.session.source_paths:
            raise ValueError("Source path is not part of this analysis")
        resolved = (self.session.root / Path(canonical)).resolve()
        try:
            resolved.relative_to(self.session.root)
        except ValueError as exc:
            raise ValueError("Source path escapes the analyzed repository") from exc
        if not resolved.is_file() or resolved.is_symlink():
            raise ValueError("Indexed source file is unavailable")
        if resolved.stat().st_size > settings.max_python_file_bytes:
            raise ValueError("Source file exceeds the configured size limit")
        try:
            if resolved.suffix.lower() == ".py":
                with tokenize.open(resolved) as stream:
                    lines = stream.read().splitlines()
            else:
                lines = resolved.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError, SyntaxError) as exc:
            # tokenize.open() reports an undecodable file (e.g. saved as UTF-16
            # instead of UTF-8) as SyntaxError, not UnicodeError - it has to decode
            # the first two lines as UTF-8 just to look for a PEP 263 encoding
            # cookie, and reports that bootstrap failure the same way it reports a
            # genuinely malformed cookie. Caught here so one bad file is skipped by
            # _build_chunks()'s existing `except ValueError: continue`, rather than
            # aborting the whole repository's index build.
            raise ValueError(f"Source file could not be decoded: {exc}") from exc
        self._lines[canonical] = lines
        return lines

    def resolve_path(self, source_path: str) -> str:
        """Normalize a possibly-relative or alias path to its canonical repository path.

        Input: source_path - any path string a caller supplies (e.g. a tool
        argument). Looks the path up in the snapshot's alias map; raises
        ValueError if the alias points at a directory rather than a file. Called
        by `_safe_lines` and `read` (and transitively by most other methods on
        this class), so lookups work whether the model/user used the canonical
        path or a known alias.
        """
        normalized = Path(source_path).as_posix().lstrip("/")
        alias = self.aliases.get(normalized)
        if alias is None:
            return normalized
        if alias.target_kind != "file":
            raise ValueError("Alias points to a directory rather than a source file")
        return alias.canonical_path

    @staticmethod
    def _chunk_id(
        path: str,
        start_line: int,
        end_line: int,
        kind: str,
        symbol_id: str = "",
    ) -> str:
        """Derive a stable, deterministic 20-char hex id for one chunk's location and kind.

        Input: path, start_line, end_line, kind, symbol_id (optional stable graph
        node id). Hashes the colon-joined fields with SHA-256, so the same source
        span/kind/symbol always gets the same chunk id across re-indexing runs.
        Called by `_build_chunks` for every symbol-backed and file-sliced chunk it
        creates.
        """
        # Different symbols can legitimately share a span and kind (for example,
        # overloads and parser-emitted declarations). Include the stable graph ID
        # so one revision can always persist every symbol chunk uniquely.
        raw = f"{path}:{start_line}:{end_line}:{kind}:{symbol_id}".encode()
        return hashlib.sha256(raw).hexdigest()[:20]

    def _build_chunks(self) -> list[SourceChunk]:
        """Build the full list of SourceChunks for this revision from the graph report and filesystem.

        Creates one chunk per graph symbol/module node that has a source span
        (capping whole-module chunks so a huge file's module-level chunk doesn't
        dominate results), plus fixed-size line-window chunks for every remaining
        file that either has no parsed symbols or isn't one of the recognized
        parsed-source languages (so docs/config/JSON/etc. stay searchable). Called
        once by `__init__` when no persisted chunks were found for this revision
        (i.e. the first index of a given repository state). This is what turns the
        parsed graph plus raw filesystem into the retrievable units Waypoint's
        search/read tools operate on.
        """
        chunks: list[SourceChunk] = []
        covered_symbols: set[str] = set()
        for node in self.session.report.nodes:
            if not node.span or node.kind.value == "repository":
                continue
            try:
                lines = self._safe_lines(node.span.path)
            except ValueError:
                continue
            if not lines:
                continue
            start = max(1, node.span.start_line)
            end = min(len(lines), max(start, node.span.end_line))
            if node.kind.value == "module":
                end = min(end, start + 159)  # cap whole-module chunks at ~160 lines
            content = "\n".join(lines[start - 1 : end]).strip()
            if not content:
                continue
            chunks.append(
                SourceChunk(
                    id=self._chunk_id(
                        node.span.path,
                        start,
                        end,
                        node.kind.value,
                        node.id,
                    ),
                    path=node.span.path,
                    start_line=start,
                    end_line=end,
                    kind=node.kind.value,
                    content=content,
                    symbol_id=node.id,
                    qualified_name=node.qualified_name,
                )
            )
            covered_symbols.add(node.id)

        symbol_paths = {chunk.path for chunk in chunks}
        for path in sorted(self.session.source_paths):
            try:
                lines = self._safe_lines(path)
            except ValueError:
                continue
            if not lines:
                continue
            # Documentation/configuration and files without parsed definitions are
            # chunked by lines. Parsed source uses its language-neutral symbol chunks.
            if path in symbol_paths and Path(path).suffix.lower() in {
                ".py", ".js", ".jsx", ".mjs", ".cjs",
                ".ts", ".tsx", ".mts", ".cts", ".java",
            }:
                continue
            # 120-line windows with a 100-line stride give a 20-line overlap between
            # consecutive chunks, so a match near a window boundary isn't split in two.
            for offset in range(0, len(lines), 100):
                end_offset = min(len(lines), offset + 120)
                content = "\n".join(lines[offset:end_offset]).strip()
                if content:
                    chunks.append(
                        SourceChunk(
                            id=self._chunk_id(path, offset + 1, end_offset, "file"),
                            path=path,
                            start_line=offset + 1,
                            end_line=end_offset,
                            kind="file",
                            content=content,
                        )
                    )
                if end_offset == len(lines):
                    break
        return chunks

    def tree(self, prefix: str = "", limit: int = 300) -> dict[str, Any]:
        """List every known path (canonical source paths plus aliases), optionally prefix-filtered.

        Input: prefix - case-insensitive path prefix filter (default: none);
        limit - capped to 1-5000 results. Returns paths plus alias metadata
        (path/canonical_path/reason/target_kind). Called by the
        `list_repository_tree` agent tool (backend/app/agent/service.py),
        backend/app/api/routes.py:get_repository_tree, and
        backend/app/mcp_server.py's `repository_tree` tool - the primary way the
        agent/API discovers what files exist before reading or searching them.
        """
        normalized = prefix.strip("/\\").lower()
        paths = sorted({
            path
            for path in (*self.session.source_paths, *self.aliases)
            if not normalized or path.lower().startswith(normalized)
        })
        bounded = max(1, min(limit, 5000))
        return {
            "paths": paths[:bounded],
            "total": len(paths),
            "truncated": len(paths) > bounded,
            "canonical_files": len(self.session.source_paths),
            "alias_count": len(self.aliases),
            "aliases": [
                {
                    "path": alias.path,
                    "canonical_path": alias.canonical_path,
                    "reason": alias.reason,
                    "target_kind": alias.target_kind,
                }
                for alias in self.snapshot.aliases
                if not normalized or alias.path.lower().startswith(normalized)
            ],
        }

    @staticmethod
    def _is_test_path(path: str) -> bool:
        """Heuristically flag whether a path looks like a test file.

        Input: path - a repository-relative path string. True if it has a
        `/test/`, `/tests/`, or `/__tests__/` path segment, or a filename starting
        with `test_` or containing `.test.`/`.spec.`. Called by `search`'s
        filtering/scoring (`include_tests`, the test-path score penalty) to
        exclude or de-prioritize test files unless the query is explicitly about
        tests.
        """
        normalized = f"/{path.lower().replace(chr(92), '/')}"
        name = Path(path).name.lower()
        return (
            "/test/" in normalized or "/tests/" in normalized
            or "/__tests__/" in normalized or name.startswith("test_")
            or ".test." in name or ".spec." in name
        )

    @staticmethod
    def _is_generated_artifact_path(path: str) -> bool:
        """Flag paths under known generated-output directories that shouldn't pollute search.

        Input: path - a repository-relative path string. Matches a fixed list of
        markers (benchmark results, frontend coverage/Playwright/test-results,
        verification results). Called by `search` to exclude these paths unless
        the query itself is about benchmarks/coverage/results
        (`asks_for_generated_artifacts`).
        """
        lowered = path.lower().replace("\\", "/")
        return any(
            marker in f"/{lowered}"
            for marker in (
                "/benchmarks/results/",
                "/frontend/coverage/",
                "/frontend/playwright-report/",
                "/frontend/test-results/",
                "/verification/results/",
                "/verification/agentic-rag-evaluation-cases.json",
                "/verification/model-quality-cases.json",
                "/benchmarks/retrieval_cases.json",
                "/benchmarks/retrieval_cases_v3.json",
            )
        )

    @staticmethod
    def _query_terms(query: str) -> tuple[set[str], set[str]]:
        """Return raw and informative lexical terms for retrieval scoring.

        Raw terms preserve intent words used by filters. Informative terms remove
        high-frequency question words and add conservative singular/stem variants,
        preventing large files from ranking highly merely because they repeat words
        such as "how", "and", or "through".
        """
        raw = {token.lower() for token in _TOKEN.findall(query)}
        informative = {token for token in raw if token not in _QUERY_STOPWORDS}
        expanded = set(informative)
        for token in informative:
            if token.endswith("ies") and len(token) > 5:
                expanded.add(f"{token[:-3]}y")
            elif token.endswith("s") and not token.endswith("ss") and len(token) > 4:
                expanded.add(token[:-1])
            for suffix in ("ing", "ed"):
                if token.endswith(suffix) and len(token) >= len(suffix) + 4:
                    stem = token[: -len(suffix)]
                    expanded.add(stem)
                    if suffix == "ed":
                        expanded.add(f"{stem}e")
        return raw, expanded or raw

    @classmethod
    def _query_clauses(cls, query: str) -> list[str]:
        """Decompose multi-hop questions into bounded lexical retrieval clauses."""
        pieces = re.split(r"[,;:]|\b(?:and then|through|along with|as well as)\b", query, flags=re.I)
        clauses: list[str] = []
        for piece in pieces:
            _raw, terms = cls._query_terms(piece)
            clause = " ".join(sorted(terms))
            if len(terms) >= 2 and clause not in clauses:
                clauses.append(clause)
        return clauses[:6]

    @staticmethod
    def _source_layer(path: str) -> str:
        lowered = f"/{path.lower().replace(chr(92), '/')}"
        name = Path(path).name.lower()
        suffix = Path(path).suffix.lower()
        if RepositoryRetrievalIndex._is_test_path(path):
            return "tests"
        if "/frontend/" in lowered or suffix in {".tsx", ".jsx", ".css"}:
            return "frontend"
        if "/api/" in lowered or "route" in name or "controller" in name or "mcp" in name:
            return "interface"
        if any(term in name for term in ("config", "setting", "env")) or suffix in {".toml", ".yaml", ".yml", ".ini"}:
            return "configuration"
        if any(term in name for term in ("index", "store", "memory", "retrieval", "database")):
            return "storage"
        if suffix in {".md", ".mdx", ".txt", ".rst"}:
            return "documentation"
        return "implementation"

    @staticmethod
    def _intent_layers(raw_tokens: set[str]) -> set[str]:
        layers = {"implementation"}
        mappings = {
            "tests": {"test", "tests", "testing", "fixture", "coverage"},
            "frontend": {"frontend", "react", "ui", "component", "browser"},
            "interface": {
                "api", "dispatch", "endpoint", "entry", "expose", "exposed",
                "http", "mcp", "route", "transport",
            },
            "configuration": {
                "bound", "bounded", "budget", "config", "configuration",
                "environment", "limit", "limits", "setting", "settings", "timeout",
            },
            "storage": {
                "index", "memory", "persist", "persistence", "restore", "restored",
                "retrieve", "retrieval", "sqlite", "store", "stored",
            },
            "documentation": {
                "documentation", "docs", "guide", "knowledge", "overview", "readme",
                "rule", "rules", "skill", "skills", "tutorial", "workflow", "worldview",
            },
        }
        for layer, vocabulary in mappings.items():
            if raw_tokens & vocabulary:
                layers.add(layer)
        return layers

    @staticmethod
    def _language(path: str) -> str:
        """Map a file's extension to a coarse language/category label.

        Input: path - any path string. Returns "python", "java", "typescript",
        "javascript", "documentation" (.md/.mdx/.txt), or "configuration"
        (everything else). Called by `search`/`find_symbols` to implement the
        `languages` filter, and by `_build_chunks` to decide which files count as
        "parsed source" (and therefore skip line-window chunking).
        """
        suffix = Path(path).suffix.lower()
        if suffix == ".py":
            return "python"
        if suffix == ".java":
            return "java"
        if suffix in {".ts", ".tsx", ".mts", ".cts"}:
            return "typescript"
        if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
            return "javascript"
        return "documentation" if suffix in {".md", ".mdx", ".txt"} else "configuration"

    def search(
        self,
        query: str,
        limit: int = 12,
        *,
        path_prefixes: list[str] | None = None,
        kinds: list[str] | None = None,
        languages: list[str] | None = None,
        include_tests: bool = True,
    ) -> list[dict[str, Any]]:
        """Waypoint's main hybrid repository search: fuses lexical, FTS, and vector signals.

        Input: query - free-text/code string; limit - capped to 1-50 results;
        path_prefixes/kinds/languages - optional filter lists; include_tests.
        Combines exact substring/token matches, SQLite FTS5 (bm25) results, and
        local subword-vector cosine similarity into one fused score per chunk
        (see the scoring block below), applies path/kind/language/test/generated-
        artifact filters, then expands the top 5 seed results one graph hop (via
        `session.report.edges`) to pull in structurally related chunks that didn't
        match textually, and finally re-orders results to favor distinct file
        paths when the query has 3+ tokens. Called by the `search_repository`
        agent tool (backend/app/agent/service.py), by
        backend/app/agent/comparison.py's `_evidence`, and by
        backend/app/mcp_server.py's `search_repository` MCP tool - it is the
        primary way the agent finds relevant code before answering a question.
        """
        normalized = query.strip().lower()
        raw_tokens, tokens = self._query_terms(normalized)
        if not normalized:
            return []
        started = time.perf_counter()
        log_event(
            logger,
            logging.INFO,
            "retrieval.query_started",
            "Hybrid repository retrieval started",
            analysis_id=self.session.id,
            revision_id=self.revision_id,
            query=query,
            limit=limit,
            path_prefixes=path_prefixes or [],
            kinds=kinds or [],
            languages=languages or [],
            include_tests=include_tests,
            indexed_chunks=len(self.chunks),
        )
        prefixes = tuple(
            Path(value).as_posix().strip("/").lower()
            for value in (path_prefixes or []) if value.strip()
        )
        allowed_kinds = {value.lower() for value in (kinds or [])}
        allowed_languages = {value.lower() for value in (languages or [])}
        try:
            retrieval_query = " ".join(sorted(tokens))
            search_queries = [retrieval_query, *self._query_clauses(query)]
            fts_results = self.storage.search(self.revision_id, search_queries[0], 200)
            # Reciprocal-rank fusion (k=60): turn a raw FTS rank position into a small
            # monotonic score so it can be combined with the vector ranks below on a
            # comparable scale, regardless of the two sources' very different raw scores.
            fts_ranks = {
                chunk_id: 1.0 / (60 + rank)
                for rank, (chunk_id, _score) in enumerate(fts_results, start=1)
            }
            # Multi-hop clauses contribute a smaller RRF signal so one dominant clause
            # cannot hide configuration, transport, storage, or validation evidence.
            for clause in search_queries[1:]:
                for rank, (chunk_id, _score) in enumerate(
                    self.storage.search(self.revision_id, clause, 100), start=1
                ):
                    fts_ranks[chunk_id] = fts_ranks.get(chunk_id, 0.0) + 0.65 / (60 + rank)
            vector_results = self.storage.vector_search(
                self.revision_id, retrieval_query, 200
            )
            # Same reciprocal-rank fusion as fts_ranks, applied to the vector-similarity ranking.
            vector_ranks = {
                chunk_id: 1.0 / (60 + rank)
                for rank, (chunk_id, _score) in enumerate(vector_results, start=1)
            }
            log_event(
                logger,
                logging.INFO,
                "retrieval.candidates_generated",
                "Lexical and vector candidates generated",
                analysis_id=self.session.id,
                revision_id=self.revision_id,
                fts_candidates=len(fts_results),
                vector_candidates=len(vector_results),
                top_fts=fts_results[:20],
                top_vectors=vector_results[:20],
            )
        except sqlite3.Error as exc:
            log_event(
                logger,
                logging.WARNING,
                "retrieval.fts_fallback",
                "Persistent FTS query failed; falling back to in-memory scoring",
                revision_id=self.revision_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            fts_ranks = {}
            vector_ranks = {}
        ranked: list[tuple[float, SourceChunk]] = []
        asks_for_generated_artifacts = any(
            token in {
                "benchmark", "benchmarks", "coverage", "evaluation",
                "evaluations", "playwright", "report", "reports", "result", "results",
            }
            for token in raw_tokens
        )
        implementation_query = any(
            token in raw_tokens
            for token in {
                "call", "calls", "class", "clone", "code", "function", "implementation",
                "index", "parse", "parsed", "parser", "persist", "rank", "request",
                "resolve", "resolved", "route", "store", "stored", "trace", "validate",
            }
        )
        intent_layers = self._intent_layers(raw_tokens)
        asks_for_documentation = any(
            token in raw_tokens
            for token in {
                "documentation", "docs", "guide", "knowledge", "overview", "readme",
                "rule", "rules", "skill", "skills", "tutorial", "workflow", "worldview",
            }
        )
        for chunk in self.chunks:
            lowered_path = chunk.path.lower()
            if prefixes and not any(lowered_path.startswith(prefix) for prefix in prefixes):
                continue
            if allowed_kinds and chunk.kind.lower() not in allowed_kinds:
                continue
            if allowed_languages and self._language(chunk.path) not in allowed_languages:
                continue
            if not include_tests and self._is_test_path(chunk.path):
                continue
            if (
                self._is_generated_artifact_path(chunk.path)
                and not asks_for_generated_artifacts
            ):
                continue
            if (
                implementation_query
                and not asks_for_documentation
                and self._language(chunk.path) == "documentation"
            ):
                continue
            haystack = " ".join(
                (
                    chunk.path,
                    chunk.qualified_name or "",
                    chunk.content,
                )
            ).lower()
            token_counts = {token: min(haystack.count(token), 3) for token in tokens}
            matched = sum(token_counts.values())
            matched_terms = sum(bool(count) for count in token_counts.values())
            fts_score = fts_ranks.get(chunk.id)
            vector_score = vector_ranks.get(chunk.id)
            if (
                not matched and normalized not in haystack
                and fts_score is None and vector_score is None
            ):
                continue
            # Fuse raw token-overlap count with the two RRF signals above, weighting
            # FTS and vector matches far more heavily than a bare substring hit count
            # (both are on a ~0-1 RRF scale, scaled up so they dominate the total).
            score = (
                float(matched)
                + (matched_terms / max(1, len(tokens))) * 10.0
                + (fts_score or 0.0) * 120.0
                + (vector_score or 0.0) * 90.0
            )
            if normalized in haystack:
                score += 12.0
            if any(token in (chunk.qualified_name or "").lower() for token in tokens):
                score += 6.0
            path_term_matches = sum(token in chunk.path.lower() for token in tokens)
            if path_term_matches:
                score += path_term_matches * 8.0
            source_layer = self._source_layer(chunk.path)
            if source_layer in intent_layers:
                score += 9.0
            if asks_for_documentation and source_layer == "documentation":
                score += 8.0
            if chunk.kind in {"class", "function", "method"}:
                score += 5.0
            elif chunk.kind == "module":
                score += 2.0
            asks_for_tests = any(
                token in {"test", "tests", "testing", "fixture", "coverage"}
                for token in tokens
            )
            if self._is_test_path(chunk.path) and not asks_for_tests:
                score -= 6.0
            asks_for_schema = any(
                token in {"schema", "schemas", "xsd", "xml", "validation"}
                for token in tokens
            )
            # XSD schema files are rarely what a query is actually about, so they rank
            # very low by default unless the query explicitly mentions schema/xsd/validation.
            if Path(chunk.path).suffix.lower() == ".xsd" and not asks_for_schema:
                score -= 18.0
            ranked.append((score, chunk))
        ranked.sort(key=lambda item: (-item[0], item[1].path, item[1].start_line))

        # Add bounded structural context around the strongest symbol-backed seeds.
        scores = {chunk.id: score for score, chunk in ranked}
        chunks_by_symbol = {
            chunk.symbol_id: chunk for chunk in self.chunks if chunk.symbol_id
        }
        related_ids: set[str] = set()
        for _score, seed in ranked[:5]:
            if not seed.symbol_id:
                continue
            for edge in self.session.report.edges:
                if edge.source == seed.symbol_id:
                    related_ids.add(edge.target)
                elif edge.target == seed.symbol_id:
                    related_ids.add(edge.source)
        for symbol_id in related_ids:
            chunk = chunks_by_symbol.get(symbol_id)
            if chunk is None or chunk.id in scores:
                continue
            lowered_path = chunk.path.lower()
            if prefixes and not any(lowered_path.startswith(prefix) for prefix in prefixes):
                continue
            if allowed_kinds and chunk.kind.lower() not in allowed_kinds:
                continue
            if allowed_languages and self._language(chunk.path) not in allowed_languages:
                continue
            if not include_tests and self._is_test_path(chunk.path):
                continue
            if (
                self._is_generated_artifact_path(chunk.path)
                and not asks_for_generated_artifacts
            ):
                continue
            if (
                implementation_query
                and not asks_for_documentation
                and self._language(chunk.path) == "documentation"
            ):
                continue
            # Graph-expanded chunks never matched the query text, so they're scored as a
            # fixed fraction (0.18) of the strongest seed's score - enough to surface as
            # structural context, but never able to outrank an actual textual match.
            scores[chunk.id] = max((scores.get(seed.id, 0.0) for _, seed in ranked[:5]), default=1.0) * 0.18
            ranked.append((scores[chunk.id], chunk))
        ranked.sort(key=lambda item: (-item[0], item[1].path, item[1].start_line))

        bounded = max(1, min(limit, 50))
        # For specific, multi-token (likely well-formed) queries, reorder results so the
        # first hit from each distinct file surfaces before a second result from a file
        # that already appeared, instead of one file's chunks crowding out other files.
        if len(tokens) >= 3:
            # File-level MMR: preserve relevance while rewarding uncovered intent
            # layers and new package neighborhoods. This is deterministic and bounded.
            unique: list[tuple[float, SourceChunk]] = []
            seen_paths: set[str] = set()
            for item in ranked:
                if item[1].path not in seen_paths:
                    unique.append(item)
                    seen_paths.add(item[1].path)
            remaining = unique[:100]
            diverse: list[tuple[float, SourceChunk]] = []
            covered_layers: set[str] = set()
            covered_parents: set[str] = set()
            covered_paths: set[str] = set()
            node_paths = {
                node.id: node.span.path
                for node in self.session.report.nodes
                if node.span is not None
            }
            graph_path_pairs = {
                (node_paths[edge.source], node_paths[edge.target])
                for edge in self.session.report.edges
                if edge.source in node_paths
                and edge.target in node_paths
                and node_paths[edge.source] != node_paths[edge.target]
            }
            maximum_score = max((score for score, _chunk in remaining), default=1.0) or 1.0
            while remaining and len(diverse) < bounded:
                def adjusted(item: tuple[float, SourceChunk]) -> float:
                    score, chunk = item
                    layer = self._source_layer(chunk.path)
                    parent = str(Path(chunk.path).parent).lower()
                    graph_related = any(
                        (chunk.path, selected_path) in graph_path_pairs
                        or (selected_path, chunk.path) in graph_path_pairs
                        for selected_path in covered_paths
                    )
                    return (
                        score / maximum_score
                        + (0.40 if layer in intent_layers and layer not in covered_layers else 0.0)
                        + (0.08 if parent not in covered_parents else -0.05)
                        + (0.08 if graph_related else 0.0)
                    )
                chosen = max(remaining, key=adjusted)
                remaining.remove(chosen)
                diverse.append(chosen)
                covered_layers.add(self._source_layer(chosen[1].path))
                covered_parents.add(str(Path(chosen[1].path).parent).lower())
                covered_paths.add(chosen[1].path)
            ranked = [*diverse, *remaining]
        selected = []
        for score, chunk in ranked[:bounded]:
            item = chunk.public(score)
            aliases = sorted(self._aliases_by_canonical.get(chunk.path, []))
            if aliases:
                item["also_available_as"] = aliases
            selected.append(item)
        selected_text = " ".join(
            f"{item['path']} {item.get('qualified_name') or ''} {item.get('content') or ''}"
            for item in selected[: min(10, len(selected))]
        ).lower()
        term_coverage = sum(token in selected_text for token in tokens) / max(1, len(tokens))
        layer_coverage = sum(
            any(self._source_layer(str(item["path"])) == layer for item in selected[:10])
            for layer in intent_layers
        ) / max(1, len(intent_layers))
        retrieval_confidence = round(term_coverage * 0.65 + layer_coverage * 0.35, 4)
        for item in selected:
            item["retrieval_confidence"] = retrieval_confidence
            item["retrieval_requires_followup"] = retrieval_confidence < 0.62
            item["query_term_coverage"] = round(term_coverage, 4)
            item["intent_layer_coverage"] = round(layer_coverage, 4)
        log_event(
            logger,
            logging.INFO,
            "retrieval.query_completed",
            "Hybrid repository retrieval completed",
            analysis_id=self.session.id,
            revision_id=self.revision_id,
            query=query,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            fused_candidates=len(scores),
            graph_related_symbols=len(related_ids),
            returned=len(selected),
            results=selected,
        )
        return selected

    def find_symbols(
        self,
        query: str,
        limit: int = 20,
        kinds: list[str] | None = None,
        languages: list[str] | None = None,
    ) -> dict[str, Any]:
        """Look up graph symbols by name/qualified-name substring match.

        Input: query - substring, matched case-insensitively; limit - capped to
        1-100; kinds/languages - optional filters. Excludes the synthetic
        "repository" root node. Scores an exact name match highest (20.0), a
        name-prefix match next (12.0), and any other substring hit lowest (6.0),
        with a bonus (+4.0) when the qualified name ends with `.query` (a
        dotted-suffix match, e.g. finding `Foo.bar` by searching "bar"). Called by
        the `find_symbols` agent tool (backend/app/agent/service.py) and
        backend/app/mcp_server.py's `find_symbols` MCP tool, as the name-based
        counterpart to the content-based `search` method above - useful when the
        model already knows a symbol's name.
        """
        normalized = query.strip().lower()
        allowed_kinds = {value.lower() for value in (kinds or [])}
        allowed_languages = {value.lower() for value in (languages or [])}
        ranked: list[tuple[float, GraphNode]] = []
        for node in self.nodes.values():
            if node.kind.value == "repository":
                continue
            if allowed_kinds and node.kind.value not in allowed_kinds:
                continue
            language = str(node.metadata.get("language", "")).lower()
            if allowed_languages and language not in allowed_languages:
                continue
            name = node.name.lower()
            qualified = node.qualified_name.lower()
            if normalized not in name and normalized not in qualified:
                continue
            score = 20.0 if name == normalized else 12.0 if name.startswith(normalized) else 6.0
            if qualified.endswith(f".{normalized}"):
                score += 4.0
            ranked.append((score, node))
        ranked.sort(key=lambda item: (-item[0], item[1].qualified_name))
        results = []
        for score, node in ranked[: max(1, min(limit, 100))]:
            results.append({
                "node_id": node.id,
                "kind": node.kind.value,
                "name": node.name,
                "qualified_name": node.qualified_name,
                "signature": node.signature,
                "language": node.metadata.get("language"),
                "path": node.span.path if node.span else None,
                "start_line": node.span.start_line if node.span else None,
                "end_line": node.span.end_line if node.span else None,
                "score": score,
            })
        return {"query": query, "revision_id": self.revision_id, "results": results, "count": len(results)}

    def status(self) -> dict[str, Any]:
        """Return this revision's persisted index diagnostics (see PersistentCodeIndex.status).

        Exposed via the `get_index_status` agent tool
        (backend/app/agent/service.py), backend/app/api/routes.py:get_index, and
        backend/app/mcp_server.py's status tool.
        """
        return self.storage.status(self.revision_id)

    def read(self, path: str, start_line: int = 1, end_line: int = 200) -> dict[str, Any]:
        """Return up to 250 line-numbered lines of one source file.

        Input: path - raw or alias path, resolved via `resolve_path`; start_line;
        end_line - clamped to at most `start_line + 249` and to the file length.
        Returns content pre-formatted as `"{line_number:>6} | {text}"` so results
        can be cited by exact line number. Called by the `read_source` agent tool
        (backend/app/agent/service.py), backend/app/agent/comparison.py,
        onboarding.py, issues.py, and backend/app/mcp_server.py's `read_source` MCP
        tool - the primary way the model/API views exact source text after
        `search`/`find_symbols` points at a location.
        """
        requested = Path(path).as_posix().lstrip("/")
        canonical = self.resolve_path(requested)
        lines = self._safe_lines(canonical)
        start = max(1, start_line)
        end = min(len(lines), max(start, end_line), start + 249)
        numbered = "\n".join(
            f"{number:>6} | {lines[number - 1]}" for number in range(start, end + 1)
        )
        return {
            "path": requested,
            "canonical_path": canonical,
            "also_available_as": sorted(self._aliases_by_canonical.get(canonical, [])),
            "start_line": start,
            "end_line": end,
            "line_count": len(lines),
            "content": numbered,
        }

    def symbol(self, node_id: str) -> dict[str, Any]:
        """Look up one graph node plus its immediate (1-hop) neighborhood.

        Input: node_id - a graph node id (as returned by `search`/`find_symbols`).
        Raises ValueError if the node doesn't exist. Called by the
        `inspect_symbol` agent tool (backend/app/agent/service.py) and
        backend/app/mcp_server.py's `inspect_symbol` MCP tool, so callers can see
        a symbol's direct relationships without a separate graph query.
        """
        node = self.nodes.get(node_id)
        if node is None:
            raise ValueError("Graph node was not found")
        neighbors = GraphQueryService(self.session.report).neighborhood(node_id, 1)
        return {
            "node": node.model_dump(mode="json"),
            "neighbors": [item.model_dump(mode="json") for item in neighbors.nodes],
            "edges": [item.model_dump(mode="json") for item in neighbors.edges],
        }

    def graph_neighborhood(self, node_id: str, depth: int = 1) -> dict[str, Any]:
        """Return the graph neighborhood (nodes + edges) around a node, out to `depth` hops.

        Input: node_id; depth - clamped to 1-3. Called by the `expand_graph` agent
        tool (backend/app/agent/service.py), backend/app/agent/onboarding.py, and
        backend/app/mcp_server.py's `expand_graph` MCP tool, when the model needs
        to explore call/import/inheritance relationships beyond a symbol's
        immediate neighbors.
        """
        neighborhood = GraphQueryService(self.session.report).neighborhood(
            node_id, max(1, min(depth, 3))
        )
        return neighborhood.model_dump(mode="json")

    def node_for_path(self, path: str) -> GraphNode | None:
        """Find the graph "module" node whose span path exactly matches `path`.

        Input: path - an exact repository-relative path (not alias-resolved
        here). Returns None if the file wasn't parsed into a module node (e.g. a
        non-source file). Called by backend/app/agent/comparison.py,
        onboarding.py, and issues.py when they need the module-level graph node
        for a cited/evidence file path (e.g. to inspect its imports).
        """
        modules = [
            node
            for node in self.nodes.values()
            if node.span and node.span.path == path and node.kind.value == "module"
        ]
        return modules[0] if modules else None


class RepositoryIndexStore:
    """Thread-safe bounded cache so one analysis is indexed only once.

    Caches one RepositoryRetrievalIndex per analysis session id (LRU-evicted
    beyond `max_indexes`), so repeated tool calls against the same analysis reuse
    the same in-memory index/SQLite connections instead of re-scanning and
    re-chunking the repository on every request. Instantiated once as the
    module-level singleton `repository_indexes` below, and used by
    backend/app/agent/service.py, comparison.py, backend/app/api/routes.py, and
    backend/app/mcp_server.py as the single entry point for obtaining a
    repository's retrieval index.
    """

    def __init__(self, max_indexes: int = 10) -> None:
        """Set up the LRU-ordered index cache and its lock (default capacity: 10 analyses)."""
        self.max_indexes = max_indexes
        self._indexes: OrderedDict[str, RepositoryRetrievalIndex] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, session: AnalysisSession) -> RepositoryRetrievalIndex:
        """Return the cached retrieval index for a session, refreshing it if the repo changed.

        Input: session - an AnalysisSession. Compares the repository's current
        content fingerprint against the one recorded at analysis time; if they
        differ, transparently re-analyzes the repository and refreshes the
        session before continuing. Returns the cached RepositoryRetrievalIndex
        if one already exists for this exact revision, otherwise builds (or
        restores from SQLite) a fresh one and caches it, evicting the
        least-recently-used entry when over `max_indexes`. Called by nearly every
        retrieval entry point (backend/app/agent/service.py, comparison.py,
        backend/app/api/routes.py, backend/app/mcp_server.py) - it is the sole way
        callers obtain a RepositoryRetrievalIndex, so filesystem drift between the
        original analysis and "now" is detected here rather than silently serving
        stale results.
        """
        snapshot = repository_snapshot(session.root, settings.max_repository_files)
        if (
            session.revision_fingerprint
            and snapshot.fingerprint != session.revision_fingerprint
        ):
            log_event(
                logger,
                logging.INFO,
                "retrieval.repository_change_detected",
                "Repository changed since analysis; refreshing graph and index",
                analysis_id=session.id,
                previous_fingerprint=session.revision_fingerprint,
                current_fingerprint=snapshot.fingerprint,
            )
            from backend.app.graph.analyzer import RepositoryAnalyzer
            from backend.app.graph.store import analysis_sessions

            refreshed_report = RepositoryAnalyzer().analyze(session.root)
            session = analysis_sessions.refresh(
                session.id, session.root, refreshed_report
            )
            snapshot = repository_snapshot(
                session.root, settings.max_repository_files
            )
        repository_id = PersistentCodeIndex.repository_id(session.root)
        revision_id = PersistentCodeIndex.revision_id(
            repository_id, snapshot.fingerprint
        )
        with self._lock:
            existing = self._indexes.get(session.id)
            if existing is not None and existing.revision_id == revision_id:
                self._indexes.move_to_end(session.id)
                return existing
        created = RepositoryRetrievalIndex(session, snapshot=snapshot)
        with self._lock:
            existing = self._indexes.get(session.id)
            if existing is not None and existing.revision_id == revision_id:
                self._indexes.move_to_end(session.id)
                return existing
            self._indexes[session.id] = created
            while len(self._indexes) > self.max_indexes:
                self._indexes.popitem(last=False)
            return created

    def rebuild(self, session: AnalysisSession) -> RepositoryRetrievalIndex:
        """Force a full re-analysis and re-index of the repository, discarding any cached state.

        Input: session - an AnalysisSession. Unlike `get`, always re-runs
        RepositoryAnalyzer regardless of whether the content fingerprint changed,
        refreshes the session's graph report, deletes the old persisted SQLite
        revision, drops any cached in-memory index, and rebuilds via `get`. Called
        by the `POST /analyses/{id}/index/rebuild` API route
        (backend/app/api/routes.py) and backend/app/mcp_server.py's rebuild tool,
        when a user explicitly wants a fresh index rather than relying on the
        automatic drift detection in `get`.
        """
        from backend.app.graph.analyzer import RepositoryAnalyzer
        from backend.app.graph.store import analysis_sessions

        report = RepositoryAnalyzer().analyze(session.root)
        refreshed = analysis_sessions.refresh(session.id, session.root, report)
        snapshot = repository_snapshot(
            refreshed.root, settings.max_repository_files
        )
        repository_id = PersistentCodeIndex.repository_id(refreshed.root)
        revision_id = PersistentCodeIndex.revision_id(
            repository_id, snapshot.fingerprint
        )
        PersistentCodeIndex(settings.state_path).delete_revision(revision_id)
        with self._lock:
            self._indexes.pop(refreshed.id, None)
        return self.get(refreshed)

    def close(self) -> None:
        """Close every cached index's underlying connection.

        The process never needs to call this - see `PersistentCodeIndex.close()`. It
        exists for tests that build a store against a temporary directory and then
        delete that directory immediately afterward.
        """
        with self._lock:
            for index in self._indexes.values():
                index.close()


repository_indexes = RepositoryIndexStore()
