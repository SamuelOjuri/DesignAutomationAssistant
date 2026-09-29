"""Interruptible production extraction; external requests already sent may finish."""

import multiprocessing
from typing import Callable

from .legacy_enquiry.analysis import analyze_downloaded_email_assets
from .legacy_enquiry.llm import LegacyGeminiClient


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
    try:
        process.start()
        sender.close()
        while not receiver.poll(poll_seconds):
            check_current()
            if not process.is_alive():
                raise RuntimeError("Design extraction process exited without a result")
        try:
            succeeded, result = receiver.recv()
        except EOFError as exc:
            raise RuntimeError("Design extraction process exited without a result") from exc
        check_current()
        if not succeeded:
            raise RuntimeError(result)
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
