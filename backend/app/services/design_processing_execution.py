"""Interruptible production extraction; external requests already sent may finish."""

import multiprocessing
import os
import signal
import threading
from dataclasses import replace
from time import monotonic
from typing import Callable, Optional

from .legacy_enquiry.analysis import analyze_downloaded_email_assets
from .legacy_enquiry.llm import LegacyGeminiClient
from .legacy_enquiry.source_documents import IncompleteSourceExtraction
from .specification_failures import SpecificationEvidenceError
from ..config import settings


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
    send_lock = threading.Lock()
    def progress(update):
        with send_lock:
            connection.send(("progress", update))
    try:
        result = analyze_downloaded_email_assets(downloaded_assets, client=client, on_progress=progress)
        connection.send((True, result))
    except Exception as exc:
        if isinstance(exc, IncompleteSourceExtraction):
            connection.send((False, {"sourceExtractionFailure": exc.manifest}))
        elif isinstance(exc, SpecificationEvidenceError):
            connection.send((False, {"specificationEvidenceFailure": exc.evidence_failure, "message": str(exc)}))
        else:
            connection.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


def run_interruptible_analysis(
    downloaded_assets,
    *,
    client,
    check_current: Callable[[], object],
    check_local: Optional[Callable[[], object]] = None,
    poll_seconds: float = 5.0,
    remote_validation_seconds: float = 60.0,
    source_scope: str | None = None,
    on_progress=None,
):
    check_current()
    # Injected clients support deterministic in-process tests and offline tools.
    # The production client runs with its entire attachment-thread tree in a
    # disposable process, allowing cancellation even inside a blocking SDK call.
    if type(client) is not LegacyGeminiClient:
        result = analyze_downloaded_email_assets(downloaded_assets, client=client,
                                                **({"on_progress": on_progress} if on_progress else {}))
        check_current()
        return result

    client = replace(client, transcription_cache_scope=source_scope or client.transcription_cache_scope,
                     pdf_native_text_enabled=settings.design_processing_native_pdf_text_enabled)
    return _run_in_process(
        _analyze_in_child, (downloaded_assets, client),
        check_current=check_current, check_local=check_local,
        poll_seconds=poll_seconds, remote_validation_seconds=remote_validation_seconds,
        on_progress=on_progress,
    )


def _run_in_process(
    target, args, *, check_current, check_local=None, poll_seconds=5.0,
    remote_validation_seconds=60.0,
    on_progress=None,
):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=target,
        args=(sender, *args),
        daemon=True,
        name="design-extraction",
    )
    started_at = monotonic()
    next_remote_validation = started_at + remote_validation_seconds
    last_progress_at = None
    last_progress_phase = None
    last_progress_failures = 0
    try:
        process.start()
        sender.close()
        while True:
            while not receiver.poll(poll_seconds):
                if check_local is None:
                    check_current()
                else:
                    check_local()
                    if monotonic() >= next_remote_validation:
                        check_current()
                        next_remote_validation = monotonic() + remote_validation_seconds
                if not process.is_alive():
                    if receiver.poll():
                        break
                    raise _process_error(process, started_at, "exited without a result")
            try:
                succeeded, result = receiver.recv()
            except EOFError as exc:
                check_current()
                raise _process_error(process, started_at, "closed its result channel without a result") from exc
            if succeeded != "progress":
                break
            if check_local is not None:
                check_local()
            if on_progress is not None:
                now = monotonic()
                phase = result.get("phase")
                failures = result.get("source_counts", {}).get("failed", 0)
                # Large ZIPs can emit hundreds of quick skip/read updates. UI
                # polls every five seconds; coalesce writes without delaying a
                # new phase or hiding a source failure.
                if (last_progress_at is None or now - last_progress_at >= 1
                        or phase != last_progress_phase or failures != last_progress_failures):
                    on_progress(result)
                    last_progress_at, last_progress_phase, last_progress_failures = now, phase, failures
            if monotonic() >= next_remote_validation:
                check_current()
                next_remote_validation = monotonic() + remote_validation_seconds
        check_current()
        if not succeeded:
            if isinstance(result, dict) and "sourceExtractionFailure" in result:
                raise IncompleteSourceExtraction(result["sourceExtractionFailure"])
            if isinstance(result, dict) and "specificationEvidenceFailure" in result:
                raise SpecificationEvidenceError(result["message"], result["specificationEvidenceFailure"])
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
