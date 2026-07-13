"""Live spike: observe how Gradescope actually serves a bulk submission export.

This is NOT a normal test — it is a diagnostic capture, gated behind
``@pytest.mark.live`` (excluded from the default suite). Its job is to record the
*ground truth* of the undocumented export download so we can write a spec against
observed behavior instead of assumptions (see
docs/superpowers/specs/... download follow-up).

What it answers:
  1. Sync vs. async — the full status/redirect chain on
     ``GET /courses/{cid}/assignments/{aid}/export/without_evaluations``: direct
     ZIP, a 302 to a generation/status URL, or an HTML "preparing" page?
  2. Transport — Content-Type / Content-Disposition / Content-Length /
     Transfer-Encoding (is the body streamable? how big?).
  3. Timing — how long each hop takes (informs poll interval/timeout).
  4. ZIP shape — the real top-level folder name (never assume it), whether
     ``submission_metadata.yml`` is present, and the folder-per-submission
     pattern — as a STRUCTURAL SUMMARY, not a file dump.

Hygiene (both are hard project rules):
  * Secrets never leak: the password and CSRF token are never printed; only a
    whitelist of safe response headers is recorded (never Cookie/Set-Cookie/
    Authorization).
  * Student source is never persisted: the ZIP is streamed to a temp file purely
    to observe transport shape, only its central directory (entry names + sizes)
    is read — student file *contents* are never decompressed — and the temp file
    is deleted in a ``finally``. The transcript is written to a gitignored path.

Run it yourself (so your credential never routes through the assistant):

    GS_LIVE_EMAIL=you@school.edu \\
    GS_LIVE_PASSWORD='...' \\
    GS_LIVE_COURSE_ID=123456 \\
    GS_LIVE_ASSIGNMENT_ID=7890123 \\
    uv run pytest -m live tests/gradescope/test_export_live.py -s

Pick the LARGEST assignment you have staff access to — a big export is what
exercises the async/streaming path we care about. Optional overrides:
``GS_LIVE_BASE_URL`` (default https://www.gradescope.com),
``GS_LIVE_OUT`` (default gradescope-spike/transcript.md, gitignored).

By default the run is READ-ONLY (discovery only). To exercise the real
create→poll→download flow — which **creates a bulk-export job on the assignment**,
exactly like clicking "Download Submissions" in the UI — opt in with
``GS_LIVE_EXERCISE=1``. Poll cadence is bounded by ``GS_LIVE_POLL_MAX`` (default
40) and ``GS_LIVE_POLL_INTERVAL`` seconds (default 5).
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import httpx
import pytest

from provgate.gradescope.client import GradescopeClient

pytestmark = pytest.mark.live

# Gradescope embeds a `gon.page_context` blob in every HTML page carrying a
# per-session CSRF token and user/org identifiers. Scrub these from any captured
# snippet before it is written or printed — they must never land in the transcript.
_SECRET_PATTERNS = (
    re.compile(r'("csrf_token"\s*:\s*")[^"]*(")'),
    re.compile(r'("authenticity_token"\s*:\s*")[^"]*(")'),
    re.compile(r'(name="authenticity_token"\s+value=")[^"]*(")'),
    re.compile(r'("(?:user|org)_trn"\s*:\s*")[^"]*(")'),
    re.compile(r"(trn:[a-z]+:[a-z]*:[a-z]*:[^\s\"']*)"),
)


# A presigned S3 URL is a bearer capability — its query string (X-Amz-Signature et al.)
# grants time-limited download access to anyone holding it. Strip the query entirely.
_SIGNED_URL_RE = re.compile(r"""(https?://[^\s"']*?)\?[^\s"']*X-Amz-[^\s"']*""")


def _scrub(text: str) -> str:
    out = _SIGNED_URL_RE.sub(r"\1?<redacted-signed-url>", text)
    for pat in _SECRET_PATTERNS:
        out = pat.sub(
            lambda m: (m.group(1) + "REDACTED" + m.group(2)) if m.lastindex else "REDACTED", out
        )
    return out


def _find_export_candidates(html: str) -> list[str]:
    """Surface every URL-ish string containing 'export' or a submissions download
    — this is how we discover the real export route without guessing it."""
    cands: set[str] = set()
    for m in re.finditer(
        r"""["'(]([^"'()\s]*(?:export|submissions/[^"'()\s]*download)[^"'()\s]*)""", html
    ):
        cands.add(m.group(1))
    return sorted(cands)


def _extract_flow_gon(html: str) -> dict[str, str]:
    """Pull `gon.<name>=<value>` assignments whose name mentions the bulk-export
    flow (export / generated / progress / poll / download) — this is where
    Gradescope stashes the real create/status/download paths for the JS flow
    (e.g. gon.create_bulk_export_path). Values are paths, not secrets."""
    out: dict[str, str] = {}
    for m in re.finditer(
        r"""gon\.([A-Za-z_][A-Za-z0-9_]*"""
        r"""(?:export|generated|progress|poll|download)[A-Za-z0-9_]*)\s*=\s*("?)([^";]*)\2""",
        html,
    ):
        out[m.group(1)] = m.group(3)
    return out


def _all_gon_names(html: str) -> list[str]:
    """Every `gon.<name>` on the page — the full menu, so we can spot whatever var
    actually holds the status/poll path (names are not secrets; values are omitted)."""
    return sorted({m.group(1) for m in re.finditer(r"gon\.([A-Za-z_][A-Za-z0-9_]*)\s*=", html)})


def _find_generated_file_urls(html: str) -> list[str]:
    """URL-ish strings mentioning generated_file(s) — the poll/download route often
    appears here as a template (with a :id / {id} / %7B placeholder)."""
    cands: set[str] = set()
    for m in re.finditer(r"""["'(]([^"'()\s]*generated_files?[^"'()\s]*)""", html):
        cands.add(m.group(1))
    return sorted(cands)


def _extract_export_form_fields(html: str) -> list[str]:
    """Dump the bulk-export modal's form controls — these reveal the create-POST
    params, i.e. whether there is a 'without evaluations' option (CLAUDE.md: raw
    submissions only, never grades). Empty result => modal is JS-rendered and the
    params aren't in server HTML (would need a browser to see)."""
    anchors = [m.start() for m in re.finditer(r"bulk[-_]export", html)]
    if not anchors:
        return []
    window = html[anchors[0] : anchors[0] + 8000]
    fields: list[str] = []
    for m in re.finditer(r"<form\b[^>]*>", window):
        action = re.search(r'action="([^"]*)"', m.group(0))
        method = re.search(r'method="([^"]*)"', m.group(0))
        if action:
            fields.append(
                f"form action={action.group(1)!r} method={method.group(1) if method else None!r}"
            )
    for m in re.finditer(r"<input\b[^>]*>", window):
        tag = m.group(0)
        name = re.search(r'name="([^"]*)"', tag)
        typ = re.search(r'type="([^"]*)"', tag)
        val = re.search(r'value="([^"]*)"', tag)
        if name or typ:
            name_str = name.group(1) if name else None
            # Redact values of auth/csrf token fields at the source (their string
            # form here wouldn't be caught by _scrub's HTML-shaped patterns).
            sensitive = name_str is not None and re.search(r"token|csrf|auth", name_str, re.I)
            val_str = "<redacted>" if sensitive else (val.group(1) if val else None)
            fields.append(
                f"input name={name_str!r} type={(typ.group(1) if typ else None)!r} "
                f"value={val_str!r} checked={'checked' in tag}"
            )
    for m in re.finditer(r'<select\b[^>]*name="([^"]*)"', window):
        fields.append(f"select name={m.group(1)!r}")
    for m in re.finditer(r'<option\b[^>]*value="([^"]*)"[^>]*>([^<]*)</option>', window):
        fields.append(f"option value={m.group(1)!r} label={m.group(2).strip()!r}")
    for m in re.finditer(r"<label\b[^>]*>([^<]+)</label>", window):
        txt = m.group(1).strip()
        if txt:
            fields.append(f"label {txt!r}")
    return fields


def _has_csrf_meta(html: str) -> bool:
    """A POST to the create endpoint needs the `csrf-token` meta value as an
    X-CSRF-Token header. Report only presence (never the value)."""
    return re.search(r'<meta[^>]+name="csrf-token"', html) is not None


# Response headers safe to record verbatim (whitelist — never dump all headers,
# which would include Set-Cookie). None of these carry secrets.
_SAFE_HEADERS = (
    "location",
    "content-type",
    "content-disposition",
    "content-length",
    "transfer-encoding",
    "retry-after",
    "cache-control",
    "date",
)

_MAX_HOPS = 10
_HTML_SNIPPET_CHARS = 800


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def _require(name: str) -> str:
    value = _env(name)
    if value is None:
        pytest.skip(f"live spike needs {name} in the environment")
    return value


def _safe_headers(resp: httpx.Response) -> dict[str, str]:
    return {k: resp.headers[k] for k in _SAFE_HEADERS if k in resp.headers}


def _summarize_zip(path: Path) -> list[str]:
    """Read ONLY the ZIP central directory (names + sizes). Never decompress
    student file contents. Returns lines for the transcript."""
    lines: list[str] = []
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        top_level = sorted({n.split("/", 1)[0] for n in names if n})
        dirs = [n for n in names if n.endswith("/")]
        has_meta = any(n.endswith("submission_metadata.yml") for n in names)
        lines.append(f"- entries: {len(infos)}")
        lines.append(
            f"- top-level names ({len(top_level)}): {top_level[:5]}"
            + (" …" if len(top_level) > 5 else "")
        )
        lines.append(f"- directory entries: {len(dirs)}")
        lines.append(f"- submission_metadata.yml present: {has_meta}")
        # Show the shape of the first few paths (depth/pattern) WITHOUT dumping
        # every student filename: just the first two path components.
        sample = []
        for n in names[:8]:
            parts = n.split("/")
            shape = "/".join(parts[:2]) + ("/…" if len(parts) > 2 else "")
            sample.append(shape)
        lines.append(f"- path shape sample: {sample}")
    return lines


def _download_and_summarize(http: httpx.Client, url: str, transcript: list[str]) -> None:
    """Stream a finished export ZIP to a temp file to observe transport shape +
    central directory, then delete it. Never decompresses student contents."""
    tmp: Path | None = None
    try:
        with http.stream("GET", url, follow_redirects=True) as resp:
            transcript.append(f"  → GET download: {resp.status_code} headers={_safe_headers(resp)}")
            if resp.status_code != 200:
                return
            fd, name = tempfile.mkstemp(prefix="gs-spike-", suffix=".zip")
            tmp = Path(name)
            total = 0
            with os.fdopen(fd, "wb") as fh:
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    fh.write(chunk)
            transcript.append(f"  → body streamed: {total} bytes")
        try:
            transcript.append("  ### downloaded ZIP structure (central directory only)")
            transcript.extend("  " + line for line in _summarize_zip(tmp))
        except zipfile.BadZipFile:
            transcript.append("  → downloaded body was NOT a valid zip")
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _exercise_bulk_export(
    http: httpx.Client, base: str, create_path: str, page_html: str, transcript: list[str]
) -> None:
    """MUTATION: POST the create endpoint (same as clicking 'Download Submissions'),
    then poll to ready and download. Self-documenting: dumps the create response
    shape so we can wire poll/download precisely even if the heuristics miss."""
    csrf_m = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', page_html)
    headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
    if csrf_m:
        headers["X-CSRF-Token"] = csrf_m.group(1)  # used in-request, never logged
    create_url = urljoin(base + "/", create_path.lstrip("/"))

    t0 = time.monotonic()
    resp = http.post(create_url, headers=headers, follow_redirects=False)
    transcript.append(
        f"- POST {create_path}: {resp.status_code} ({time.monotonic() - t0:.2f}s) "
        f"headers={_safe_headers(resp)}"
    )
    body = resp.text
    transcript.append(f"  → create response snippet: {_scrub(body[:_HTML_SNIPPET_CHARS])!r}")

    data = None
    try:
        data = resp.json()
    except ValueError:
        transcript.append("  → create response is not JSON")

    export_id = None
    status_url = None
    if isinstance(data, dict):
        transcript.append(f"  → create JSON keys: {sorted(data.keys())}")
        for k in ("generated_file_id", "id", "export_id", "bulk_export_id"):
            if data.get(k) is not None:
                export_id = data[k]
                break
        for k in ("status_url", "url", "path", "poll_url"):
            if isinstance(data.get(k), str):
                status_url = data[k]
                break
    transcript.append(f"  → parsed export_id={export_id!r}, status_url={_scrub(str(status_url))!r}")

    if export_id is None:
        transcript.append("  → no export id in create response; cannot poll (see snippet above)")
        return

    # create_path is …/assignments/{aid}/export; the generated file lives under the
    # assignment. Probe the likely status routes, lock onto the first that answers.
    assignment_path = (
        create_path[: create_path.rfind("/export")] if "/export" in create_path else create_path
    )
    # The generated file is a COURSE-level resource: /courses/{cid}/generated_files/{id}
    # (per gon.generated_file_path), NOT under the assignment — which is why the
    # assignment-scoped probes 404'd on the prior run.
    course_path = assignment_path.split("/assignments/")[0]
    candidates: list[str] = []
    if status_url:
        candidates.append(
            urljoin(base + "/", status_url.lstrip("/"))
            if status_url.startswith("/")
            else status_url
        )
    candidates += [
        f"{base}{course_path}/generated_files/{export_id}.json",
        f"{base}{course_path}/generated_files/{export_id}",
        f"{base}{assignment_path}/generated_files/{export_id}.json",
    ]

    poll_url: str | None = None
    transcript.append("  → probing status routes:")
    for cand in candidates:
        pr = http.get(cand, headers={"Accept": "application/json"})
        transcript.append(f"    probe {cand.replace(base, '')}: {pr.status_code}")
        if pr.status_code != 404:
            poll_url = cand
            break
    if poll_url is None:
        transcript.append("  → all status-route probes 404'd; inspect create response shape above")
        return
    transcript.append(f"  → polling: {poll_url.replace(base, '')}")

    poll_max = int(os.environ.get("GS_LIVE_POLL_MAX", "40"))
    poll_interval = float(os.environ.get("GS_LIVE_POLL_INTERVAL", "5"))
    download_url = None
    ready = False
    for i in range(poll_max):
        pr = http.get(poll_url, headers={"Accept": "application/json"})
        pdata = None
        try:
            pdata = pr.json()
        except ValueError:
            pass
        if isinstance(pdata, dict):
            prog = pdata.get("progress", pdata.get("percent"))
            status = pdata.get("status")
            # The status carries a `url` from progress=0 (pointing at the not-yet-
            # generated S3 object). Keep the latest, but only treat it as usable
            # once the job reports done — else the download 404s on S3.
            for k in ("url", "download_url", "download"):
                if isinstance(pdata.get(k), str):
                    download_url = pdata[k]
                    break
            transcript.append(
                f"    poll {i}: {pr.status_code} progress={prog} status={status!r} "
                f"keys={sorted(pdata.keys())}"
            )
            done = (isinstance(prog, (int, float)) and prog >= 100) or (
                isinstance(status, str)
                and status.lower() in ("complete", "completed", "done", "ready", "success")
            )
            if done:
                ready = True
                break
        else:
            transcript.append(f"    poll {i}: {pr.status_code} (non-JSON)")
        time.sleep(poll_interval)

    if not ready:
        transcript.append("  → export not ready within poll budget (raise GS_LIVE_POLL_MAX)")
        return

    # Ready. Prefer the signed URL from the status; else the conventional .zip sibling.
    if not download_url:
        download_url = f"{course_path}/generated_files/{export_id}.zip"
        transcript.append(f"  → no url in status; trying conventional {download_url}")
    transcript.append(f"  → download target: {_scrub(str(download_url))}")
    durl = (
        urljoin(base + "/", download_url.lstrip("/"))
        if download_url.startswith("/")
        else download_url
    )
    _download_and_summarize(http, durl, transcript)


def test_capture_export_behavior() -> None:
    email = _require("GS_LIVE_EMAIL")
    password = _require("GS_LIVE_PASSWORD")
    course_id = _require("GS_LIVE_COURSE_ID")
    assignment_id = _require("GS_LIVE_ASSIGNMENT_ID")
    base = os.environ.get("GS_LIVE_BASE_URL", "https://www.gradescope.com").rstrip("/")
    out_path = Path(os.environ.get("GS_LIVE_OUT", "gradescope-spike/transcript.md"))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    transcript: list[str] = [
        "# Gradescope export live-spike transcript",
        "",
        f"- base: {base}",
        f"- course/assignment: {course_id}/{assignment_id}",
        "",
    ]

    # follow_redirects=True satisfies GradescopeClient.login's precondition; the
    # export walk below overrides it per-request (follow_redirects=False) so we
    # can SEE each redirect hop instead of the client swallowing them.
    http = httpx.Client(follow_redirects=True, timeout=300.0)
    zip_temp: Path | None = None  # defined before try so the finally is clean
    try:
        gs = GradescopeClient(http, base_url=base)
        t0 = time.monotonic()
        gs.login(email, password)  # proven login flow; never logs the password
        transcript.append(f"## login: OK ({time.monotonic() - t0:.2f}s)")
        transcript.append("")

        # --- Manual redirect walk over the export endpoint -------------------
        url = f"{base}/courses/{course_id}/assignments/{assignment_id}/export/without_evaluations"
        transcript.append("## export redirect/response chain")
        final_resp_headers: dict[str, str] = {}

        for hop in range(_MAX_HOPS):
            hop_start = time.monotonic()
            with http.stream("GET", url, follow_redirects=False) as resp:
                elapsed = time.monotonic() - hop_start
                headers = _safe_headers(resp)
                transcript.append(
                    f"- hop {hop}: {resp.status_code} {url.replace(base, '')}"
                    f"  ({elapsed:.2f}s)  headers={headers}"
                )

                if resp.is_redirect and "location" in resp.headers:
                    url = urljoin(url, resp.headers["location"])
                    continue

                # Terminal response — classify it.
                final_resp_headers = headers
                ctype = resp.headers.get("content-type", "")
                disp = resp.headers.get("content-disposition", "")
                looks_zip = "zip" in ctype or "octet-stream" in ctype or ".zip" in disp.lower()

                if looks_zip:
                    fd, tmp_name = tempfile.mkstemp(prefix="gs-spike-", suffix=".zip")
                    zip_temp = Path(tmp_name)
                    total = 0
                    with os.fdopen(fd, "wb") as fh:
                        for chunk in resp.iter_bytes():
                            total += len(chunk)
                            fh.write(chunk)
                    transcript.append(f"  → body streamed: {total} bytes")
                else:
                    # HTML/text terminal (likely a "preparing"/generating page).
                    # Capture a bounded, structural snippet — enough to see poll/
                    # refresh hints, not a data dump.
                    body = resp.read().decode("utf-8", errors="replace")
                    snippet = _scrub(body[:_HTML_SNIPPET_CHARS].replace("\n", " "))
                    lowered = body.lower()
                    hints = [
                        w
                        for w in (
                            "preparing",
                            "generating",
                            "refresh",
                            "please wait",
                            "not ready",
                            "in progress",
                        )
                        if w in lowered
                    ]
                    transcript.append(f"  → non-zip terminal, content-type={ctype!r}")
                    transcript.append(f"  → keyword hints: {hints}")
                    transcript.append(f"  → snippet: {snippet!r}")
                break
        else:
            transcript.append(f"- stopped after {_MAX_HOPS} hops without a terminal response")

        # --- Structural summary of the ZIP (names/sizes only) ----------------
        if zip_temp is not None:
            transcript.append("")
            transcript.append("## export ZIP structure (central directory only)")
            try:
                transcript.extend(_summarize_zip(zip_temp))
            except zipfile.BadZipFile:
                transcript.append("- terminal body was NOT a valid zip (see snippet above)")

        transcript.append("")
        transcript.append(f"## final response headers: {final_resp_headers}")

        # --- Endpoint discovery ---------------------------------------------
        # The assumed export path may be wrong or POST-only. Fetch the staff-facing
        # management pages and surface every export/download URL they reference, so
        # we learn the REAL route + method instead of guessing. Also validates the
        # course/assignment ids exist (a 200 assignment page = ids are right).
        transcript.append("")
        transcript.append("## endpoint discovery (management pages)")
        create_path = ""
        discovery_html = ""
        for label, page_url in (
            ("assignment", f"{base}/courses/{course_id}/assignments/{assignment_id}"),
            ("submissions", f"{base}/courses/{course_id}/assignments/{assignment_id}/submissions"),
            (
                "review_grades",
                f"{base}/courses/{course_id}/assignments/{assignment_id}/review_grades",
            ),
        ):
            try:
                page = http.get(page_url, follow_redirects=True)
            except httpx.HTTPError as e:
                transcript.append(
                    f"- {label} ({page_url.replace(base, '')}): request error {type(e).__name__}"
                )
                continue
            is200 = page.status_code == 200
            candidates = _find_export_candidates(page.text) if is200 else []
            gon_paths = _extract_flow_gon(page.text) if is200 else {}
            genfile_urls = _find_generated_file_urls(page.text) if is200 else []
            transcript.append(
                f"- {label} ({page_url.replace(base, '')}): {page.status_code}, "
                f"final={str(page.url).replace(base, '')}, csrf_meta={_has_csrf_meta(page.text)}"
            )
            for c in candidates:
                transcript.append(f"    • candidate: {_scrub(c)}")
            for name, value in gon_paths.items():
                transcript.append(f"    • gon.{name} = {_scrub(value)!r}")
            for u in genfile_urls:
                transcript.append(f"    • generated_files url: {_scrub(u)}")
            if is200 and not candidates and not gon_paths:
                transcript.append(
                    "    • (no export/download URL found in page HTML — likely JS-constructed)"
                )
            # Full gon menu + bulk-export modal form fields once (review_grades page):
            # the gon menu helps spot the status/poll path var; the form fields reveal
            # the create-POST params (the 'without evaluations' question).
            if is200 and label == "review_grades":
                transcript.append(f"    • all gon.* names: {_all_gon_names(page.text)}")
                form_fields = _extract_export_form_fields(page.text)
                if form_fields:
                    transcript.append("    • bulk-export modal form fields:")
                    for f in form_fields:
                        transcript.append(f"        - {_scrub(f)}")
                else:
                    transcript.append(
                        "    • bulk-export modal: no server-side form fields found "
                        "(likely JS-rendered — create params not in HTML)"
                    )
            if not create_path and "create_bulk_export_path" in gon_paths:
                create_path = gon_paths["create_bulk_export_path"]
                discovery_html = page.text

        # --- Exercise the real flow (opt-in; MUTATES: creates an export job) ---
        if os.environ.get("GS_LIVE_EXERCISE") == "1":
            transcript.append("")
            transcript.append("## exercise bulk export (MUTATION — creates a real export job)")
            if create_path:
                _exercise_bulk_export(http, base, create_path, discovery_html, transcript)
            else:
                transcript.append("- no create_bulk_export_path discovered; cannot exercise")
    finally:
        # Never persist student source: delete the temp export unconditionally.
        if zip_temp is not None:
            zip_temp.unlink(missing_ok=True)
        http.close()

    out_path.write_text("\n".join(transcript) + "\n")
    print(f"\n[spike] transcript written to {out_path}\n")
    print("\n".join(transcript))

    # Minimal sanity so pytest reports meaningfully; the value is the transcript.
    assert any(line.startswith("## login: OK") for line in transcript)
    assert any(line.startswith("- hop 0:") for line in transcript)
