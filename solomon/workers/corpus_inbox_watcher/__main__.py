"""Allow `python -m solomon.workers.corpus_inbox_watcher`."""
from . import main

if __name__ == "__main__":
    raise SystemExit(main())
