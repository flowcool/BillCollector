"""Cleanup failures must not leave later recovery steps unattempted."""

import pytest

from tests.check_container_compose import cleanup_lab


def test_all_cleanup_steps_run_after_failure():
    attempted = []
    failure = RuntimeError("synthetic stop failure")

    def stop():
        attempted.append("stop")
        raise failure

    def down():
        attempted.append("down")
        raise RuntimeError("synthetic down failure")

    with pytest.raises(RuntimeError) as caught:
        cleanup_lab([stop, lambda: attempted.append("wait"), down,
                     lambda: attempted.append("ownership")])
    assert caught.value is failure
    assert attempted == ["stop", "wait", "down", "ownership"]
    assert "synthetic down failure" in failure.__notes__[0]


def test_cleanup_keeps_original_test_failure():
    failure = AssertionError("synthetic collection failure")
    attempted = []

    def down():
        raise RuntimeError("synthetic cleanup failure")

    with pytest.raises(AssertionError) as caught:
        try:
            raise failure
        finally:
            cleanup_lab([down, lambda: attempted.append("ownership")])
    assert caught.value is failure
    assert attempted == ["ownership"]
    assert "synthetic cleanup failure" in failure.__notes__[0]


def test_successful_cleanup_does_not_fail():
    attempted = []
    cleanup_lab([lambda: attempted.append("ownership")])
    assert attempted == ["ownership"]
