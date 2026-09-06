"""Runtime paths shared by CLI, Web and authorization helpers."""

from __future__ import annotations

import os
from pathlib import Path


def data_root() -> Path:
    """Return the configured persistent data root.

    Local CLI behavior remains unchanged when ``PAPERFLOW_DATA_ROOT`` is not
    set: paths are resolved relative to the current working directory.  The
    Web deployment sets it to a local directory such as ``/var/lib/paperflow``.
    """
    configured = os.getenv("PAPERFLOW_DATA_ROOT", "").strip()
    return Path(configured).expanduser().resolve() if configured else Path.cwd().resolve()


def data_path(*parts: str) -> Path:
    return data_root().joinpath(*parts)


def ensure_data_directories() -> Path:
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    for relative in ("downloads", "exports", "imports", "job-logs", "sessions"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root
