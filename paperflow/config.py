"""加载本机 `.env`，不覆盖调用者已显式设置的环境变量。"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path | None = None) -> Path | None:
    candidates = [Path(path)] if path else [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    env_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if env_path is None:
        return None
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
    return env_path
