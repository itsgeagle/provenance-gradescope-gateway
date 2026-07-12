import pytest

from provgate.store.crypto import SecretBox, generate_key
from provgate.store.db import connect
from provgate.store.models import AssignmentPolicy, PolicyKind, SecretKind
from provgate.store.repository import Repository


def make_repo() -> Repository:
    return Repository(connect(":memory:"), SecretBox(generate_key()))


def test_add_and_get_class() -> None:
    repo = make_repo()
    c = repo.add_class(
        label="cs61a",
        gradescope_course_id="180852",
        gradescope_email="staff@example.edu",
        provenance_base_url="https://prov.example.edu/api/v1",
        provenance_semester_id="sem-1",
        assignment_policy=AssignmentPolicy(PolicyKind.ALL),
    )
    assert c.id > 0
    got = repo.get_class("cs61a")
    assert got == c
    assert got.assignment_policy == AssignmentPolicy(PolicyKind.ALL)


def test_get_missing_class_returns_none() -> None:
    assert make_repo().get_class("nope") is None


def test_list_enabled_only() -> None:
    repo = make_repo()
    repo.add_class(
        label="a",
        gradescope_course_id="1",
        gradescope_email="e",
        provenance_base_url="u",
        provenance_semester_id="s",
        assignment_policy=AssignmentPolicy(PolicyKind.ALL),
    )
    repo.add_class(
        label="b",
        gradescope_course_id="2",
        gradescope_email="e",
        provenance_base_url="u",
        provenance_semester_id="s",
        assignment_policy=AssignmentPolicy(PolicyKind.ALL),
        enabled=False,
    )
    assert {c.label for c in repo.list_classes(enabled_only=True)} == {"a"}
    assert {c.label for c in repo.list_classes()} == {"a", "b"}


def test_secret_roundtrip_is_encrypted_at_rest() -> None:
    repo = make_repo()
    c = repo.add_class(
        label="a",
        gradescope_course_id="1",
        gradescope_email="e",
        provenance_base_url="u",
        provenance_semester_id="s",
        assignment_policy=AssignmentPolicy(PolicyKind.ALL),
    )
    repo.set_secret(c.id, SecretKind.GRADESCOPE_PASSWORD, "pw123")
    assert repo.get_secret(c.id, SecretKind.GRADESCOPE_PASSWORD) == "pw123"
    # stored bytes are not the plaintext
    row = repo._conn.execute(  # noqa: SLF001 - test inspects storage on purpose
        "SELECT ciphertext FROM secrets WHERE class_id=?", (c.id,)
    ).fetchone()
    assert b"pw123" not in row["ciphertext"]


def test_set_secret_twice_upserts_to_second_value() -> None:
    repo = make_repo()
    c = repo.add_class(
        label="a",
        gradescope_course_id="1",
        gradescope_email="e",
        provenance_base_url="u",
        provenance_semester_id="s",
        assignment_policy=AssignmentPolicy(PolicyKind.ALL),
    )
    repo.set_secret(c.id, SecretKind.GRADESCOPE_PASSWORD, "first")
    repo.set_secret(c.id, SecretKind.GRADESCOPE_PASSWORD, "second")
    assert repo.get_secret(c.id, SecretKind.GRADESCOPE_PASSWORD) == "second"


def test_get_missing_secret_raises() -> None:
    repo = make_repo()
    c = repo.add_class(
        label="a",
        gradescope_course_id="1",
        gradescope_email="e",
        provenance_base_url="u",
        provenance_semester_id="s",
        assignment_policy=AssignmentPolicy(PolicyKind.ALL),
    )
    with pytest.raises(KeyError):
        repo.get_secret(c.id, SecretKind.PROVENANCE_TOKEN)
