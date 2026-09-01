"""网络层：会话、代理 failover、限速器。"""

from __future__ import annotations

import os
import threading
import time

import requests

def configured_proxies() -> list[str | None]:
    """读取可选代理列表；不在源码中保存远程代理凭据。"""
    configured = [value.strip() for value in os.getenv("PAPERFLOW_PROXIES", "").split(",") if value.strip()]
    return [None, "http://127.0.0.1:7890", *configured]


DEFAULT_PROXIES = configured_proxies()
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def make_session(proxy: str | None = None, email: str = "") -> requests.Session:
    s = requests.Session()
    ua = USER_AGENT
    if email:
        ua = f"SpeciesLiteratureTool/1.0 (mailto:{email})"
    s.headers["User-Agent"] = ua
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


class RateLimiter:
    """令牌桶限速：控制每秒最大请求数（稳定优先，避免触发反爬）。"""

    def __init__(self, max_per_minute: int = 30) -> None:
        bounded_rate = min(max(max_per_minute, 1), 3600)
        self.interval = 60.0 / bounded_rate
        self.lock = threading.Lock()
        self.next_time = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.next_time:
                time.sleep(self.next_time - now)
            self.next_time = time.monotonic() + self.interval


def with_failover(fn, proxies: list[str | None] | None = None, retries: int = 2,
                  timeouts: list[float] | None = None):
    """按代理池执行 fn(session, ...)，失败自动切换代理并重试。"""
    proxies = proxies or DEFAULT_PROXIES
    timeouts = timeouts or [10.0, 15.0, 25.0]
    last = None
    for _ in range(retries + 1):
        for i, proxy in enumerate(proxies):
            try:
                return fn(proxy, timeouts[min(i, len(timeouts) - 1)])
            except Exception as exc:
                last = exc
                continue
        time.sleep(1.0)
    raise RuntimeError(f"所有代理均失败: {str(last)[:150]}")
