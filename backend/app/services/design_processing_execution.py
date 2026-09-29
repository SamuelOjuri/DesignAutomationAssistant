"""Interruptible production extraction; external requests already sent may finish."""

import multiprocessing
import os
import signal
from time import monotonic
from typing import Callable

from .legacy_enquiry.analysis import analyze_downloaded_email_assets
from .legacy_enquiry.llm import LegacyGeminiClient


class DesignExtractionProcessError(RuntimeError):
    """Child-process diagnostics retained in the worker log and job last_error."""

    def __init__(
        self, message: str, *, pid: int | None, exit_code: int | None, elapsed_seconds: float,
    ):
        self.pid = pid
        self.exit_code = exit_code
        self.elapsed_seconds = elapsed_seconds
        self.signal_name = None
        if os.name == "posix" and exit_code is not None and exit_code < 0:
            try:
                self.signal_name = signal.Signals(-exit_code).name
            except ValueError:
                self.signal_name = f"signal_{-exit_code}"
        super().__init__(
            f"Design extraction process failure (pid={pid}, exit_code={exit_code}, "
            f"signal={self.signal_name}, elapsed_seconds={elapsed_seconds:.3f}): {message}"
        )


def _process_error(process, started_at, message):
    # EOF can arrive just before the child finishes shutting down. Give it a
    # bounded opportunity to exit so diagnostics reflect its natural exit,
    # rather than the terminate/kill calls in the parent's cleanup below.
    process.join(timeout=1)
    return DesignExtractionProcessError(
        message, pid=process.pid, exit_code=process.exitcode,
        elapsed_seconds=monotonic() - started_at,
    )


def _analyze_in_child(connection, downloaded_assets, client):
    try:
        result = analyze_downloaded_email_assets(downloaded_assets, client=client)
        connection.send((True, result))
    except Exception as exc:
        connection.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


def run_interruptible_analysis(
    downloaded_assets,
    *,
    client,
    check_current: Callable[[], object],
    poll_seconds: float = 5.0,
):
    check_current()
    # Injected clients support deterministic in-process tests and offline tools.
    # The production client runs with its entire attachment-thread tree in a
    # disposable process, allowing cancellation even inside a blocking SDK call.
    if type(client) is not LegacyGeminiClient:
        result = analyze_downloaded_email_assets(downloaded_assets, client=client)
        check_current()
        return result

    return _run_in_process(
        _analyze_in_child, (downloaded_assets, client),
        check_current=check_current, poll_seconds=poll_seconds,
    )


def _run_in_process(target, args, *, check_current, poll_seconds=5.0):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=target,
        args=(sender, *args),
        daemon=True,
        name="design-extraction",
    )
    started_at = monotonic()
    try:
        process.start()
        sender.close()
        while not receiver.poll(poll_seconds):
            check_current()
            if not process.is_alive():
                # The child may have sent its result and exited while the
                # eligibility check was running. Drain that result before
                # deciding that an exited child failed.
                if receiver.poll():
                    break
                raise _process_error(
                    process, started_at, "exited without a result",
                )
        try:
            succeeded, result = receiver.recv()
        except EOFError as exc:
            check_current()
            raise _process_error(
                process, started_at,
                "closed its result channel without a result",
            ) from exc
        check_current()
        if not succeeded:
            raise _process_error(process, started_at, f"reported {result}")
        return result
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            process.close()
