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


def test_timeout_env_vars_override_defaults() -> None:
    s = load_settings(
        {
            "PROVGATE_DB_PATH": "/tmp/x.db",
            "PROVGATE_SECRET_KEY": "k",
            "PROVGATE_POLL_INTERVAL_S": "5",
            "PROVGATE_POLL_TIMEOUT_S": "120",
            "PROVGATE_HTTP_TIMEOUT_S": "30",
        }
    )
    assert s.poll_interval_s == 5.0
    assert s.poll_timeout_s == 120.0
    assert s.http_timeout_s == 30.0


def test_timeout_env_vars_omitted_keeps_defaults() -> None:
    s = load_settings({"PROVGATE_DB_PATH": "/tmp/x.db", "PROVGATE_SECRET_KEY": "k"})
    assert s.poll_interval_s == 2.0
    assert s.poll_timeout_s == 600.0
    assert s.http_timeout_s == 60.0


def test_webhook_settings_default_and_override() -> None:
    base = {"PROVGATE_DB_PATH": "/tmp/x.db", "PROVGATE_SECRET_KEY": "k"}
    s = load_settings(base)
    assert s.webhook_url is None
    assert s.webhook_timeout_s == 10.0
    s2 = load_settings(
        {**base, "PROVGATE_WEBHOOK_URL": "https://h/wh", "PROVGATE_WEBHOOK_TIMEOUT_S": "3.5"}
    )
    assert s2.webhook_url == "https://h/wh"
    assert s2.webhook_timeout_s == 3.5


def test_chunk_settings_default_and_override() -> None:
    base = {"PROVGATE_DB_PATH": "/tmp/x.db", "PROVGATE_SECRET_KEY": "k"}
    s = load_settings(base)
    assert s.ingest_chunk_threshold_bytes == 16 * 1024 * 1024
    assert s.ingest_chunk_size_bytes == 16 * 1024 * 1024
    s2 = load_settings(
        {
            **base,
            "PROVGATE_INGEST_CHUNK_THRESHOLD_BYTES": "1048576",
            "PROVGATE_INGEST_CHUNK_SIZE_BYTES": "524288",
        }
    )
    assert s2.ingest_chunk_threshold_bytes == 1048576
    assert s2.ingest_chunk_size_bytes == 524288


def test_gs_export_poll_settings_default_and_override() -> None:
    base = {"PROVGATE_DB_PATH": "/tmp/x.db", "PROVGATE_SECRET_KEY": "k"}
    s = load_settings(base)
    assert s.gs_export_poll_interval_s == 5.0
    assert s.gs_export_poll_timeout_s == 600.0
    s2 = load_settings(
        {
            **base,
            "PROVGATE_GS_EXPORT_POLL_INTERVAL_S": "2",
            "PROVGATE_GS_EXPORT_POLL_TIMEOUT_S": "120",
        }
    )
    assert s2.gs_export_poll_interval_s == 2.0
    assert s2.gs_export_poll_timeout_s == 120.0
