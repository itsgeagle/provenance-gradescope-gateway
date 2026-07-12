"""Persistence repository over the SQLite store."""

from __future__ import annotations

import sqlite3

from .crypto import SecretBox
from .models import AssignmentPolicy, ClassConfig, SecretKind


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
