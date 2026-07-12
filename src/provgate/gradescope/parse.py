"""Pure HTML parsing for the Gradescope login + course pages (regex-based, no deps).

Selectors are provisional — verify against live HTML fixtures (see module note in
client.py) and adjust the patterns here if Gradescope's markup differs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CSRF_RE = re.compile(
    r'name="authenticity_token"\s+value="([^"]+)"'
    r'|value="([^"]+)"\s+name="authenticity_token"'
)
_ASSIGNMENT_RE = re.compile(r'href="/courses/\d+/assignments/(\d+)(?:/[a-z_]*)?"[^>]*>([^<]+)</a>')


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
    seen: dict[str, Assignment] = {}
    for aid, title in _ASSIGNMENT_RE.findall(html):
        if aid not in seen:
            seen[aid] = Assignment(id=aid, title=title.strip())
    return list(seen.values())
