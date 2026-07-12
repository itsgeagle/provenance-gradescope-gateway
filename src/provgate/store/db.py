"""SQLite connection + schema (idempotent)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL UNIQUE,
    gradescope_course_id TEXT NOT NULL,
    gradescope_email TEXT NOT NULL,
    provenance_base_url TEXT NOT NULL,
    provenance_semester_id TEXT NOT NULL,
    assignment_policy TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS secrets (
    class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    PRIMARY KEY (class_id, kind)
);

CREATE TABLE IF NOT EXISTS forwarded_submissions (
    class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    gs_assignment_id TEXT NOT NULL,
    submission_key TEXT NOT NULL,
    provenance_job_id TEXT NOT NULL,
    forwarded_at TEXT NOT NULL,
    PRIMARY KEY (class_id, gs_assignment_id, submission_key)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    gs_assignment_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    delta_count INTEGER NOT NULL,
    job_id TEXT,
    error_summary TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn
