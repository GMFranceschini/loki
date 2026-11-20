"""Shared HTTP client and a simple rate limiter."""

from __future__ import annotations

import logging
import threading
import time

import httpx

from .. import __version__

log = logging.getLogger(__name__)

# Generic client name. No personal detail goes out unless the user explicitly
# puts a contact email in config/interests.yaml.
DEFAULT_UA = f"Loki-LitRadar/{__version__} (local literature digest; httpx)"


def user_agent(contact_email: str = "") -> str:
    contact_email = (contact_email or "").strip()
    return f"{DEFAULT_UA[:-1]}; {contact_email})" if contact_email else DEFAULT_UA


class RateLimiter:
    """Blocks so that calls happen no faster than `per_second`."""

    def __init__(self, per_second: float):
        self.min_interval = 1.0 / max(per_second, 0.01)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            gap = self.min_interval - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


def build_client(contact_email: str = "", timeout: float = 45.0) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=15.0),
        headers={"User-Agent": user_agent(contact_email), "Accept-Encoding": "gzip"},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )


def get_with_retry(
    client: httpx.Client,
    url: str,
    params: dict | None = None,
    limiter: RateLimiter | None = None,
    attempts: int = 4,
    backoff: float = 2.0,
) -> httpx.Response | None:
    """GET with retries on timeouts, 429 and 5xx. Returns None if all fail."""
    for attempt in range(1, attempts + 1):
        if limiter:
            limiter.wait()
        try:
            resp = client.get(url, params=params)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            if attempt == attempts:
                log.error("GET %s failed after %d attempts: %s", url, attempts, exc)
                return None
            delay = backoff ** attempt
            log.warning("GET %s failed (%s); retrying in %.0fs", url, exc, delay)
            time.sleep(delay)
    return None
