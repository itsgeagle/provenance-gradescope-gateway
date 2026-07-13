"""Pure HTML parsing for the Gradescope login + course pages (regex-based, no deps).

Selectors are provisional — verify against live HTML fixtures (see module note in
client.py) and adjust the patterns here if Gradescope's markup differs.
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass

_CSRF_RE = re.compile(
    r'name="authenticity_token"\s+value="([^"]+)"'
    r'|value="([^"]+)"\s+name="authenticity_token"'
)
_ASSIGNMENTS_TABLE_RE = re.compile(
    r'data-react-class="AssignmentsTable"\s+data-react-props="([^"]*)"'
)


@dataclass(frozen=True)
class Assignment:
    id: str
    title: str


def parse_csrf_token(html: str) -> str:
    m = _CSRF_RE.search(html)
    if not m:
        raise ValueError("no authenticity_token found in login page")
    return m.group(1) or m.group(2)


def parse_assignments(html: str) -> list[Assignment]:
    """Extract assignments from the instructor course page's React `AssignmentsTable`
    component props. Raises ValueError (never returns []) so a markup change surfaces
    loudly instead of silently syncing nothing."""
    m = _ASSIGNMENTS_TABLE_RE.search(html)
    if not m:
        raise ValueError("no AssignmentsTable component on course page")
    try:
        props = json.loads(_html.unescape(m.group(1)))
    except (ValueError, TypeError) as e:
        raise ValueError(f"could not parse AssignmentsTable props: {e}") from e
    rows = props.get("table_data") if isinstance(props, dict) else None
    if not isinstance(rows, list):
        raise ValueError("AssignmentsTable props missing table_data")
    seen: set[str] = set()
    out: list[Assignment] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        if not isinstance(rid, str):
            continue
        aid = rid[len("assignment_") :] if rid.startswith("assignment_") else rid
        title = row.get("title")
        if aid and aid not in seen:
            seen.add(aid)
            out.append(Assignment(id=aid, title=(title if isinstance(title, str) else "").strip()))
    if not out:
        raise ValueError("AssignmentsTable had no assignment rows")
    return out
