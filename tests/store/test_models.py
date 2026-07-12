import pytest

from provgate.store.models import AssignmentPolicy, PolicyKind


def test_parse_all() -> None:
    p = AssignmentPolicy.parse("all")
    assert p == AssignmentPolicy(PolicyKind.ALL, ())
    assert p.serialize() == "all"


def test_parse_include() -> None:
    p = AssignmentPolicy.parse("include:872677, 872690")
    assert p == AssignmentPolicy(PolicyKind.INCLUDE, ("872677", "872690"))
    assert p.serialize() == "include:872677,872690"


def test_parse_exclude() -> None:
    p = AssignmentPolicy.parse("exclude:900001")
    assert p == AssignmentPolicy(PolicyKind.EXCLUDE, ("900001",))
    assert p.serialize() == "exclude:900001"


def test_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        AssignmentPolicy.parse("include:")
    with pytest.raises(ValueError):
        AssignmentPolicy.parse("nonsense")
