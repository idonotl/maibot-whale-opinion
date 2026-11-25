"""讨论状态的持久化层：topics / evidence / turns / audit。"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

DDL = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        user_key TEXT NOT NULL,
        topic_key TEXT NOT NULL,
        event_ref TEXT DEFAULT '',
        scope TEXT DEFAULT '',
        criteria_json TEXT DEFAULT '[]',
        user_position TEXT DEFAULT '',
        bot_stance TEXT DEFAULT 'uncertain',
        bot_position TEXT DEFAULT '',
        main_reason TEXT DEFAULT '',
        agreement_json TEXT DEFAULT '[]',
        uncertainties_json TEXT DEFAULT '[]',
        evidence_status TEXT DEFAULT 'unknown',
        last_sent_position TEXT DEFAULT '',
        discussion_state TEXT DEFAULT 'open',
        revision INTEGER DEFAULT 1,
        no_new_info_rounds INTEGER DEFAULT 0,
        reasons_json TEXT DEFAULT '[]',
        created_utc REAL NOT NULL,
        updated_utc REAL NOT NULL,
        expressed_utc REAL DEFAULT 0,
        UNIQUE(session_id, topic_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        topic_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        source_type TEXT NOT NULL,
        tool_name TEXT DEFAULT '',
        query TEXT DEFAULT '',
        url TEXT DEFAULT '',
        host TEXT DEFAULT '',
        title TEXT DEFAULT '',
        snippet TEXT DEFAULT '',
        read_level TEXT DEFAULT 'none',
        event_time TEXT DEFAULT '',
        source_group TEXT DEFAULT '',
        success INTEGER DEFAULT 1,
        limitation TEXT DEFAULT '',
        fetched_utc REAL NOT NULL,
        UNIQUE(session_id, topic_key, tool_name, url, query)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        user_key TEXT DEFAULT '',
        batch_id TEXT NOT NULL,
        message_id TEXT DEFAULT '',
        action TEXT DEFAULT 'none',
        new_info INTEGER DEFAULT 0,
        stance_before TEXT DEFAULT '',
        stance_after TEXT DEFAULT '',
        gen_state TEXT DEFAULT 'pending',
        send_state TEXT DEFAULT 'pending',
        created_utc REAL NOT NULL,
        UNIQUE(session_id, batch_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT DEFAULT '',
        topic_key TEXT DEFAULT '',
        category TEXT DEFAULT '',
        reason TEXT DEFAULT '',
        model_calls INTEGER DEFAULT 0,
        searches INTEGER DEFAULT 0,
        error_type TEXT DEFAULT '',
        created_utc REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_topics_session ON topics(session_id, updated_utc)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_topic ON evidence(session_id, topic_key, fetched_utc)",
    "CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, created_utc)",
]


class Repository:
    """插件私有 SQLite；只存简短结论与依据，不存模型长推理。"""

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def start(self) -> None:
        with self.connect() as db:
            for statement in DDL:
                db.execute(statement)
            row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            current = int(row["value"]) if row else 0
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    "数据库 schema 版本 %s 高于插件支持的 %s，拒绝写入" % (current, SCHEMA_VERSION))
            if current < SCHEMA_VERSION:
                db.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),))

    # ---------- topics ----------
    def get_topic(self, session_id: str, topic_key: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM topics WHERE session_id=? AND topic_key=?",
                (session_id, topic_key)).fetchone()
        return self._topic_row(row)

    def latest_topic(self, session_id: str, max_age_hours: float = 24.0):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM topics WHERE session_id=? AND updated_utc>=? "
                "ORDER BY updated_utc DESC LIMIT 1",
                (session_id, time.time() - max_age_hours * 3600)).fetchone()
        return self._topic_row(row)

    def upsert_topic(self, topic: dict) -> dict:
        now = time.time()
        payload = dict(topic)
        payload.setdefault("created_utc", now)
        payload["updated_utc"] = now
        with self.connect() as db:
            existing = db.execute(
                "SELECT id, revision FROM topics WHERE session_id=? AND topic_key=?",
                (payload["session_id"], payload["topic_key"])).fetchone()
            if existing:
                revision = int(existing["revision"]) + 1
                db.execute(
                    """UPDATE topics SET user_key=?, event_ref=?, scope=?, criteria_json=?,
                       user_position=?, bot_stance=?, bot_position=?, main_reason=?,
                       agreement_json=?, uncertainties_json=?, evidence_status=?,
                       last_sent_position=?, discussion_state=?, revision=?,
                       no_new_info_rounds=?, reasons_json=?, updated_utc=?
                       WHERE id=?""",
                    (
                        payload.get("user_key", ""), payload.get("event_ref", ""),
                        payload.get("scope", ""), json.dumps(payload.get("criteria") or [], ensure_ascii=False),
                        payload.get("user_position", ""), payload.get("bot_stance", "uncertain"),
                        payload.get("bot_position", ""), payload.get("main_reason", ""),
                        json.dumps(payload.get("agreement_points") or [], ensure_ascii=False),
                        json.dumps(payload.get("uncertainties") or [], ensure_ascii=False),
                        payload.get("evidence_status", "unknown"),
                        payload.get("last_sent_position", ""), payload.get("discussion_state", "open"),
                        revision, int(payload.get("no_new_info_rounds") or 0),
                        json.dumps(payload.get("reasons") or [], ensure_ascii=False), now,
                        existing["id"],
                    ))
            else:
                db.execute(
                    """INSERT INTO topics(session_id, user_key, topic_key, event_ref, scope,
                       criteria_json, user_position, bot_stance, bot_position, main_reason,
                       agreement_json, uncertainties_json, evidence_status, last_sent_position,
                       discussion_state, revision, no_new_info_rounds, reasons_json,
                       created_utc, updated_utc, expressed_utc)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        payload["session_id"], payload.get("user_key", ""), payload["topic_key"],
                        payload.get("event_ref", ""), payload.get("scope", ""),
                        json.dumps(payload.get("criteria") or [], ensure_ascii=False),
                        payload.get("user_position", ""), payload.get("bot_stance", "uncertain"),
                        payload.get("bot_position", ""), payload.get("main_reason", ""),
                        json.dumps(payload.get("agreement_points") or [], ensure_ascii=False),
                        json.dumps(payload.get("uncertainties") or [], ensure_ascii=False),
                        payload.get("evidence_status", "unknown"),
                        payload.get("last_sent_position", ""), payload.get("discussion_state", "open"),
                        int(payload.get("revision") or 1), int(payload.get("no_new_info_rounds") or 0),
                        json.dumps(payload.get("reasons") or [], ensure_ascii=False),
                        payload["created_utc"], now, float(payload.get("expressed_utc") or 0),
                    ))
        return self.get_topic(payload["session_id"], payload["topic_key"]) or {}

    def mark_expressed(self, session_id: str, topic_key: str, position: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE topics SET last_sent_position=?, expressed_utc=?, updated_utc=? "
                "WHERE session_id=? AND topic_key=?",
                (position, time.time(), time.time(), session_id, topic_key))

    def close_topic(self, session_id: str, topic_key: str, state: str = "closed") -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE topics SET discussion_state=?, updated_utc=? WHERE session_id=? AND topic_key=?",
                (state, time.time(), session_id, topic_key))

    def list_topics(self, limit: int = 20):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM topics ORDER BY updated_utc DESC LIMIT ?", (limit,)).fetchall()
        return [self._topic_row(row) for row in rows]

    def delete_topic(self, session_id: str, topic_key: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM topics WHERE session_id=? AND topic_key=?", (session_id, topic_key))
            db.execute("DELETE FROM evidence WHERE session_id=? AND topic_key=?", (session_id, topic_key))

    # ---------- evidence ----------
    def add_evidence(self, session_id: str, topic_key: str, evidence: dict) -> bool:
        with self.connect() as db:
            try:
                db.execute(
                    """INSERT OR IGNORE INTO evidence(session_id, topic_key, kind, source_type,
                       tool_name, query, url, host, title, snippet, read_level, event_time,
                       source_group, success, limitation, fetched_utc)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        session_id, topic_key, evidence.get("kind", "unknown"),
                        evidence.get("source_type", "none"), evidence.get("tool_name", ""),
                        evidence.get("query", ""), evidence.get("url", ""), evidence.get("host", ""),
                        evidence.get("title", ""), evidence.get("snippet", ""),
                        evidence.get("read_level", "none"), evidence.get("event_time", ""),
                        evidence.get("source_group", ""), 1 if evidence.get("success", True) else 0,
                        evidence.get("limitation", ""), evidence.get("fetched_utc") or time.time(),
                    ))
                return db.total_changes > 0
            except sqlite3.Error:
                return False

    def list_evidence(self, session_id: str, topic_key: str, limit: int = 8):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM evidence WHERE session_id=? AND topic_key=? "
                "ORDER BY success DESC, fetched_utc DESC LIMIT ?",
                (session_id, topic_key, limit)).fetchall()
        return [dict(row) for row in rows]

    def count_evidence(self, session_id: str, topic_key: str) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM evidence WHERE session_id=? AND topic_key=?",
                (session_id, topic_key)).fetchone()
        return int(row["n"] if row else 0)

    # ---------- turns ----------
    def add_turn(self, turn: dict) -> bool:
        with self.connect() as db:
            try:
                db.execute(
                    """INSERT OR IGNORE INTO turns(session_id, user_key, batch_id, message_id,
                       action, new_info, stance_before, stance_after, gen_state, send_state, created_utc)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        turn["session_id"], turn.get("user_key", ""), turn["batch_id"],
                        turn.get("message_id", ""), turn.get("action", "none"),
                        1 if turn.get("new_info") else 0, turn.get("stance_before", ""),
                        turn.get("stance_after", ""), turn.get("gen_state", "pending"),
                        turn.get("send_state", "pending"), time.time(),
                    ))
                return db.total_changes > 0
            except sqlite3.Error:
                return False

    def mark_turn_sent(self, session_id: str, batch_id: str, sent: bool) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE turns SET send_state=?, gen_state=? WHERE session_id=? AND batch_id=?",
                ("sent" if sent else "failed", "generated", session_id, batch_id))

    def get_turn(self, session_id: str, batch_id: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM turns WHERE session_id=? AND batch_id=?",
                (session_id, batch_id)).fetchone()
        return dict(row) if row else None

    # ---------- audit ----------
    def audit(self, session_id: str, topic_key: str, category: str, reason: str = "",
              model_calls: int = 0, searches: int = 0, error_type: str = "") -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO audit(session_id, topic_key, category, reason, model_calls,
                   searches, error_type, created_utc) VALUES(?,?,?,?,?,?,?,?)""",
                (session_id, topic_key, category, reason[:300], model_calls, searches, error_type, time.time()))

    def recent_audit(self, limit: int = 20):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM audit ORDER BY created_utc DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict:
        with self.connect() as db:
            topics = db.execute("SELECT COUNT(*) AS n FROM topics").fetchone()["n"]
            open_topics = db.execute(
                "SELECT COUNT(*) AS n FROM topics WHERE discussion_state='open'").fetchone()["n"]
            evidence = db.execute("SELECT COUNT(*) AS n FROM evidence").fetchone()["n"]
            calls = db.execute("SELECT COALESCE(SUM(model_calls),0) AS n FROM audit").fetchone()["n"]
            errors = db.execute(
                "SELECT COUNT(*) AS n FROM audit WHERE error_type<>''").fetchone()["n"]
        return {"topics": topics, "open_topics": open_topics, "evidence": evidence,
                "model_calls": calls, "errors": errors}

    # ---------- 清理 ----------
    def cleanup(self, active_hours: float, closed_days: float, audit_days: float, evidence_days: float) -> dict:
        now = time.time()
        with self.connect() as db:
            expired = db.execute(
                "DELETE FROM topics WHERE (discussion_state='open' AND updated_utc<?) "
                "OR (discussion_state<>'open' AND updated_utc<?)",
                (now - active_hours * 3600, now - closed_days * 86400)).rowcount
            ev = db.execute("DELETE FROM evidence WHERE fetched_utc<?", (now - evidence_days * 86400,)).rowcount
            aud = db.execute("DELETE FROM audit WHERE created_utc<?", (now - audit_days * 86400,)).rowcount
            turns = db.execute("DELETE FROM turns WHERE created_utc<?", (now - closed_days * 86400,)).rowcount
        return {"topics": expired, "evidence": ev, "audit": aud, "turns": turns}

    @staticmethod
    def _topic_row(row):
        if not row:
            return None
        data = dict(row)
        for key in ("criteria_json", "agreement_json", "uncertainties_json", "reasons_json"):
            try:
                data[key] = json.loads(data.get(key) or "[]")
            except json.JSONDecodeError:
                data[key] = []
        return data
