"""SQLite 持久层。

不可变版本原则：
  * versions 表只 INSERT，从不 UPDATE/DELETE；同一内容哈希重复提交返回同一版本号。
  * 完整流程定义以 JSON 存入版本行，版本之间互不影响。
  * 导师锁定、情境、演练记录是版本之外的旁路数据，不改动版本内容。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.schemas import ProcessSpec, Scenario


def _default_db_path() -> Path:
    return Path(os.environ.get("DRILL_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "drill.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processes (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    process_code TEXT NOT NULL,
    version      INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE(process_code, version),
    UNIQUE(process_code, content_hash)
);
CREATE TABLE IF NOT EXISTS step_locks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    process_code TEXT NOT NULL,
    version      INTEGER NOT NULL,
    step_code    TEXT NOT NULL,
    mentor       TEXT NOT NULL,
    note         TEXT DEFAULT '',
    locked_at    TEXT NOT NULL,
    UNIQUE(process_code, version, step_code)
);
CREATE TABLE IF NOT EXISTS scenarios (
    code         TEXT PRIMARY KEY,
    process_code TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    process_code   TEXT NOT NULL,
    version        INTEGER NOT NULL,
    scenario_code  TEXT,
    entry          TEXT NOT NULL,
    actor          TEXT NOT NULL,
    params_json    TEXT NOT NULL,
    materials_json TEXT NOT NULL,
    started_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    round_no   INTEGER,
    at_step    TEXT,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    process_code TEXT NOT NULL,
    version      INTEGER NOT NULL,
    code         TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE(process_code, version, code)
);
CREATE TABLE IF NOT EXISTS repair_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    process_code TEXT NOT NULL,
    version     INTEGER NOT NULL,
    scenario_codes_json TEXT NOT NULL,
    minimal_json TEXT NOT NULL,
    feasible    INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else _default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(_SCHEMA)


def canonical_hash(spec: ProcessSpec) -> str:
    """对除版本号外的流程内容做规范哈希（SHA-256）。"""
    import hashlib

    payload = spec.model_dump(mode="json")
    payload.pop("version", None)
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 流程与版本
# ---------------------------------------------------------------------------


def upsert_process_meta(conn: sqlite3.Connection, spec: ProcessSpec) -> None:
    conn.execute(
        "INSERT INTO processes(code, name, description, created_at) "
        "VALUES(?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, "
        "description=excluded.description",
        (spec.code, spec.name, spec.description, _now()),
    )


def save_version(
    conn: sqlite3.Connection, spec: ProcessSpec
) -> tuple[int, str, bool]:
    """写入不可变版本。同内容哈希返回旧版本号（幂等）。

    返回 (version, content_hash, created)。
    """
    content_hash = canonical_hash(spec)
    row = conn.execute(
        "SELECT version FROM versions WHERE process_code=? AND content_hash=?",
        (spec.code, content_hash),
    ).fetchone()
    if row:
        return int(row["version"]), content_hash, False

    last = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS m FROM versions WHERE process_code=?",
        (spec.code,),
    ).fetchone()
    version = int(last["m"]) + 1
    stored = spec.model_copy(update={"version": version})
    conn.execute(
        "INSERT INTO versions(process_code, version, content_hash, content_json, created_at)"
        " VALUES(?,?,?,?,?)",
        (spec.code, version, content_hash, stored.model_dump_json(), _now()),
    )
    return version, content_hash, True


def get_version(
    conn: sqlite3.Connection, process_code: str, version: int | None = None
) -> dict[str, Any] | None:
    if version is None:
        row = conn.execute(
            "SELECT * FROM versions WHERE process_code=? ORDER BY version DESC LIMIT 1",
            (process_code,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM versions WHERE process_code=? AND version=?",
            (process_code, version),
        ).fetchone()
    if not row:
        return None
    return {
        "process_code": row["process_code"],
        "version": row["version"],
        "content_hash": row["content_hash"],
        "spec": ProcessSpec.model_validate_json(row["content_json"]),
        "created_at": row["created_at"],
    }


def list_versions(conn: sqlite3.Connection, process_code: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT version, content_hash, created_at FROM versions "
        "WHERE process_code=? ORDER BY version",
        (process_code,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 导师锁定
# ---------------------------------------------------------------------------


def lock_step(
    conn: sqlite3.Connection,
    process_code: str,
    version: int,
    step_code: str,
    mentor: str,
    note: str = "",
) -> bool:
    """锁定某步骤；已锁定则返回 False（幂等不覆盖，保留原导师确认痕迹）。"""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO step_locks(process_code, version, step_code, mentor, note, locked_at)"
        " VALUES(?,?,?,?,?,?)",
        (process_code, version, step_code, mentor, note, _now()),
    )
    return cursor.rowcount > 0


def get_locks(
    conn: sqlite3.Connection, process_code: str, version: int
) -> set[str]:
    rows = conn.execute(
        "SELECT step_code FROM step_locks WHERE process_code=? AND version=?",
        (process_code, version),
    ).fetchall()
    return {r["step_code"] for r in rows}


def list_locks(conn: sqlite3.Connection, process_code: str, version: int) -> list[dict]:
    rows = conn.execute(
        "SELECT step_code, mentor, note, locked_at FROM step_locks "
        "WHERE process_code=? AND version=? ORDER BY step_code",
        (process_code, version),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 情境
# ---------------------------------------------------------------------------


def save_scenario(conn: sqlite3.Connection, scenario: Scenario) -> None:
    conn.execute(
        "INSERT INTO scenarios(code, process_code, content_json, created_at)"
        " VALUES(?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET content_json=excluded.content_json, "
        "process_code=excluded.process_code",
        (scenario.code, scenario.process_code, scenario.model_dump_json(), _now()),
    )


def get_scenario(conn: sqlite3.Connection, code: str) -> Scenario | None:
    row = conn.execute(
        "SELECT content_json FROM scenarios WHERE code=?", (code,)
    ).fetchone()
    return Scenario.model_validate_json(row["content_json"]) if row else None


def list_scenarios(conn: sqlite3.Connection, process_code: str | None = None) -> list[dict]:
    if process_code:
        rows = conn.execute(
            "SELECT code, process_code FROM scenarios WHERE process_code=? ORDER BY code",
            (process_code,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT code, process_code FROM scenarios ORDER BY code"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 演练会话与事件（实际路径）
# ---------------------------------------------------------------------------


def create_session(
    conn: sqlite3.Connection,
    process_code: str,
    version: int,
    entry: str,
    actor: str,
    params: dict[str, Any],
    initial_materials: dict[str, Any],
    scenario_code: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO sessions(process_code, version, scenario_code, entry, actor, "
        "params_json, materials_json, started_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            process_code,
            version,
            scenario_code,
            entry,
            actor,
            json.dumps(params, ensure_ascii=False, sort_keys=True),
            json.dumps(initial_materials, ensure_ascii=False, sort_keys=True),
            _now(),
        ),
    )
    return int(cur.lastrowid)


def get_session(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sessions WHERE id=?", (session_id,)
    ).fetchone()


def add_event(
    conn: sqlite3.Connection,
    session_id: int,
    seq: int,
    kind: str,
    payload: dict[str, Any],
    round_no: int | None = None,
    at_step: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events(session_id, seq, kind, round_no, at_step, payload)"
        " VALUES(?,?,?,?,?,?)",
        (
            session_id,
            seq,
            kind,
            round_no,
            at_step,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )


def get_events(conn: sqlite3.Connection, session_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT seq, kind, round_no, at_step, payload FROM events "
        "WHERE session_id=? ORDER BY seq, id",
        (session_id,),
    ).fetchall()
    return [
        {
            "seq": r["seq"],
            "kind": r["kind"],
            "round_no": r["round_no"],
            "at_step": r["at_step"],
            "payload": json.loads(r["payload"]),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 候选补充规则
# ---------------------------------------------------------------------------


def save_candidate(
    conn: sqlite3.Connection,
    process_code: str,
    version: int,
    code: str,
    content: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO candidates(process_code, version, code, content_json, created_at)"
        " VALUES(?,?,?,?,?)",
        (process_code, version, code, json.dumps(content, ensure_ascii=False), _now()),
    )


def list_candidates(
    conn: sqlite3.Connection, process_code: str, version: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT code, content_json, created_at FROM candidates "
        "WHERE process_code=? AND version=? ORDER BY code",
        (process_code, version),
    ).fetchall()
    return [
        {"code": r["code"], **json.loads(r["content_json"]), "created_at": r["created_at"]}
        for r in rows
    ]


def save_repair_run(
    conn: sqlite3.Connection,
    process_code: str,
    version: int,
    scenario_codes: list[str],
    minimal: list[str],
    feasible: bool,
) -> None:
    conn.execute(
        "INSERT INTO repair_runs(process_code, version, scenario_codes_json, minimal_json, "
        "feasible, created_at) VALUES(?,?,?,?,?,?)",
        (
            process_code,
            version,
            json.dumps(scenario_codes, ensure_ascii=False),
            json.dumps(minimal, ensure_ascii=False),
            1 if feasible else 0,
            _now(),
        ),
    )
