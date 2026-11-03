"""Device-side plumbing shared by ``brain-push`` and the hosted hooks.

No database, no embedder, no SQLite file: a client only needs the server
URL and a device token. Standard library HTTP keeps hook start-up cheap.

Configuration (environment first, then files under the user's home, never
the repo):

  BRAIN_URL               server base URL (default: http://localhost:8080)
  BRAIN_DEVICE_TOKEN      the device token itself
  BRAIN_DEVICE_TOKEN_FILE file holding the token (default:
                          ~/.config/brain/device-token)
  BRAIN_CLIENT_DIR        state, lock, and log directory (default:
                          ~/.config/brain)
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://localhost:8080"
_USER_AGENT = "brain-client/1"


class ClientError(RuntimeError):
    """A request the server refused. ``status`` is the HTTP status, or 0 when
    the server could not be reached at all."""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status == 429 or self.status >= 500


def client_dir() -> Path:
    raw = (os.environ.get("BRAIN_CLIENT_DIR") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".config" / "brain"


def base_url() -> str:
    return (os.environ.get("BRAIN_URL") or DEFAULT_URL).strip().rstrip("/")


def load_token() -> str:
    """The device token, or "" when none is configured."""
    token = (os.environ.get("BRAIN_DEVICE_TOKEN") or "").strip()
    if token:
        return token
    raw = (os.environ.get("BRAIN_DEVICE_TOKEN_FILE") or "").strip()
    path = Path(raw).expanduser() if raw else client_dir() / "device-token"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def post_json(path: str, payload: dict, *, token: str, timeout: float) -> dict:
    """One POST. Raises :class:`ClientError` on any failure."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url() + path,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        message = ""
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error") or ""
        except Exception:
            pass
        err = ClientError(exc.code, message or exc.reason or "request failed")
        err.retry_after = _retry_after(exc.headers.get("Retry-After") if exc.headers else None)
        raise err from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ClientError(0, f"server unreachable ({type(exc).__name__})") from None
    try:
        out = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ClientError(502, "response is not JSON") from None
    if not isinstance(out, dict):
        raise ClientError(502, "response is not an object")
    return out


def _retry_after(raw) -> float | None:
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def post_with_retry(
    path: str,
    payload: dict,
    *,
    token: str,
    timeout: float = 60.0,
    attempts: int = 4,
    max_wait: float = 60.0,
    sleep=time.sleep,
) -> dict:
    """POST with exponential backoff on network errors, 429, and 5xx.
    A 4xx other than 429 is final and raised at once."""
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            return post_json(path, payload, token=token, timeout=timeout)
        except ClientError as exc:
            if not exc.retryable or attempt == attempts:
                raise
            wait = getattr(exc, "retry_after", None)
            if wait is None:
                wait = delay + random.uniform(0, delay / 2)
            sleep(min(wait, max_wait))
            delay *= 2
    raise ClientError(0, "unreachable")  # pragma: no cover


def debug(name: str, msg: str) -> None:
    if os.environ.get("BRAIN_HOOK_DEBUG") == "1":
        import sys

        print(f"{name}: {msg}", file=sys.stderr)
