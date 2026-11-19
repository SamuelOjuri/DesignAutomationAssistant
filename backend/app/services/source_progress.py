"""Thread-safe source progress without filenames or document contents."""
import threading

from .specification_semantics import ARCHIVE_EXPANDED_STATUS, DOCX_EXPANDED_STATUS


class SourceProgress:
    def __init__(self, callback=None):
        self.callback = callback
        self.states = {}
        self.lock = threading.Lock()

    def record(self, record):
        with self.lock:
            self.states[record["sourceId"]] = record.get("status", "pending")
            self._emit("reading_sources")

    def phase(self, phase):
        with self.lock:
            self._emit(phase)

    def _emit(self, phase):
        if self.callback is None:
            return
        statuses = [value for value in self.states.values()
                    if value not in {ARCHIVE_EXPANDED_STATUS, DOCX_EXPANDED_STATUS}]
        self.callback({"phase": phase, "source_counts": {
            "total": len(statuses), "read": statuses.count("read"),
            "skipped": sum(value.startswith("skipped:") for value in statuses),
            "review": sum(value.startswith("review:") for value in statuses),
            "failed": sum(value.startswith("failed:") for value in statuses),
        }})
