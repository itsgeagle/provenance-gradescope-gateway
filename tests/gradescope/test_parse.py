from provgate.gradescope.parse import Assignment, parse_assignments, parse_csrf_token

LOGIN_HTML = """
<form action="/login" method="post">
  <input type="hidden" name="authenticity_token" value="TOK-123" />
  <input name="session[email]" />
</form>
"""

COURSE_HTML = """
<table>
  <tr><td><a href="/courses/180852/assignments/872677">Homework 1</a></td></tr>
  <tr><td><a href="/courses/180852/assignments/872690/submissions">Homework 2</a></td></tr>
</table>
"""


def test_parse_csrf_token() -> None:
    assert parse_csrf_token(LOGIN_HTML) == "TOK-123"


def test_parse_assignments() -> None:
    got = parse_assignments(COURSE_HTML)
    assert Assignment(id="872677", title="Homework 1") in got
    assert Assignment(id="872690", title="Homework 2") in got
    assert len(got) == 2
