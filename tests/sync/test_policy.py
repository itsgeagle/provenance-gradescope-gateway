from provgate.store.models import AssignmentPolicy, PolicyKind
from provgate.sync.policy import resolve_assignments

ALL = ["100", "200", "300"]


def test_all() -> None:
    assert resolve_assignments(AssignmentPolicy(PolicyKind.ALL), ALL) == ["100", "200", "300"]


def test_include_keeps_order_and_ignores_unknown() -> None:
    p = AssignmentPolicy(PolicyKind.INCLUDE, ("300", "999", "100"))
    assert resolve_assignments(p, ALL) == ["100", "300"]


def test_exclude() -> None:
    p = AssignmentPolicy(PolicyKind.EXCLUDE, ("200",))
    assert resolve_assignments(p, ALL) == ["100", "300"]
