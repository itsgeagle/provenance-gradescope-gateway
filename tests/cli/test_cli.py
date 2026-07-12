import json

import httpx
import respx
from typer.testing import CliRunner

from provgate.cli.main import app

runner = CliRunner()


def test_keygen_prints_a_key() -> None:
    result = runner.invoke(app, ["keygen"])
    assert result.exit_code == 0
    assert len(result.stdout.strip()) > 20


def test_class_add_and_list(tmp_path, monkeypatch) -> None:
    from provgate.store.crypto import generate_key

    monkeypatch.setenv("PROVGATE_DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("PROVGATE_SECRET_KEY", generate_key())
    # secrets provided on stdin (gradescope password, then provenance token)
    add = runner.invoke(
        app,
        [
            "class",
            "add",
            "--label",
            "cs61a",
            "--gradescope-course",
            "180852",
            "--gradescope-email",
            "staff@example.edu",
            "--provenance-base-url",
            "https://prov/api/v1",
            "--provenance-semester",
            "sem-1",
            "--assignments",
            "all",
        ],
        input="gs-password\nprov-token\n",
    )
    assert add.exit_code == 0, add.stdout
    listed = runner.invoke(app, ["class", "list"])
    assert "cs61a" in listed.stdout
    # secret values must never be echoed
    assert "gs-password" not in add.stdout
    assert "prov-token" not in add.stdout


def test_class_edit_updates_course_and_assignments(tmp_path, monkeypatch) -> None:
    from provgate.store.crypto import generate_key

    monkeypatch.setenv("PROVGATE_DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("PROVGATE_SECRET_KEY", generate_key())
    add = runner.invoke(
        app,
        [
            "class",
            "add",
            "--label",
            "cs61a",
            "--gradescope-course",
            "180852",
            "--gradescope-email",
            "staff@example.edu",
            "--provenance-base-url",
            "https://prov/api/v1",
            "--provenance-semester",
            "sem-1",
            "--assignments",
            "all",
        ],
        input="gs-password\nprov-token\n",
    )
    assert add.exit_code == 0, add.stdout

    edit = runner.invoke(
        app,
        [
            "class",
            "edit",
            "cs61a",
            "--gradescope-course",
            "999999",
            "--assignments",
            "include:1,2",
        ],
    )
    assert edit.exit_code == 0, edit.stdout

    listed = runner.invoke(app, ["class", "list"])
    assert "course=999999" in listed.stdout
    assert "policy=include:1,2" in listed.stdout


def test_class_edit_missing_label_fails(tmp_path, monkeypatch) -> None:
    from provgate.store.crypto import generate_key

    monkeypatch.setenv("PROVGATE_DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("PROVGATE_SECRET_KEY", generate_key())
    result = runner.invoke(app, ["class", "edit", "nope"])
    assert result.exit_code != 0


def test_class_edit_rotate_gs_password_reads_stdin_without_echo(tmp_path, monkeypatch) -> None:
    from provgate.store.crypto import generate_key

    monkeypatch.setenv("PROVGATE_DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("PROVGATE_SECRET_KEY", generate_key())
    add = runner.invoke(
        app,
        [
            "class",
            "add",
            "--label",
            "cs61a",
            "--gradescope-course",
            "180852",
            "--gradescope-email",
            "staff@example.edu",
            "--provenance-base-url",
            "https://prov/api/v1",
            "--provenance-semester",
            "sem-1",
            "--assignments",
            "all",
        ],
        input="gs-password\nprov-token\n",
    )
    assert add.exit_code == 0, add.stdout

    edit = runner.invoke(
        app,
        ["class", "edit", "cs61a", "--rotate-gs-password"],
        input="new-secret-password\n",
    )
    assert edit.exit_code == 0, edit.stdout
    assert "new-secret-password" not in edit.stdout


def test_doctor_missing_class_fails(tmp_path, monkeypatch) -> None:
    from provgate.store.crypto import generate_key

    monkeypatch.setenv("PROVGATE_DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("PROVGATE_SECRET_KEY", generate_key())
    result = runner.invoke(app, ["doctor", "--class", "nope"])
    assert result.exit_code != 0


def test_sync_all_flag_accepted_with_no_classes(tmp_path, monkeypatch) -> None:
    from provgate.store.crypto import generate_key

    monkeypatch.setenv("PROVGATE_DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("PROVGATE_SECRET_KEY", generate_key())
    result = runner.invoke(app, ["sync", "--all", "--dry-run"])
    assert result.exit_code == 0, result.stdout


def test_sync_rejects_class_and_all_together(tmp_path, monkeypatch) -> None:
    from provgate.store.crypto import generate_key

    monkeypatch.setenv("PROVGATE_DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("PROVGATE_SECRET_KEY", generate_key())
    result = runner.invoke(app, ["sync", "--class", "x", "--all"])
    assert result.exit_code != 0


@respx.mock
def test_sync_posts_webhook_summary_when_url_set(tmp_path, monkeypatch) -> None:
    from provgate.store.crypto import generate_key

    route = respx.post("https://hooks.example.com/wh").mock(return_value=httpx.Response(204))
    monkeypatch.setenv("PROVGATE_DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("PROVGATE_SECRET_KEY", generate_key())
    monkeypatch.setenv("PROVGATE_WEBHOOK_URL", "https://hooks.example.com/wh")

    result = runner.invoke(app, ["sync", "--all"])
    assert result.exit_code == 0, result.stdout
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert "provgate sync" in body["content"]


@respx.mock
def test_sync_no_webhook_when_url_unset(tmp_path, monkeypatch) -> None:
    from provgate.store.crypto import generate_key

    route = respx.post("https://hooks.example.com/wh").mock(return_value=httpx.Response(204))
    monkeypatch.setenv("PROVGATE_DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("PROVGATE_SECRET_KEY", generate_key())
    # no PROVGATE_WEBHOOK_URL
    result = runner.invoke(app, ["sync", "--all"])
    assert result.exit_code == 0, result.stdout
    assert not route.called
