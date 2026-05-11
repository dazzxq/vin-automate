"""HTTPS client for the VPS backend at $API_BASE_URL.

Sole communication layer between Mac (crawl/extract) + Cowork (scoring/
brainstorming) and the VPS-side PHP API documented in PLAN-v2.md §4.

Design rules:
- Bearer token loaded once from `.env`.
- Retry on 429 (honor Retry-After header) and 5xx transient errors;
  configurable max attempts. 4xx other than 429 -> raise immediately.
- 30s default per-request timeout (overridable on the long-running
  /api/notify/ endpoint which can take up to Phase-2 budget = 241s).
- All requests + responses (status only) are logged to
  logs/api-client.log via a rotating handler.

This module deliberately keeps NO local state — there is no SQLite,
no cache. The VPS is the single source of truth.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


# --- module init ---------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_LOGS_DIR = _PROJECT_ROOT / "logs"
_LOG_PATH = _LOGS_DIR / "api-client.log"

load_dotenv(_ENV_PATH, override=False)

_log = logging.getLogger("api_client")
if not _log.handlers:
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _handler = logging.handlers.RotatingFileHandler(
        _LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)


class ApiError(Exception):
    """Raised on permanent non-2xx responses (after retries exhausted)."""

    def __init__(self, status: int, body: Any, method: str, path: str) -> None:
        super().__init__(f"{method} {path} -> {status}: {body!r}")
        self.status = status
        self.body = body
        self.method = method
        self.path = path


class ApiClient:
    """HTTPS client with bearer auth + retry/backoff."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.base_url = (base_url or os.getenv("API_BASE_URL", "")).rstrip("/")
        self.token    = token    or os.getenv("API_TOKEN", "")
        if not self.base_url:
            raise RuntimeError("API_BASE_URL not set in env")
        if not self.token:
            raise RuntimeError("API_TOKEN not set in env")
        self.timeout = timeout
        self.max_retries = max_retries

    # ------------------------------------------------------------------
    # Core HTTP
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        timeout: float | None = None,
        require_auth: bool = True,
        retryable: bool = True,
    ) -> dict:
        url = self.base_url + path
        headers = {"Accept": "application/json"}
        if require_auth:
            headers["Authorization"] = f"Bearer {self.token}"
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        last_exc: Exception | None = None
        backoff = [1, 2, 4]
        # Non-retryable endpoints (e.g. /api/articles/{id}/fail, where each
        # successful call bumps retry_count) go through the request loop only
        # once. The caller must handle ambiguous transport failures.
        max_attempts = self.max_retries if retryable else 1
        for attempt in range(1, max_attempts + 1):
            try:
                with httpx.Client() as client:
                    resp = client.request(
                        method,
                        url,
                        json=json_body,
                        params=params,
                        headers=headers,
                        timeout=timeout if timeout is not None else self.timeout,
                    )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                _log.warning("transport-fail %s %s (attempt %d/%d): %s",
                             method, path, attempt, max_attempts, e)
                last_exc = e
                if attempt < max_attempts:
                    time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
                    continue
                raise ApiError(0, str(e), method, path) from e

            _log.info("%s %s -> %d", method, path, resp.status_code)
            if 200 <= resp.status_code < 300:
                if not resp.content:
                    return {}
                try:
                    return resp.json()
                except json.JSONDecodeError as e:
                    raise ApiError(resp.status_code, resp.text, method, path) from e

            # Retryable: 429 (honor Retry-After) + 5xx.
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt >= max_attempts:
                    raise ApiError(resp.status_code, _safe_body(resp), method, path)
                retry_after = _retry_after_seconds(resp) or backoff[min(attempt - 1, len(backoff) - 1)]
                _log.warning("retry %s %s in %ds (status=%d, attempt %d/%d)",
                             method, path, retry_after, resp.status_code, attempt, max_attempts)
                time.sleep(retry_after)
                continue

            # 4xx other than 429: permanent.
            raise ApiError(resp.status_code, _safe_body(resp), method, path)

        # Should be unreachable; raise to keep mypy happy.
        if last_exc is not None:
            raise ApiError(0, str(last_exc), method, path)
        raise ApiError(0, "exhausted retries", method, path)

    # ------------------------------------------------------------------
    # Endpoint wrappers
    # ------------------------------------------------------------------
    def health(self) -> dict:
        # Health is public; no auth header needed.
        return self._request("GET", "/api/health", require_auth=False, timeout=10.0)

    def post_article(self, *, url: str, canonical_url: str,
                     title: str | None = None, source: str | None = None,
                     published_at: str | None = None) -> dict:
        body = {"url": url, "canonical_url": canonical_url}
        if title is not None:        body["title"] = title
        if source is not None:       body["source"] = source
        if published_at is not None: body["published_at"] = published_at
        return self._request("POST", "/api/articles", json_body=body)

    def list_articles(self, **filters: Any) -> list[dict]:
        # Coerce bool flags to "1"/"0" for the query string.
        params = {k: ("1" if v is True else "0" if v is False else v) for k, v in filters.items() if v is not None}
        resp = self._request("GET", "/api/articles", params=params)
        return resp.get("rows", [])

    def get_article(self, article_id: int) -> dict:
        return self._request("GET", f"/api/articles/{article_id}")

    def patch_extract(self, article_id: int, *, content: str,
                      title: str | None = None, source: str | None = None) -> dict:
        body: dict[str, Any] = {"content": content}
        if title is not None:  body["title"]  = title
        if source is not None: body["source"] = source
        return self._request("PATCH", f"/api/articles/{article_id}/extract", json_body=body)

    def patch_score(self, article_id: int, *, score: int, reason: str) -> dict:
        return self._request("PATCH", f"/api/articles/{article_id}/score",
                             json_body={"score": score, "reason": reason})

    def patch_brainstorm(self, article_id: int, ideas: list[dict]) -> dict:
        return self._request("PATCH", f"/api/articles/{article_id}/brainstorm",
                             json_body={"ideas": ideas})

    def post_fail(self, article_id: int, *, stage: str, error_code: str,
                  message: str) -> dict:
        # Single-shot: each successful /fail call bumps retry_count, so
        # retrying on a transport hiccup or 5xx would double-count. If this
        # raises, the caller's next loop iteration / cron tick re-tries
        # cleanly because the server only saw at most one attempt.
        return self._request("POST", f"/api/articles/{article_id}/fail",
                             json_body={"stage": stage, "error_code": error_code, "message": message},
                             retryable=False)

    def post_notify(self, article_id: int) -> dict:
        # Phase 2 can take up to 241s worst-case (Telegram backoff + retries).
        # Bump timeout well above that. The nginx vhost extends
        # fastcgi_read_timeout to 300s for the same reason.
        return self._request("POST", f"/api/notify/{article_id}", timeout=320.0)

    def acquire_lock(self, name: str, ttl_seconds: int) -> dict:
        return self._request("POST", f"/api/lock/{name}/acquire",
                             json_body={"ttl_seconds": ttl_seconds})

    def heartbeat_lock(self, name: str, owner_id: str, ttl_seconds: int) -> dict:
        return self._request("POST", f"/api/lock/{name}/heartbeat",
                             json_body={"owner_id": owner_id, "ttl_seconds": ttl_seconds})

    def release_lock(self, name: str, owner_id: str) -> dict:
        return self._request("DELETE", f"/api/lock/{name}",
                             json_body={"owner_id": owner_id})

    def inject_test(self, token: str) -> dict:
        # Server enforces localhost-only at nginx level; this client method is
        # callable but the request will 403 if not from VPS loopback / SSH tunnel.
        return self._request("POST", "/api/admin/inject-test",
                             json_body={"token": token})


# --- helpers ------------------------------------------------------------

def _retry_after_seconds(resp: httpx.Response) -> int | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return None


def _safe_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except json.JSONDecodeError:
        return resp.text[:500]


if __name__ == "__main__":
    # Quick smoke test when run directly: print health.
    print(json.dumps(ApiClient().health(), indent=2))
