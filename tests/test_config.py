from pathlib import Path

import pytest

from provgate.config import ConfigError, load_settings


def test_load_settings_from_env() -> None:
    s = load_settings({"PROVGATE_DB_PATH": "/tmp/x.db", "PROVGATE_SECRET_KEY": "k"})
    assert s.db_path == Path("/tmp/x.db")
    assert s.secret_key == "k"
    assert s.poll_interval_s == 2.0


def test_missing_db_path_raises() -> None:
    with pytest.raises(ConfigError):
        load_settings({"PROVGATE_SECRET_KEY": "k"})


def test_missing_secret_key_raises() -> None:
    with pytest.raises(ConfigError):
        load_settings({"PROVGATE_DB_PATH": "/tmp/x.db"})
