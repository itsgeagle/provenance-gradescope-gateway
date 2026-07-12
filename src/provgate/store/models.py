"""Domain models + assignment-policy parsing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PolicyKind(StrEnum):
    ALL = "all"
    INCLUDE = "include"
    EXCLUDE = "exclude"


@dataclass(frozen=True)
class AssignmentPolicy:
    kind: PolicyKind
    ids: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: str) -> AssignmentPolicy:
        raw = raw.strip()
        if raw == "all":
            return cls(PolicyKind.ALL, ())
        for prefix, kind in (("include:", PolicyKind.INCLUDE), ("exclude:", PolicyKind.EXCLUDE)):
            if raw.startswith(prefix):
                ids = tuple(x.strip() for x in raw[len(prefix) :].split(",") if x.strip())
                if not ids:
                    raise ValueError(f"{kind.value} policy needs at least one id")
                return cls(kind, ids)
        raise ValueError(f"unrecognized assignment policy: {raw!r}")

    def serialize(self) -> str:
        if self.kind is PolicyKind.ALL:
            return "all"
        return f"{self.kind.value}:{','.join(self.ids)}"


class SecretKind(StrEnum):
    GRADESCOPE_PASSWORD = "gradescope_password"
    PROVENANCE_TOKEN = "provenance_token"


@dataclass(frozen=True)
class ClassConfig:
    id: int
    label: str
    gradescope_course_id: str
    gradescope_email: str
    provenance_base_url: str
    provenance_semester_id: str
    assignment_policy: AssignmentPolicy
    enabled: bool


@dataclass(frozen=True)
class RunRecord:
    class_id: int
    gs_assignment_id: str
    outcome: str
    delta_count: int
    job_id: str | None
    error_summary: str | None
    started_at: str
    finished_at: str
