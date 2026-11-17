"""Minimal Jira Cloud REST v2 client: read ticket context, check idempotency marker, post ONE comment.

GETs are retried once on network errors / 5xx. POST is never retried (a retry could double-post).
The API token is only ever passed to requests' auth; it never appears in messages.
"""

from __future__ import annotations

import re
import time
from typing import Any

import requests

ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
CONTEXT_MAX_CHARS = 1500


class JiraError(Exception):
    """Jira call failed. `status` is the HTTP status (None for network errors). CLI maps to exit 3."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _check_key(key: str) -> str:
    key = (key or "").strip().upper()
    if not ISSUE_KEY_RE.match(key):
        raise JiraError(f"Invalid Jira issue key {key!r} (expected e.g. PROJ-123).")
    return key


def _error_for(resp: requests.Response, key: str) -> JiraError:
    code = resp.status_code
    if code == 401:
        return JiraError("Jira authentication failed (401): check JIRA_EMAIL and JIRA_API_TOKEN.", code)
    if code == 403:
        return JiraError(f"No permission for {key} (403): the token's user cannot read/comment on it.", code)
    if code == 404:
        # Jira Cloud answers 404 (not 401) when the token is wrong, so mention credentials too.
        return JiraError(f"Issue {key} not found, or no permission to see it (404). "
                         "Check the key, JIRA_EMAIL and JIRA_API_TOKEN.", code)
    detail = ""
    try:
        body = resp.json()
        detail = "; ".join(body.get("errorMessages", []) + [f"{k}: {v}" for k, v in body.get("errors", {}).items()])
    except ValueError:
        pass
    return JiraError(f"Jira HTTP {code} for {key}" + (f": {detail[:300]}" if detail else ""), code)


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str, timeout: float = 20,
                 session: requests.Session | None = None):
        if not (base_url and email and token):
            raise JiraError("Jira is not configured: set JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN.")
        self.base = base_url.rstrip("/") + "/rest/api/2"
        self.timeout = timeout
        self.s = session or requests.Session()
        self.s.auth = (email, token)
        self.s.headers.update({"Accept": "application/json"})

    def _get(self, path: str, key: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in (1, 2):
            try:
                resp = self.s.get(self.base + path, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == 2:
                    raise JiraError(f"Cannot reach Jira: {type(e).__name__}") from None
                time.sleep(1)
                continue
            if resp.status_code >= 500 and attempt == 1:
                time.sleep(1)
                continue
            if not resp.ok:
                raise _error_for(resp, key)
            return resp.json()
        raise JiraError("unreachable")  # pragma: no cover

    def get_issue_context(self, key: str) -> str:
        """Summary + description, whitespace-collapsed, <= 1500 chars."""
        key = _check_key(key)
        fields = self._get(f"/issue/{key}", key, {"fields": "summary,description"}).get("fields", {})
        summary = (fields.get("summary") or "").strip()
        desc = fields.get("description") or ""
        if not isinstance(desc, str):  # defensive: ADF object if a v3 shape ever comes back
            desc = ""
        text = f"Summary: {summary}" + (f"\nDescription: {desc}" if desc.strip() else "")
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
        return text if len(text) <= CONTEXT_MAX_CHARS else text[: CONTEXT_MAX_CHARS - 1] + "…"

    def has_marker(self, key: str, marker: str) -> bool:
        """True if any existing comment contains the idempotency marker (paginates all comments)."""
        key = _check_key(key)
        start = 0
        while True:
            page = self._get(f"/issue/{key}/comment", key, {"startAt": start, "maxResults": 100})
            comments = page.get("comments", [])
            if any(marker in (c.get("body") or "") for c in comments):
                return True
            start += len(comments)
            if not comments or start >= page.get("total", 0):
                return False

    def post_comment(self, key: str, body: str) -> dict:
        """POST exactly one comment. Never retried."""
        key = _check_key(key)
        try:
            resp = self.s.post(f"{self.base}/issue/{key}/comment", json={"body": body}, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise JiraError(f"Cannot reach Jira while posting (not retried; check the ticket before re-running): "
                            f"{type(e).__name__}") from None
        if not resp.ok:
            raise _error_for(resp, key)
        return resp.json()
