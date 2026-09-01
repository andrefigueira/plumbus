"""SQLite-backed job store. Deliberately boring so it survives restarts."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL,          -- queued|running|done|failed|cancelled
    backend       TEXT NOT NULL,
    prompt        TEXT,
    source_images TEXT NOT NULL DEFAULT '[]',
    params        TEXT NOT NULL DEFAULT '{}',
    progress      REAL NOT NULL DEFAULT 0,
    stage         TEXT,
    log           TEXT NOT NULL DEFAULT '',
    error         TEXT,
    glb_path      TEXT,
    thumb_path    TEXT,
    stats         TEXT NOT NULL DEFAULT '{}',
    cost_usd      REAL,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_status  ON jobs (status);
"""

_JSON_FIELDS = {"source_images", "params", "stats"}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.plumbus_db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for f in _JSON_FIELDS:
        try:
            d[f] = json.loads(d[f] or ("[]" if f == "source_images" else "{}"))
        except (json.JSONDecodeError, TypeError):
            d[f] = [] if f == "source_images" else {}
    return d


def create_job(
    backend: str,
    source_images: list[str],
    params: dict[str, Any],
    prompt: str | None = None,
) -> str:
    job_id = uuid.uuid4().hex[:12]
    with connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, status, backend, prompt, source_images, params, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                job_id,
                "queued",
                backend,
                prompt,
                json.dumps(source_images),
                json.dumps(params),
                time.time(),
            ),
        )
    return job_id


def update(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    for f in _JSON_FIELDS & fields.keys():
        fields[f] = json.dumps(fields[f])
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def append_log(job_id: str, line: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET log = COALESCE(log,'') || ? WHERE id = ?",
            (line.rstrip() + "\n", job_id),
        )


def get(job_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_jobs(limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM jobs"
    args: list[Any] = []
    if status:
        q += " WHERE status = ?"
        args.append(status)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def next_queued() -> dict[str, Any] | None:
    """Atomically claim the oldest queued job so CLI gen and the worker cannot both run it."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'queued'",
            (row["id"],),
        )
        if cur.rowcount != 1:
            return None
        d = _row_to_dict(row)
        d["status"] = "running"
        return d


def delete(job_id: str) -> None:
    job = get(job_id)
    if job:
        for key in ("glb_path", "thumb_path"):
            p = job.get(key)
            if p and Path(p).exists():
                Path(p).unlink(missing_ok=True)
    with connect() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def reset_orphans() -> int:
    """Jobs left 'running' by a hard restart are not coming back. Requeue them."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='queued', progress=0, stage=NULL WHERE status='running'"
        )
        return cur.rowcount
