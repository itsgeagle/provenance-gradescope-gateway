import provgate


def test_version_is_exposed() -> None:
    assert provgate.__version__ == "0.1.0"
