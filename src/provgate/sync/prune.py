"""Prune a Gradescope export ZIP down to not-yet-forwarded submissions.

Pure function, no I/O. Enumerates submission folder keys from the ZIP's
top-level `submission_*` folders (identical to the metadata keys by Gradescope's
construction) and copies `submission_metadata.yml` verbatim — never rewriting it.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

METADATA_FILENAME = "submission_metadata.yml"


class NotAnExportError(Exception):
    """The bytes are not a Gradescope export (not a ZIP, or no metadata file)."""


@dataclass(frozen=True)
class PrunedExport:
    zip_bytes: bytes
    forwarded_keys: frozenset[str]
    total_submissions: int


def _is_noise(name: str) -> bool:
    return "__MACOSX/" in name or name.endswith("/.DS_Store") or name.endswith(".DS_Store")


def _locate_metadata(names: list[str]) -> str:
    candidates = [
        n
        for n in names
        if (n == METADATA_FILENAME or n.endswith("/" + METADATA_FILENAME)) and "__MACOSX/" not in n
    ]
    if not candidates:
        raise NotAnExportError(f"no {METADATA_FILENAME} in archive")
    return min(candidates, key=len)


def _key_for(name: str, export_prefix: str) -> str | None:
    if not name.startswith(export_prefix):
        return None
    rest = name[len(export_prefix) :]
    if "/" not in rest:
        return None  # a file directly at the export root (e.g. the metadata)
    return rest.split("/", 1)[0]


def prune_export(source: Path | bytes, already_forwarded: set[str]) -> PrunedExport:
    src: Path | io.BytesIO = io.BytesIO(source) if isinstance(source, bytes) else source
    try:
        zin = zipfile.ZipFile(src)
    except zipfile.BadZipFile as e:
        raise NotAnExportError("not a valid ZIP") from e

    with zin:
        names = zin.namelist()
        meta_name = _locate_metadata(names)
        export_prefix = meta_name[: -len(METADATA_FILENAME)]

        all_keys: set[str] = set()
        for name in names:
            if _is_noise(name):
                continue
            key = _key_for(name, export_prefix)
            if key is not None:
                all_keys.add(key)

        new_keys = {k for k in all_keys if k not in already_forwarded}

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            zout.writestr(meta_name, zin.read(meta_name))
            for name in names:
                if name == meta_name or _is_noise(name):
                    continue
                key = _key_for(name, export_prefix)
                if key is not None and key in new_keys:
                    zout.writestr(name, zin.read(name))

    return PrunedExport(
        zip_bytes=out.getvalue(),
        forwarded_keys=frozenset(new_keys),
        total_submissions=len(all_keys),
    )
