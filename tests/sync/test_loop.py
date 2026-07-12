from provgate.sync.loop import run_loop


def test_run_loop_runs_n_times() -> None:
    calls = {"n": 0}

    def once() -> None:
        calls["n"] += 1

    slept: list[float] = []
    iters = run_loop(once, 3600.0, sleep=slept.append, max_iters=3)
    assert iters == 3
    assert calls["n"] == 3
    assert slept == [3600.0, 3600.0, 3600.0]


def test_loop_continues_after_iteration_error() -> None:
    calls = {"n": 0}

    def once() -> None:
        calls["n"] += 1
        raise RuntimeError("one bad pass")

    iters = run_loop(once, 1.0, sleep=lambda _s: None, max_iters=2)
    assert iters == 2  # an error in a pass does not stop the loop
    assert calls["n"] == 2
