"""Build in-memory Gradescope export ZIPs for tests."""

from __future__ import annotations

import io
import zipfile


def make_export(
    submissions: dict[str, dict],
    *,
    prefix: str = "assignment_export/",
    macos_noise: bool = True,
) -> bytes:
    lines: list[str] = []
    for key, sub in submissions.items():
        lines.append(f"{key}:")
        lines.append("  :submitters:")
        lines.append(f"    - :sid: '{sub['sid']}'")
    meta_yaml = "\n".join(lines) + "\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(prefix + "submission_metadata.yml", meta_yaml)
        for key, sub in submissions.items():
            for fname, data in sub["files"].items():
                z.writestr(f"{prefix}{key}/{fname}", data)
        if macos_noise:
            z.writestr("__MACOSX/._x", b"noise")
            z.writestr(prefix + ".DS_Store", b"noise")
    return buf.getvalue()
