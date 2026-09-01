from __future__ import annotations

import threading
import sqlite3
import json
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from backend.app.config import settings
from backend.app.agent.retrieval import LocalCodeVectorizer


@dataclass(slots=True)
class Conversation:
    """A lightweight handle identifying one chat thread: its id and the analysis it
    belongs to. Returned by ConversationStore.get_or_create()/latest() and passed back
    into history()/transcript()/append_turn() to scope those calls to one thread."""

    id: str
    analysis_id: str


@dataclass(frozen=True, slots=True)
class HybridMemoryDiagnostics:
    """Observable accounting for one recent-plus-semantic memory retrieval."""

    recent_turns: int
    semantic_candidates: int
    semantic_turns: int
    semantic_characters: int
    maximum_semantic_turns: int
    minimum_similarity: float
    selected_scores: tuple[float, ...]


class ConversationStore:
    """SQLite-backed store for multi-turn chat history, scoped per analysis and per
    channel ("ask", "onboarding", "inspector").

    Exists so the Q&A agent (backend/app/agent/service.py:RepositoryAgentService.answer)
    can resume a conversation across requests/process restarts, and so
    backend/app/api/routes.py:latest_conversation can replay a saved transcript to the
    frontend. A module-level singleton `conversations` (bottom of this file) is the
    instance actually used in production; tests construct their own with a temp path.
    """

    def __init__(self, path: Path | None = None, max_conversations: int = 200) -> None:
        """Configure the SQLite path and the max number of conversations to retain
        (oldest pruned first); the schema is created lazily on first connection."""
        self.path = (path or settings.state_path).resolve()
        self.max_conversations = max_conversations
        self._lock = threading.RLock()
        self._initialized = False
        self._local = threading.local()

    def _connect(self) -> sqlite3.Connection:
        """Return this thread's cached SQLite connection, opening (and migrating) it on
        first use.

        Kept per-thread (via `threading.local`) instead of reopened on every call: FastAPI
        dispatches these synchronous store methods across a small pool of worker threads,
        so a thread-local connection is never touched by more than one thread at a time,
        while still being reused across many requests handled by that thread - unlike a
        fresh open-then-close per call, which forces SQLite to checkpoint its WAL (fold
        pending writes back into the main file) on every single call, since that
        connection was the last one open. Against `settings.state_path` - a file shared by
        every store in the app and, over a long dev session, potentially large - that
        checkpoint was costing seconds per request. Called by every public method before
        it touches the database; see `close()` for the matching teardown a test needs
        before deleting the underlying file.
        """
        with self._lock:
            connection = getattr(self._local, "connection", None)
            if connection is not None:
                return connection
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            if not self._initialized:
                connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    analysis_id TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'ask',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON conversation_messages(conversation_id, id);
                CREATE TABLE IF NOT EXISTS conversation_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    user_message_id INTEGER NOT NULL,
                    assistant_message_id INTEGER NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    vectorizer TEXT NOT NULL,
                    revision_fingerprint TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (user_message_id) REFERENCES conversation_messages(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (assistant_message_id) REFERENCES conversation_messages(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_memories_conversation
                    ON conversation_memories(conversation_id, id);
                """
                )
                # Lightweight schema migration: older databases were created before the
                # answer_json/channel columns existed, so add them in place if absent.
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(conversation_messages)"
                    ).fetchall()
                }
                if "answer_json" not in columns:
                    connection.execute(
                        "ALTER TABLE conversation_messages ADD COLUMN answer_json TEXT"
                    )
                conversation_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(conversations)"
                    ).fetchall()
                }
                if "channel" not in conversation_columns:
                    connection.execute(
                        "ALTER TABLE conversations "
                        "ADD COLUMN channel TEXT NOT NULL DEFAULT 'ask'"
                    )
                connection.commit()
                self._initialized = True
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

    def _prune(self, connection: sqlite3.Connection) -> None:
        """Delete the oldest conversations beyond `self.max_conversations`, keyed by
        most-recent-update. Called by get_or_create() after every write so the table
        never grows without bound (cascades to conversation_messages via ON DELETE
        CASCADE).
        """
        rows = connection.execute(
            "SELECT id FROM conversations ORDER BY updated_at DESC"
        ).fetchall()
        for row in rows[self.max_conversations :]:
            connection.execute(
                "DELETE FROM conversations WHERE id = ?", (row["id"],)
            )

    def get_or_create(
        self,
        analysis_id: str,
        conversation_id: str | None,
        channel: str = "ask",
    ) -> Conversation:
        """Look up an existing conversation by id, or start a new one, scoped to
        `analysis_id` and `channel`.

        Input: conversation_id may be None (always creates a fresh conversation) or a
        previously issued id. Raises ValueError if the given conversation_id belongs to a
        different analysis or a different channel, preventing cross-analysis/cross-channel
        history leakage. Touching an existing conversation updates its updated_at and
        triggers pruning. Called by
        backend/app/agent/service.py:RepositoryAgentService.answer at the start of every
        Q&A turn to resolve which thread to append to.
        """
        with self._lock:
            connection = self._connect()
            row = (
                connection.execute(
                    "SELECT id, analysis_id, channel FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if conversation_id
                else None
            )
            if row is not None and row["analysis_id"] != analysis_id:
                raise ValueError("Conversation belongs to another analysis")
            if row is not None and row["channel"] != channel:
                raise ValueError("Conversation belongs to another workspace")
            now = time.time()
            # `with connection:` (not `connection.commit()`) so a mid-write exception
            # rolls back instead of leaving this persistent connection stuck holding a
            # write lock that would block every other store sharing this SQLite file.
            with connection:
                if row is None:
                    identifier = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO conversations("
                        "id, analysis_id, channel, created_at, updated_at"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (identifier, analysis_id, channel, now, now),
                    )
                else:
                    identifier = row["id"]
                    connection.execute(
                        "UPDATE conversations SET updated_at = ? WHERE id = ?",
                        (now, identifier),
                    )
                self._prune(connection)
            return Conversation(id=identifier, analysis_id=analysis_id)

    def latest(self, analysis_id: str, channel: str = "ask") -> Conversation | None:
        """Return the most recently updated conversation for `analysis_id`/`channel`, or
        None if the pair has no conversation yet. Called by
        backend/app/api/routes.py:latest_conversation (GET
        /analyses/{analysis_id}/conversation/latest) so the frontend can restore the last
        chat thread when a workspace is reopened.
        """
        with self._lock:
            connection = self._connect()
            row = connection.execute(
                "SELECT id, analysis_id FROM conversations "
                "WHERE analysis_id = ? AND channel = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (analysis_id, channel),
            ).fetchone()
            if row is None:
                return None
            return Conversation(id=str(row["id"]), analysis_id=str(row["analysis_id"]))

    def history(self, conversation: Conversation) -> list[dict[str, str]]:
        """Return the last `settings.chat_history_turns` question/answer pairs for
        `conversation` as plain {"role", "content"} dicts, oldest first, ready to seed the
        model's message list.

        Called by backend/app/agent/service.py:RepositoryAgentService.answer to prime a
        new agent turn with prior conversational context. Turns are limited so the prompt
        doesn't grow unbounded across a long chat.
        """
        with self._lock:
            limit = settings.chat_history_turns * 2
            connection = self._connect()
            # Take the most recent `limit` rows by id (newest first), then flip back
            # to chronological order for the outer SELECT.
            rows = connection.execute(
                "SELECT role, content FROM ("
                "SELECT id, role, content FROM conversation_messages "
                "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?"
                ") ORDER BY id ASC",
                (conversation.id, limit),
            ).fetchall()
            return [
                {"role": str(row["role"]), "content": str(row["content"])}
                for row in rows
            ]

    def hybrid_history(
        self,
        conversation: Conversation,
        query: str,
        revision_fingerprint: str | None = None,
    ) -> tuple[list[dict[str, str]], HybridMemoryDiagnostics]:
        """Combine the recent N-turn window with relevant older vector memories.

        Semantic candidates are strictly scoped to the same conversation (which is
        already scoped to one analysis and channel). Candidate score combines local
        sparse-vector cosine similarity (85%), logarithmic recency (10%), and an exact
        repository-revision bonus (5%). Older selected turns are placed before the
        recent window, deduplicated, and bounded by both count and characters.
        """
        with self._lock:
            connection = self._connect()
            recent_rows = connection.execute(
                "SELECT id, role, content FROM ("
                "SELECT id, role, content FROM conversation_messages "
                "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?"
                ") ORDER BY id ASC",
                (conversation.id, settings.chat_history_turns * 2),
            ).fetchall()
            recent_ids = {int(row["id"]) for row in recent_rows}
            recent = [
                {"role": str(row["role"]), "content": str(row["content"])}
                for row in recent_rows
            ]
            if settings.chat_semantic_memory_items <= 0 or not query.strip():
                return recent, HybridMemoryDiagnostics(
                    recent_turns=len(recent) // 2,
                    semantic_candidates=0,
                    semantic_turns=0,
                    semantic_characters=0,
                    maximum_semantic_turns=settings.chat_semantic_memory_items,
                    minimum_similarity=settings.chat_memory_min_similarity,
                    selected_scores=(),
                )
            rows = connection.execute(
                "SELECT id, user_message_id, assistant_message_id, question, answer, "
                "vector_json, revision_fingerprint, created_at "
                "FROM conversation_memories WHERE conversation_id = ? ORDER BY id DESC",
                (conversation.id,),
            ).fetchall()

        query_vector = LocalCodeVectorizer.vectorize(query)
        now = time.time()
        ranked: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            if int(row["user_message_id"]) in recent_ids:
                continue
            stored = {
                int(key): float(value)
                for key, value in json.loads(str(row["vector_json"])).items()
            }
            similarity = LocalCodeVectorizer.similarity(query_vector, stored)
            if similarity < settings.chat_memory_min_similarity:
                continue
            age_days = max(0.0, (now - float(row["created_at"])) / 86_400)
            recency = 1.0 / (1.0 + math.log1p(age_days))
            same_revision = bool(
                revision_fingerprint
                and row["revision_fingerprint"] == revision_fingerprint
            )
            score = similarity * 0.85 + recency * 0.10 + (0.05 if same_revision else 0.0)
            ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], -int(item[1]["id"])))

        selected: list[tuple[float, sqlite3.Row]] = []
        used_characters = 0
        for score, row in ranked:
            characters = len(str(row["question"])) + len(str(row["answer"]))
            if selected and used_characters + characters > settings.chat_memory_max_characters:
                continue
            selected.append((score, row))
            used_characters += characters
            if len(selected) >= settings.chat_semantic_memory_items:
                break
        selected.sort(key=lambda item: int(item[1]["id"]))
        semantic: list[dict[str, str]] = []
        for _score, row in selected:
            semantic.extend((
                {"role": "user", "content": str(row["question"])},
                {"role": "assistant", "content": str(row["answer"])},
            ))
        return [*semantic, *recent], HybridMemoryDiagnostics(
            recent_turns=len(recent) // 2,
            semantic_candidates=len(ranked),
            semantic_turns=len(selected),
            semantic_characters=used_characters,
            maximum_semantic_turns=settings.chat_semantic_memory_items,
            minimum_similarity=settings.chat_memory_min_similarity,
            selected_scores=tuple(round(score, 4) for score, _row in selected),
        )

    def transcript(self, conversation: Conversation) -> list[dict[str, str | None]]:
        """Return the full stored message log for `conversation`, including each
        assistant turn's raw `answer_json` (the serialized GroundedAnswer, if any), in
        chronological order.

        Unlike history(), this is not truncated to the chat-history window. Called by
        backend/app/api/routes.py:latest_conversation to rebuild the structured
        ConversationTranscript (with citations) returned to the frontend, using
        answer_json to recover the original GroundedAnswer where available.
        """
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                "SELECT role, content, answer_json FROM conversation_messages "
                "WHERE conversation_id = ? ORDER BY id ASC",
                (conversation.id,),
            ).fetchall()
            return [
                {
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "answer_json": (
                        str(row["answer_json"])
                        if row["answer_json"] is not None
                        else None
                    ),
                }
                for row in rows
            ]

    def append_turn(
        self,
        conversation: Conversation,
        question: str,
        answer: str,
        answer_json: str | None = None,
        revision_fingerprint: str | None = None,
    ) -> None:
        """Record one question/answer turn for `conversation` and prune older messages
        beyond the retention window.

        Input: question/answer are plain text; answer_json is the optional serialized
        GroundedAnswer stored alongside the assistant row so transcript() can restore
        citations later. The turn is also embedded into the local sparse-vector memory
        table. Messages older than `chat_memory_retention_turns` are deleted (with their
        vector rows by cascade), while only `chat_history_turns` recent turns are sent
        verbatim on each request. Called by
        backend/app/agent/service.py:RepositoryAgentService.answer at the end of each Q&A
        turn to persist it.
        """
        with self._lock:
            connection = self._connect()
            now = time.time()
            # `with connection:` (not `connection.commit()`) so a mid-write exception
            # rolls back instead of leaving this persistent connection stuck holding a
            # write lock that would block every other store sharing this SQLite file.
            with connection:
                user_cursor = connection.execute(
                    "INSERT INTO conversation_messages("
                    "conversation_id, role, content, answer_json, created_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (conversation.id, "user", question, None, now),
                )
                assistant_cursor = connection.execute(
                    "INSERT INTO conversation_messages("
                    "conversation_id, role, content, answer_json, created_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (conversation.id, "assistant", answer, answer_json, now),
                )
                memory_text = f"User question: {question}\nAssistant answer: {answer}"
                vector = LocalCodeVectorizer.vectorize(memory_text)
                connection.execute(
                    "INSERT INTO conversation_memories("
                    "conversation_id, user_message_id, assistant_message_id, question, "
                    "answer, vector_json, vectorizer, revision_fingerprint, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        conversation.id,
                        int(user_cursor.lastrowid),
                        int(assistant_cursor.lastrowid),
                        question,
                        answer,
                        json.dumps(vector, separators=(",", ":"), sort_keys=True),
                        "local-subword-hash-v1",
                        revision_fingerprint,
                        now,
                    ),
                )
                max_messages = settings.chat_memory_retention_turns * 2
                connection.execute(
                    "DELETE FROM conversation_messages WHERE conversation_id = ? "
                    "AND id NOT IN (SELECT id FROM conversation_messages "
                    "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?)",
                    (conversation.id, conversation.id, max_messages),
                )
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation.id),
                )


conversations = ConversationStore()
