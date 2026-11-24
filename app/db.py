"""SQLite 持久层：申报、核定版、预览快照。"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS recording (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS declaration (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL REFERENCES recording(id),
    order_no     TEXT NOT NULL,
    start_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    shares_json  TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (recording_id, order_no)
);

CREATE TABLE IF NOT EXISTS declaration_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    declaration_id INTEGER NOT NULL REFERENCES declaration(id),
    order_no       TEXT NOT NULL,
    start_date     TEXT NOT NULL,
    end_date       TEXT NOT NULL,
    shares_json    TEXT NOT NULL,
    version        INTEGER NOT NULL,
    changed_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settlement_version (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id     INTEGER NOT NULL REFERENCES recording(id),
    period_start     TEXT NOT NULL,
    period_end       TEXT NOT NULL,
    version_no       INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'published',
    returned_orders  TEXT NOT NULL,
    retained_orders  TEXT NOT NULL,
    segments_json    TEXT NOT NULL,
    input_signature  TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (recording_id, period_start, period_end, version_no)
);

CREATE TABLE IF NOT EXISTS preview_snapshot (
    token          TEXT PRIMARY KEY,
    recording_id   INTEGER NOT NULL,
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    result_json    TEXT NOT NULL,
    input_signature TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DATABASE"], detect_types=sqlite3.PARSE_DECLTYPES
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        g.db = conn
    return g.db


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()


# ---------- 录音 / 申报 ----------

def get_or_create_recording(title: str) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM recording WHERE title = ?", (title,)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO recording (title) VALUES (?)", (title,))
    conn.commit()
    return cur.lastrowid


def list_recordings() -> list[sqlite3.Row]:
    return get_db().execute("SELECT * FROM recording ORDER BY title").fetchall()


def get_recording(recording_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM recording WHERE id = ?", (recording_id,)
    ).fetchone()


def declaration_count(recording_id: int) -> int:
    return get_db().execute(
        "SELECT COUNT(*) AS c FROM declaration WHERE recording_id = ?",
        (recording_id,),
    ).fetchone()["c"]


def _row_to_decl(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "recording_id": row["recording_id"],
        "order_no": row["order_no"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "shares": json.loads(row["shares_json"]),
        "version": row["version"],
        "updated_at": row["updated_at"],
    }


def list_declarations(recording_id: int) -> list[dict]:
    rows = get_db().execute(
        "SELECT * FROM declaration WHERE recording_id = ? ORDER BY order_no",
        (recording_id,),
    ).fetchall()
    return [_row_to_decl(r) for r in rows]


def get_declaration(declaration_id: int) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM declaration WHERE id = ?", (declaration_id,)
    ).fetchone()
    return _row_to_decl(row) if row else None


def find_declaration(recording_id: int, order_no: str) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM declaration WHERE recording_id = ? AND order_no = ?",
        (recording_id, order_no),
    ).fetchone()
    return _row_to_decl(row) if row else None


def upsert_declaration(
    recording_id: int,
    order_no: str,
    start_date: str,
    end_date: str,
    shares: dict[str, int],
) -> tuple[dict, bool]:
    """新增或更正申报；更正时把旧版写入 history。返回 (申报, 是否更正)。"""
    conn = get_db()
    shares_json = json.dumps(shares, ensure_ascii=False, sort_keys=True)
    existing = conn.execute(
        "SELECT * FROM declaration WHERE recording_id = ? AND order_no = ?",
        (recording_id, order_no),
    ).fetchone()

    if existing is None:
        if declaration_count(recording_id) >= 12:
            raise ValueError("每首录音最多 12 单申报")
        cur = conn.execute(
            """INSERT INTO declaration
               (recording_id, order_no, start_date, end_date, shares_json)
               VALUES (?, ?, ?, ?, ?)""",
            (recording_id, order_no, start_date, end_date, shares_json),
        )
        conn.commit()
        return get_declaration(cur.lastrowid), False

    conn.execute(
        """INSERT INTO declaration_history
           (declaration_id, order_no, start_date, end_date, shares_json, version)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            existing["id"],
            existing["order_no"],
            existing["start_date"],
            existing["end_date"],
            existing["shares_json"],
            existing["version"],
        ),
    )
    conn.execute(
        """UPDATE declaration
           SET start_date = ?, end_date = ?, shares_json = ?,
               version = version + 1, updated_at = datetime('now')
           WHERE id = ?""",
        (start_date, end_date, shares_json, existing["id"]),
    )
    conn.commit()
    return get_declaration(existing["id"]), True


def list_history(declaration_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM declaration_history WHERE declaration_id = ? ORDER BY version",
        (declaration_id,),
    ).fetchall()


# ---------- 预览快照 ----------

def save_preview(token: str, recording_id: int, start: str, end: str,
                  result: dict, signature: str) -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO preview_snapshot
           (token, recording_id, period_start, period_end, result_json, input_signature)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token, recording_id, start, end,
         json.dumps(result, ensure_ascii=False), signature),
    )
    conn.commit()


def get_preview(token: str) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM preview_snapshot WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        return None
    return {
        "token": row["token"],
        "recording_id": row["recording_id"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "result": json.loads(row["result_json"]),
        "input_signature": row["input_signature"],
    }


# ---------- 核定版 ----------

def list_versions(recording_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM settlement_version WHERE recording_id = ? "
        "ORDER BY period_start, period_end, version_no",
        (recording_id,),
    ).fetchall()


def get_version(version_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM settlement_version WHERE id = ?", (version_id,)
    ).fetchone()


def next_version_no(recording_id: int, start: str, end: str) -> int:
    row = get_db().execute(
        "SELECT COALESCE(MAX(version_no), 0) AS m FROM settlement_version "
        "WHERE recording_id = ? AND period_start = ? AND period_end = ?",
        (recording_id, start, end),
    ).fetchone()
    return row["m"] + 1


def publish_version(recording_id: int, start: str, end: str, result: dict,
                    signature: str, fail: bool = False) -> int:
    """发布核定版；fail=True 时模拟写入失败（事务回滚，不产生新版）。"""
    conn = get_db()
    try:
        version_no = next_version_no(recording_id, start, end)
        if fail:
            raise sqlite3.OperationalError("模拟写入失败")
        conn.execute(
            """INSERT INTO settlement_version
               (recording_id, period_start, period_end, version_no, status,
                returned_orders, retained_orders, segments_json, input_signature)
               VALUES (?, ?, ?, ?, 'published', ?, ?, ?, ?)""",
            (
                recording_id, start, end, version_no,
                json.dumps(result["returned"], ensure_ascii=False),
                json.dumps(result["retain"], ensure_ascii=False),
                json.dumps(result["segments"], ensure_ascii=False),
                signature,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM settlement_version WHERE recording_id = ? "
            "AND period_start = ? AND period_end = ? AND version_no = ?",
            (recording_id, start, end, version_no),
        ).fetchone()
        return row["id"]
    except Exception:
        conn.rollback()
        raise


# ---------- 资料指纹 ----------

def input_signature(declarations: list[dict], start: str, end: str) -> str:
    """对结算期内相关申报做规范哈希，确认时与预览指纹核对。"""
    relevant = sorted(
        (d for d in declarations
         if d["end_date"] > start and d["start_date"] < end),
        key=lambda d: (d["order_no"], d["id"]),
    )
    payload = [
        {
            "order_no": d["order_no"],
            "start_date": d["start_date"],
            "end_date": d["end_date"],
            "shares": {k: d["shares"][k] for k in sorted(d["shares"])},
            "version": d["version"],
        }
        for d in relevant
    ]
    blob = json.dumps(
        {"period": [start, end], "declarations": payload},
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
