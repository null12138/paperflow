"""Small, shared helpers for safe PDF response persistence."""

from __future__ import annotations

import os
from pathlib import Path

import requests


def _maximum_pdf_bytes() -> int:
    try:
        megabytes = int(os.getenv("PAPERFLOW_MAX_PDF_MB", "256"))
    except ValueError:
        megabytes = 256
    return min(max(megabytes, 10), 2048) * 1024 * 1024


def save_streamed_pdf(response: requests.Response, target: Path) -> bool:
    """Validate and atomically save a streamed HTTP response as a PDF."""
    partial = target.with_suffix(target.suffix + ".part")
    partial.unlink(missing_ok=True)
    total = 0
    maximum = _maximum_pdf_bytes()
    started = False
    prefix = b""
    try:
        with partial.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue
                if not started:
                    prefix += chunk
                    if len(prefix) < 5:
                        continue
                    if not prefix.startswith(b"%PDF-"):
                        return False
                    started = True
                    chunk = prefix
                total += len(chunk)
                if total > maximum:
                    return False
                handle.write(chunk)
        if not started:
            return False
        partial.replace(target)
        return True
    finally:
        partial.unlink(missing_ok=True)
