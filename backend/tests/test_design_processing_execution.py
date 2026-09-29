import multiprocessing
import os
import signal
from types import SimpleNamespace

import pytest

from backend.app.services import design_processing_execution as execution


def _send_when_released(connection, release):
    if not release.wait(15):
        raise RuntimeError("test parent never released child")
    connection.send((True, {"result": "ok"}))
    connection.close()


def _exit_without_result(connection, exit_code):
    os._exit(exit_code)


def _report_error(connection):
    connection.send((False, "ValueError: invalid extraction response"))
    connection.close()


def _send_large_result(connection):
    connection.send((True, b"x" * 1_000_000))
    connection.close()


@pytest.mark.parametrize("cancel_on_check", [None, 1, 2])
def test_result_arriving_during_eligibility_check_is_drained_and_still_guarded(monkeypatch, cancel_on_check):
    context = multiprocessing.get_context("spawn")
    release = context.Event()
    handles = {}
    checks = []

    def pipe(*args, **kwargs):
        receiver, sender = context.Pipe(*args, **kwargs)
        handles["receiver"] = receiver
        return receiver, sender

    def process(*args, **kwargs):
        child = context.Process(*args, **kwargs)
        handles["process"] = child
        return child

    def check_current():
        checks.append(True)
        if len(checks) == 1:
            # Force the production race: the initial poll saw no result, then
            # extraction finishes while the parent is checking eligibility.
            release.set()
            child = handles["process"]
            child.join(timeout=15)
            assert child.exitcode == 0
            assert handles["receiver"].poll(0)
        if len(checks) == cancel_on_check:
            raise RuntimeError("completed_folder")

    monkeypatch.setattr(execution.multiprocessing, "get_context", lambda method: SimpleNamespace(
        Pipe=pipe, Process=process,
    ))
    if cancel_on_check is None:
        result = execution._run_in_process(
            _send_when_released, (release,), check_current=check_current, poll_seconds=0.01,
        )
        assert result == {"result": "ok"}
        assert len(checks) == 2
    else:
        with pytest.raises(RuntimeError, match="completed_folder") as error:
            execution._run_in_process(
                _send_when_released, (release,), check_current=check_current, poll_seconds=0.01,
            )
        assert not isinstance(error.value, execution.DesignExtractionProcessError)
    assert not any(child.name == "design-extraction" for child in multiprocessing.active_children())


@pytest.mark.parametrize("exit_code", [0, 7])
def test_child_exit_without_message_reports_natural_exit_details(exit_code):
    with pytest.raises(execution.DesignExtractionProcessError, match="without a result") as error:
        execution._run_in_process(
            _exit_without_result, (exit_code,), check_current=lambda: None, poll_seconds=0.01,
        )
    failure = error.value
    assert failure.exit_code == exit_code
    assert failure.pid > 0
    assert failure.elapsed_seconds >= 0
    assert failure.signal_name is None
    assert f"exit_code={exit_code}" in str(failure)
    assert "elapsed_seconds=" in str(failure)
    assert not any(child.name == "design-extraction" for child in multiprocessing.active_children())


def test_child_reported_failure_keeps_error_and_process_details():
    with pytest.raises(execution.DesignExtractionProcessError, match="ValueError: invalid extraction response") as error:
        execution._run_in_process(_report_error, (), check_current=lambda: None, poll_seconds=0.01)
    assert error.value.exit_code == 0
    assert error.value.pid > 0


def test_large_result_can_be_received_before_child_exits():
    checks = []
    result = execution._run_in_process(
        _send_large_result, (), check_current=lambda: checks.append(True), poll_seconds=0.01,
    )
    assert result == b"x" * 1_000_000
    assert checks


def test_posix_signal_is_reported_without_assuming_oom(monkeypatch):
    monkeypatch.setattr(execution, "os", SimpleNamespace(name="posix"))
    error = execution.DesignExtractionProcessError(
        "missing result", pid=123, exit_code=-int(signal.SIGTERM), elapsed_seconds=1.5,
    )
    assert error.signal_name == "SIGTERM"
    assert "signal=SIGTERM" in str(error)
    assert "OOM" not in str(error)


def test_unfinished_child_exit_code_remains_unknown(monkeypatch):
    monkeypatch.setattr(execution, "os", SimpleNamespace(name="posix"))
    error = execution.DesignExtractionProcessError(
        "closed result channel", pid=123, exit_code=None, elapsed_seconds=1.5,
    )
    assert error.exit_code is None and error.signal_name is None


def test_process_diagnostics_survive_job_error_truncation():
    error = execution.DesignExtractionProcessError(
        "long upstream error " * 200, pid=123, exit_code=7, elapsed_seconds=1.5,
    )
    persisted_error = str(error)[:2000]
    assert "pid=123" in persisted_error and "exit_code=7" in persisted_error
    assert "elapsed_seconds=1.500" in persisted_error
