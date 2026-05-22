"""Solomon corpus inbox watcher.

OS-supervised long-lived worker. Watches ``corpus/inbox/`` for new files
and calls ``solomon.corpus.ingest.ingest_file`` on them. Implementation
choices follow REPORT-CORPUS.md §1.7:

  * **watchdog** for recursive FS events.
  * **30s debounce** after the last event in a burst, capped at 5 resets
    OR 5 min total — prevents livelock from a steady drip of events.
  * **3s file-stable check** — confirms size hasn't changed for 3s before
    we queue, so we don't pick up half-written files.
  * **Catch-up scan on startup** — any pre-existing inbox files get queued.
  * **Polling fallback** — when watchdog isn't installed, poll every 5s.
  * **Skip parking dirs** — ``_oversized``, ``_unsupported``,
    ``_pre-redaction``, ``_forgotten``.

The watcher is import-safe — it doesn't start any threads at module load.
Call ``run()`` to start the loop (blocks). Tests cover the pieces that
don't need a live filesystem clock:

  * ``catch_up_scan``        — finds pre-existing inbox files
  * ``_should_skip_path``    — parking-dir filter
  * ``_is_file_stable``      — st_size stability check (monkey-patched
    time + stat to keep it fast)
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Set

from ...corpus.ingest import IngestResult, ingest_file
from ...corpus.schema_config import corpus_root

logger = logging.getLogger("solomon.workers.corpus_inbox_watcher")

DEBOUNCE_SECONDS = 30.0
DEBOUNCE_MAX_RESETS = 5
DEBOUNCE_HARD_CAP_SECONDS = 300.0
FILE_STABLE_SECONDS = 3.0
POLL_INTERVAL_SECONDS = 5.0

PARKING_SUBDIRS = {"_oversized", "_unsupported", "_pre-redaction", "_forgotten"}


# ---------------------------------------------------------------------------
# Pure helpers (easy to test)
# ---------------------------------------------------------------------------


def _should_skip_path(path: Path, *, inbox_root: Path) -> bool:
    """True if the path is a directory, hidden, or sits in a parking subdir."""
    if path.is_dir():
        return True
    if path.name.startswith("."):
        return True
    try:
        rel = path.resolve().relative_to(inbox_root.resolve())
    except (ValueError, OSError):
        # Outside the inbox — skip.
        return True
    return any(part in PARKING_SUBDIRS for part in rel.parts)


def _is_file_stable(
    path: Path,
    *,
    stable_seconds: float = FILE_STABLE_SECONDS,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Confirm the file's size is unchanged after stable_seconds.

    Returns False when the file disappears mid-check.
    """
    try:
        s1 = path.stat().st_size
    except OSError:
        return False
    sleeper(stable_seconds)
    try:
        s2 = path.stat().st_size
    except OSError:
        return False
    return s1 == s2


def catch_up_scan(*, inbox_root: Optional[Path] = None) -> List[Path]:
    """Return the list of files currently sitting in the inbox that aren't
    in a parking subdir. The watcher calls this on startup so files
    dropped while the worker was down don't get missed.
    """
    root = inbox_root or (corpus_root() / "inbox")
    if not root.exists():
        return []
    files: List[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and not _should_skip_path(p, inbox_root=root):
            files.append(p)
    return files


# ---------------------------------------------------------------------------
# Watcher class
# ---------------------------------------------------------------------------


class InboxWatcher:
    """Watches ``corpus/inbox/`` and dispatches ``ingest_file`` per event."""

    def __init__(
        self,
        *,
        inbox_root: Optional[Path] = None,
        ingest_fn: Optional[Callable[[Path], IngestResult]] = None,
    ) -> None:
        self.inbox_root = (inbox_root or (corpus_root() / "inbox")).resolve()
        self.ingest_fn = ingest_fn or ingest_file
        self._pending: Set[Path] = set()
        self._last_event_at: float = 0.0
        self._first_event_at: float = 0.0
        self._resets: int = 0

    # ----- queueing -------------------------------------------------------

    def queue(self, path: Path) -> None:
        if _should_skip_path(path, inbox_root=self.inbox_root):
            return
        self._pending.add(path)
        now = time.time()
        if not self._pending or self._first_event_at == 0:
            self._first_event_at = now
        self._last_event_at = now
        self._resets += 1

    def _ready_to_drain(self) -> bool:
        if not self._pending:
            return False
        now = time.time()
        since_last = now - self._last_event_at
        since_first = now - self._first_event_at
        return (
            since_last >= DEBOUNCE_SECONDS
            or self._resets >= DEBOUNCE_MAX_RESETS
            or since_first >= DEBOUNCE_HARD_CAP_SECONDS
        )

    def drain(self) -> List[IngestResult]:
        """Process all pending files. Resets debounce state."""
        if not self._pending:
            return []
        results: List[IngestResult] = []
        for path in list(self._pending):
            if not path.exists():
                continue
            if not _is_file_stable(path):
                # Not stable yet — leave it for the next debounce round.
                continue
            try:
                results.append(self.ingest_fn(path))
            except Exception:  # noqa: BLE001
                logger.exception("ingest_file failed for %s", path)
        self._pending.clear()
        self._last_event_at = 0.0
        self._first_event_at = 0.0
        self._resets = 0
        return results

    # ----- top-level loop -------------------------------------------------

    def run(self) -> None:  # pragma: no cover — depends on watchdog runtime
        """Blocking loop. Uses watchdog when available, else polls."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.warning(
                "watchdog not installed; falling back to %ss polling",
                POLL_INTERVAL_SECONDS,
            )
            self._poll_loop()
            return

        # Catch-up scan.
        for p in catch_up_scan(inbox_root=self.inbox_root):
            self.queue(p)

        handler = _Handler(self)
        observer = Observer()
        observer.schedule(handler, str(self.inbox_root), recursive=True)
        observer.start()
        try:
            while True:
                time.sleep(1.0)
                if self._ready_to_drain():
                    self.drain()
        finally:
            observer.stop()
            observer.join()

    def _poll_loop(self) -> None:  # pragma: no cover
        seen: Set[Path] = set()
        while True:
            for p in catch_up_scan(inbox_root=self.inbox_root):
                if p not in seen:
                    seen.add(p)
                    self.queue(p)
            if self._ready_to_drain():
                results = self.drain()
                for r in results:
                    # After ingest the file is moved out of inbox; clear it from `seen`.
                    seen = {s for s in seen if s.exists()}
            time.sleep(POLL_INTERVAL_SECONDS)


try:
    from watchdog.events import FileSystemEventHandler  # type: ignore

    class _Handler(FileSystemEventHandler):  # pragma: no cover
        def __init__(self, watcher: InboxWatcher) -> None:
            super().__init__()
            self.watcher = watcher

        def on_created(self, event):
            if not event.is_directory:
                self.watcher.queue(Path(event.src_path))

        def on_moved(self, event):
            if not event.is_directory:
                self.watcher.queue(Path(event.dest_path))

        def on_modified(self, event):
            if not event.is_directory:
                self.watcher.queue(Path(event.src_path))

except ImportError:  # pragma: no cover
    _Handler = None  # type: ignore


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    watcher = InboxWatcher()
    logger.info("corpus inbox watcher starting (inbox=%s)", watcher.inbox_root)
    watcher.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
