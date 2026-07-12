import io
import zipfile

import pytest

from provgate.sync.prune import NotAnExportError, prune_export
from tests.support.export_fixture import make_export


def _names(zip_bytes: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        return set(z.namelist())


def test_prune_keeps_only_new_submissions() -> None:
    export = make_export(
        {
            "submission_1": {"sid": "s1", "files": {"manifest.json": b"a"}},
            "submission_2": {"sid": "s2", "files": {"manifest.json": b"b"}},
        }
    )
    pruned = prune_export(export, already_forwarded={"submission_1"})
    assert pruned.forwarded_keys == frozenset({"submission_2"})
    assert pruned.total_submissions == 2
    names = _names(pruned.zip_bytes)
    assert "assignment_export/submission_metadata.yml" in names
    assert "assignment_export/submission_2/manifest.json" in names
    assert not any("submission_1/" in n for n in names)


def test_metadata_copied_verbatim() -> None:
    export = make_export({"submission_1": {"sid": "s1", "files": {"manifest.json": b"a"}}})
    with zipfile.ZipFile(io.BytesIO(export)) as z:
        original = z.read("assignment_export/submission_metadata.yml")
    pruned = prune_export(export, already_forwarded=set())
    with zipfile.ZipFile(io.BytesIO(pruned.zip_bytes)) as z:
        assert z.read("assignment_export/submission_metadata.yml") == original


def test_macos_noise_dropped() -> None:
    export = make_export({"submission_1": {"sid": "s1", "files": {"manifest.json": b"a"}}})
    names = _names(prune_export(export, set()).zip_bytes)
    assert not any("__MACOSX" in n or n.endswith(".DS_Store") for n in names)


def test_empty_delta_when_all_forwarded() -> None:
    export = make_export({"submission_1": {"sid": "s1", "files": {"manifest.json": b"a"}}})
    pruned = prune_export(export, already_forwarded={"submission_1"})
    assert pruned.forwarded_keys == frozenset()
    assert not any("submission_1/" in n for n in _names(pruned.zip_bytes))


def test_not_an_export_raises() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("random.txt", b"x")
    with pytest.raises(NotAnExportError):
        prune_export(buf.getvalue(), set())


def test_garbage_bytes_raise() -> None:
    with pytest.raises(NotAnExportError):
        prune_export(b"not a zip", set())
