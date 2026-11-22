#!/usr/bin/env python3
"""Minimal GitHub REST client (standard library only).

The transport never raises for HTTP status errors: callers inspect the status
code, so `allow_404` and "expected failure" handling stay explicit. Tests inject
their own transport instead of talking to github.com.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com"
UPLOADS_ROOT = "https://uploads.github.com"
USER_AGENT = "hermes-desktop-light-builder"


class GitHubError(RuntimeError):
    """An API call failed, or answered something the caller cannot trust."""


def urllib_transport(method: str, url: str, headers: dict, body):
    """Default transport: returns (status, headers, payload) without raising."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


class GitHub:
    def __init__(self, token: str | None = None, api: str = API_ROOT,
                 uploads: str = UPLOADS_ROOT, transport=None):
        self.token = token or None
        self.api = api.rstrip("/")
        self.uploads = uploads.rstrip("/")
        self.transport = transport or urllib_transport
        self.requests: list[tuple[str, str]] = []

    # -- plumbing -----------------------------------------------------------
    def _headers(self, content_type: str | None) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def call(self, method: str, url: str, body=None, content_type: str | None = None):
        self.requests.append((method, url))
        return self.transport(method, url, self._headers(content_type), body)

    @staticmethod
    def _fail(method: str, path: str, status: int, payload: bytes) -> GitHubError:
        detail = payload[:400].decode("utf-8", "replace")
        return GitHubError(f"{method} {path} -> HTTP {status}: {detail}")

    # -- JSON verbs ---------------------------------------------------------
    def get(self, path: str, params: dict | None = None, allow_404: bool = False):
        url = self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, _, payload = self.call("GET", url)
        if status == 404 and allow_404:
            return None
        if status >= 300:
            raise self._fail("GET", path, status, payload)
        return json.loads(payload.decode("utf-8"))

    def send(self, method: str, path: str, payload: dict | None = None,
             allow_404: bool = False):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, _, response = self.call(method, self.api + path, body, "application/json")
        if status == 404 and allow_404:
            return None
        if status >= 300:
            raise self._fail(method, path, status, response)
        return json.loads(response.decode("utf-8")) if response else {}

    def request_bytes(self, method: str, path: str, body: bytes, content_type: str,
                      params: dict | None = None, allow_404: bool = False,
                      absolute: bool = False):
        url = (self.uploads if absolute else self.api) + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, _, response = self.call(method, url, body, content_type)
        if status == 404 and allow_404:
            return None
        if status >= 300:
            raise self._fail(method, path, status, response)
        return json.loads(response.decode("utf-8")) if response else {}

    # -- convenience --------------------------------------------------------
    def paginate(self, path: str, params: dict | None = None, max_pages: int = 10):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        for page in range(1, max_pages + 1):
            params["page"] = page
            batch = self.get(path, params)
            if not batch:
                return
            for entry in batch:
                yield entry
            if len(batch) < params["per_page"]:
                return
