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

-- 验收作用域 = 录音 × 结算期：仓里实际留下的份额按结算期留存，
-- 发布新裁定版本后回执与净额继续有效（差异按新版核定值重新核对）。
-- 回执以「回执号 + 内容哈希」去重：同号同内容只计一次，同号异内容拒绝。
CREATE TABLE IF NOT EXISTS warehouse_receipt (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL REFERENCES recording(id),
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    receipt_no   TEXT NOT NULL,
    content_sha  TEXT NOT NULL,
    line_count   INTEGER NOT NULL,
    received_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (recording_id, period_start, period_end, receipt_no)
);

-- 净额区间表：同一范围、同一权利人、同一万分比，
-- 入仓 +1 / 冲销 -1；各区间互不重叠，且只保存非零净额。
CREATE TABLE IF NOT EXISTS warehouse_interval (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL REFERENCES recording(id),
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    holder       TEXT NOT NULL,
    share_value  INTEGER NOT NULL,
    start_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    net_count    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_warehouse_interval
    ON warehouse_interval (recording_id, period_start, period_end,
                           holder, share_value, start_date);

-- 每个核定版的最新验收结果（回执入账时与回执同事务更新）
CREATE TABLE IF NOT EXISTS acceptance_check (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id     INTEGER NOT NULL REFERENCES settlement_version(id),
    passed         INTEGER NOT NULL,
    mismatch_count INTEGER NOT NULL,
    receipt_count  INTEGER NOT NULL,
    result_json    TEXT NOT NULL,
    checked_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (version_id)
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
        conn.execute("PRAGMA busy_timeout = 15000")
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
        row = conn.execute(
            "SELECT id FROM settlement_version WHERE recording_id = ? "
            "AND period_start = ? AND period_end = ? AND version_no = ?",
            (recording_id, start, end, version_no),
        ).fetchone()
        # 以新版核定值对既有仓数据立即核对，验收与发布同事务生效
        _refresh_check(conn, get_version(row["id"]))
        conn.commit()
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


# ---------- 验收（回执 / 净额区间 / 核对结果） ----------

def _scope(version) -> tuple:
    return (version["recording_id"], version["period_start"], version["period_end"])


def list_warehouse_intervals(version_id: int) -> list[dict]:
    version = get_version(version_id)
    if version is None:
        return []
    rows = get_db().execute(
        """SELECT * FROM warehouse_interval
           WHERE recording_id = ? AND period_start = ? AND period_end = ?
           ORDER BY holder, share_value, start_date""",
        _scope(version),
    ).fetchall()
    return [dict(r) for r in rows]


def get_acceptance(version_id: int) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM acceptance_check WHERE version_id = ?", (version_id,)
    ).fetchone()
    if not row:
        return None
    return _check_row(row)


def _check_row(row) -> dict:
    result = json.loads(row["result_json"])
    result["checked_at"] = row["checked_at"]
    result["receipt_count"] = row["receipt_count"]
    return result


def list_receipts(version_id: int) -> list[dict]:
    version = get_version(version_id)
    if version is None:
        return []
    rows = get_db().execute(
        """SELECT * FROM warehouse_receipt
           WHERE recording_id = ? AND period_start = ? AND period_end = ?
           ORDER BY received_at, id""",
        _scope(version),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_acceptance(version_id: int) -> dict | None:
    """该结算期最近一次核对结果（用于核对结果与回执同事务、同时可查）。"""
    version = get_version(version_id)
    if version is None:
        return None
    row = get_db().execute(
        """SELECT c.* FROM acceptance_check c
           JOIN settlement_version v ON v.id = c.version_id
           WHERE v.recording_id = ? AND v.period_start = ? AND v.period_end = ?
           ORDER BY c.id DESC LIMIT 1""",
        _scope(version),
    ).fetchone()
    return _check_row(row) if row else None


def _refresh_scope(conn, scope) -> None:
    """回执变动后，重算该结算期所有核定版的核对结果。

    仓数据按结算期共享；同一份新回执可能同时改变新旧版本的差异，
    因此同时收回/恢复各版的通过标记。
    """
    rows = conn.execute(
        """SELECT * FROM settlement_version
           WHERE recording_id = ? AND period_start = ? AND period_end = ?
           ORDER BY version_no""",
        scope,
    ).fetchall()
    for row in rows:
        _refresh_check(conn, row)


def _refresh_check(conn, version) -> dict:
    """按当前净额区间重算核对结果并写入（不重放回执）。"""
    from .acceptance import compute_check

    segments = json.loads(version["segments_json"])
    scope = _scope(version)
    intervals = [
        dict(r) for r in conn.execute(
            """SELECT * FROM warehouse_interval
               WHERE recording_id = ? AND period_start = ? AND period_end = ?
               ORDER BY holder, share_value, start_date""",
            scope,
        ).fetchall()
    ]
    receipt_count = conn.execute(
        """SELECT COUNT(*) AS c FROM warehouse_receipt
           WHERE recording_id = ? AND period_start = ? AND period_end = ?""",
        scope,
    ).fetchone()["c"]
    result = compute_check(
        version["period_start"], version["period_end"],
        segments, intervals, receipt_count,
    )
    conn.execute(
        """INSERT INTO acceptance_check
           (version_id, passed, mismatch_count, receipt_count, result_json, checked_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT (version_id) DO UPDATE SET
             passed = excluded.passed,
             mismatch_count = excluded.mismatch_count,
             receipt_count = excluded.receipt_count,
             result_json = excluded.result_json,
             checked_at = excluded.checked_at""",
        (
            version["id"],
            1 if result["passed"] else 0,
            result["mismatch_count"],
            receipt_count,
            json.dumps(result, ensure_ascii=False),
        ),
    )
    return result


def ingest_receipt(version_id: int, receipt_no: str, content_sha: str,
                   lines: list[dict], fail: bool = False) -> tuple[str, dict]:
    """回执入账：回执行与核对结果在同一事务中生效。

    返回 (state, 核对结果)；state ∈ {"applied", "duplicate"}。
    同号异内容抛 ReceiptConflict。写入失败时整体回滚（fail=True 用于
    演示失败后重传），调用方可用同一回执号重传，原核对结果保持不变。
    """
    from .acceptance import ReceiptConflict, apply_line_delta

    conn = get_db()
    version = get_version(version_id)
    if version is None:
        raise ReceiptErrorLike("核定版不存在")
    scope = _scope(version)

    # 先快速判定同号；再做写事务，避免重复回执进入写锁
    existing = conn.execute(
        """SELECT content_sha FROM warehouse_receipt
           WHERE recording_id = ? AND period_start = ? AND period_end = ?
             AND receipt_no = ?""",
        (*scope, receipt_no),
    ).fetchone()
    if existing is not None:
        if existing["content_sha"] != content_sha:
            raise ReceiptConflict(
                f"回执号 {receipt_no} 已入账过不同内容，本次拒绝入账"
            )
        return "duplicate", refresh_acceptance(version_id)

    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:  # 并发写库超时
        raise ReceiptErrorLike(f"数据库繁忙，请稍后重传：{exc}") from exc

    try:
        # 写锁内再查一次，兼容并发同号上传
        existing = conn.execute(
            """SELECT content_sha FROM warehouse_receipt
               WHERE recording_id = ? AND period_start = ? AND period_end = ?
                 AND receipt_no = ?""",
            (*scope, receipt_no),
        ).fetchone()
        if existing is not None:
            conn.rollback()
            if existing["content_sha"] == content_sha:
                return "duplicate", refresh_acceptance(version_id)
            raise ReceiptConflict(
                f"回执号 {receipt_no} 已入账过不同内容，本次拒绝入账"
            )

        for line in lines:
            delta = 1 if line["action"] == "in" else -1
            apply_line_delta(
                conn, scope, line["holder"], line["value"],
                line["start_date"], line["end_date"], delta,
            )

        conn.execute(
            """INSERT INTO warehouse_receipt
               (recording_id, period_start, period_end,
                receipt_no, content_sha, line_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (*scope, receipt_no, content_sha, len(lines)),
        )
        if fail:
            raise sqlite3.OperationalError("模拟写入失败")
        _refresh_scope(conn, scope)
        result = _refresh_check(conn, version)
        conn.commit()
        return "applied", result
    except ReceiptConflict:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise


def refresh_acceptance(version_id: int) -> dict:
    """重新核对（不重放回执，仅按当前净额区间重算）。"""
    conn = get_db()
    version = get_version(version_id)
    if version is None:
        raise ValueError("核定版不存在")
    try:
        result = _refresh_check(conn, version)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


class ReceiptErrorLike(ValueError):
    """db 层可披露的业务错误。"""
