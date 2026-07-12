"""Persistence repository over the SQLite store."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from .crypto import SecretBox
from .models import AssignmentPolicy, ClassConfig, RunRecord, SecretKind


def _row_to_class(row: sqlite3.Row) -> ClassConfig:
    return ClassConfig(
        id=row["id"],
        label=row["label"],
        gradescope_course_id=row["gradescope_course_id"],
        gradescope_email=row["gradescope_email"],
        provenance_base_url=row["provenance_base_url"],
        provenance_semester_id=row["provenance_semester_id"],
        assignment_policy=AssignmentPolicy.parse(row["assignment_policy"]),
        enabled=bool(row["enabled"]),
    )


class Repository:
    def __init__(self, conn: sqlite3.Connection, box: SecretBox) -> None:
        self._conn = conn
        self._box = box

    # --- classes -----------------------------------------------------------
    def add_class(
        self,
        *,
        label: str,
        gradescope_course_id: str,
        gradescope_email: str,
        provenance_base_url: str,
        provenance_semester_id: str,
        assignment_policy: AssignmentPolicy,
        enabled: bool = True,
    ) -> ClassConfig:
        cur = self._conn.execute(
            """
            INSERT INTO classes (label, gradescope_course_id, gradescope_email,
                                 provenance_base_url, provenance_semester_id,
                                 assignment_policy, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                label,
                gradescope_course_id,
                gradescope_email,
                provenance_base_url,
                provenance_semester_id,
                assignment_policy.serialize(),
                int(enabled),
            ),
        )
        self._conn.commit()
        got = self.get_class(label)
        assert got is not None and got.id == cur.lastrowid
        return got

    def get_class(self, label: str) -> ClassConfig | None:
        row = self._conn.execute("SELECT * FROM classes WHERE label = ?", (label,)).fetchone()
        return None if row is None else _row_to_class(row)

    def list_classes(self, *, enabled_only: bool = False) -> list[ClassConfig]:
        sql = "SELECT * FROM classes"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY label"
        return [_row_to_class(r) for r in self._conn.execute(sql)]

    def set_enabled(self, label: str, enabled: bool) -> None:
        self._conn.execute("UPDATE classes SET enabled = ? WHERE label = ?", (int(enabled), label))
        self._conn.commit()

    def remove_class(self, label: str) -> None:
        self._conn.execute("DELETE FROM classes WHERE label = ?", (label,))
        self._conn.commit()

    # --- secrets -----------------------------------------------------------
    def set_secret(self, class_id: int, kind: SecretKind, plaintext: str) -> None:
        self._conn.execute(
            """
            INSERT INTO secrets (class_id, kind, ciphertext) VALUES (?, ?, ?)
            ON CONFLICT(class_id, kind) DO UPDATE SET ciphertext = excluded.ciphertext
            """,
            (class_id, kind.value, self._box.encrypt(plaintext)),
        )
        self._conn.commit()

    def get_secret(self, class_id: int, kind: SecretKind) -> str:
        row = self._conn.execute(
            "SELECT ciphertext FROM secrets WHERE class_id = ? AND kind = ?",
            (class_id, kind.value),
        ).fetchone()
        if row is None:
            raise KeyError(f"no {kind.value} for class {class_id}")
        return self._box.decrypt(row["ciphertext"])

    # --- watermark ---------------------------------------------------------
    def forwarded_keys(self, class_id: int, gs_assignment_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT submission_key FROM forwarded_submissions "
            "WHERE class_id = ? AND gs_assignment_id = ?",
            (class_id, gs_assignment_id),
        )
        return {r["submission_key"] for r in rows}

    def mark_forwarded(
        self,
        class_id: int,
        gs_assignment_id: str,
        keys: Iterable[str],
        job_id: str,
        now_iso: str,
    ) -> None:
        self._conn.executemany(
            """
            INSERT INTO forwarded_submissions
                (class_id, gs_assignment_id, submission_key, provenance_job_id, forwarded_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(class_id, gs_assignment_id, submission_key) DO NOTHING
            """,
            [(class_id, gs_assignment_id, k, job_id, now_iso) for k in keys],
        )
        self._conn.commit()

    # --- runs --------------------------------------------------------------
    def record_run(self, run: RunRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO runs (class_id, gs_assignment_id, outcome, delta_count,
                              job_id, error_summary, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.class_id,
                run.gs_assignment_id,
                run.outcome,
                run.delta_count,
                run.job_id,
                run.error_summary,
                run.started_at,
                run.finished_at,
            ),
        )
        self._conn.commit()

    def recent_runs(self, limit: int = 50) -> list[RunRecord]:
        rows = self._conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [
            RunRecord(
                class_id=r["class_id"],
                gs_assignment_id=r["gs_assignment_id"],
                outcome=r["outcome"],
                delta_count=r["delta_count"],
                job_id=r["job_id"],
                error_summary=r["error_summary"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
            )
            for r in rows
        ]
