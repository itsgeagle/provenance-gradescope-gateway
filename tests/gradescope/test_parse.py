import html as _html
import json

import pytest

from provgate.gradescope.parse import Assignment, parse_assignments, parse_csrf_token

LOGIN_HTML = """
<form action="/login" method="post">
  <input type="hidden" name="authenticity_token" value="TOK-123" />
  <input name="session[email]" />
</form>
"""


def _assignments_page(rows: list[dict]) -> str:
    props = _html.escape(json.dumps({"table_data": rows}), quote=True)
    return f'<div data-react-class="AssignmentsTable" data-react-props="{props}"></div>'


def test_parse_csrf_token() -> None:
    assert parse_csrf_token(LOGIN_HTML) == "TOK-123"


def test_parse_assignments_from_react_props() -> None:
    rows = [
        {
            "id": "assignment_872677",
            "title": "Homework 1",
            "url": "/courses/1/assignments/872677",
        },
        {
            "id": "assignment_872690",
            "title": "Homework 2",
            "url": "/courses/1/assignments/872690",
        },
    ]
    html = _assignments_page(rows)
    got = parse_assignments(html)
    assert Assignment(id="872677", title="Homework 1") in got
    assert Assignment(id="872690", title="Homework 2") in got
    assert len(got) == 2


def test_parse_assignments_no_component_raises() -> None:
    with pytest.raises(ValueError):
        parse_assignments("<html><body>no table here</body></html>")


def test_parse_assignments_empty_table_raises() -> None:
    with pytest.raises(ValueError):
        parse_assignments(_assignments_page([]))
