"""Constructs real dependencies from settings (kept out of main.py for testability)."""

from __future__ import annotations

import datetime as _dt

import httpx

from provgate.config import Settings
from provgate.gradescope.client import GradescopeClient
from provgate.provenance.client import ProvenanceClient
from provgate.store.crypto import SecretBox
from provgate.store.db import connect
from provgate.store.repository import Repository
from provgate.sync.ports import GradescopeLogin


def open_repo(settings: Settings) -> Repository:
    return Repository(connect(settings.db_path), SecretBox(settings.secret_key))


def real_gs_login(settings: Settings) -> GradescopeLogin:
    def login(email: str, password: str) -> GradescopeClient:
        client = GradescopeClient(
            httpx.Client(follow_redirects=True, timeout=settings.http_timeout_s),
            poll_interval_s=settings.gs_export_poll_interval_s,
            poll_timeout_s=settings.gs_export_poll_timeout_s,
        )
        client.login(email, password)
        return client

    return login


def real_prov(settings: Settings) -> ProvenanceClient:
    return ProvenanceClient(
        httpx.Client(timeout=settings.http_timeout_s),
        poll_interval_s=settings.poll_interval_s,
        poll_timeout_s=settings.poll_timeout_s,
        chunk_threshold_bytes=settings.ingest_chunk_threshold_bytes,
        chunk_size_bytes=settings.ingest_chunk_size_bytes,
    )


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
