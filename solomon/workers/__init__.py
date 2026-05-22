"""Solomon workers package — long-lived OS-supervised processes.

Each worker lives in a subpackage with an ``__init__.py`` exposing
``main()`` and an ``__main__.py`` so it can be invoked with
``python -m solomon.workers.<name>``.

Current workers:
  - corpus_inbox_watcher : watchdog-based corpus/inbox/ ingester
  - plaud_ingest         : IMAP IDLE listener for Plaud voice recordings (stub)
"""
