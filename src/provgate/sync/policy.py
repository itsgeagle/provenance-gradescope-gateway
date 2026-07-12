"""Resolve an assignment policy against a course's assignment list."""

from __future__ import annotations

from collections.abc import Sequence

from provgate.store.models import AssignmentPolicy, PolicyKind


def resolve_assignments(policy: AssignmentPolicy, all_ids: Sequence[str]) -> list[str]:
    if policy.kind is PolicyKind.ALL:
        return list(all_ids)
    wanted = set(policy.ids)
    if policy.kind is PolicyKind.INCLUDE:
        return [i for i in all_ids if i in wanted]
    return [i for i in all_ids if i not in wanted]
