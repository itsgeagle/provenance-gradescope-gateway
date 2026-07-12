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
