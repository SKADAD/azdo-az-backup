"""Azure DevOps REST client.

Uses Basic auth with a PAT (Personal Access Token). The PAT is taken from the
``--pat`` CLI flag or the ``AZURE_DEVOPS_EXT_PAT`` / ``AZDO_PAT`` environment
variables (matching the ``az devops`` CLI convention).
"""
from __future__ import annotations

import base64
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

import requests

from .util import get_logger

log = get_logger(__name__)

DEFAULT_API_VERSION = "7.1"

# Single-shot attachment uploads are capped by the service (~130 MB); use the
# chunked protocol above this.
CHUNKED_UPLOAD_THRESHOLD = 100 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024


class AzDoError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AzDoAuthError(AzDoError):
    """The service answered with a sign-in page instead of data."""


class AzDoClient:
    """Thin wrapper around the Azure DevOps REST API."""

    def __init__(self, org_url: str, pat: str | None = None, timeout: float = 60.0):
        if not org_url:
            raise ValueError("org_url is required")
        # Normalize: ensure trailing slash, accept either https://dev.azure.com/org or https://org.visualstudio.com
        if not org_url.endswith("/"):
            org_url = org_url + "/"
        self.org_url = org_url
        self.org_name = self._extract_org_name(org_url)
        self.pat = pat or os.environ.get("AZURE_DEVOPS_EXT_PAT") or os.environ.get("AZDO_PAT")
        if not self.pat:
            raise AzDoError(
                "No PAT supplied. Pass --pat or set AZURE_DEVOPS_EXT_PAT (or AZDO_PAT)."
            )
        self.timeout = timeout
        self._basic_token = base64.b64encode(f":{self.pat}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {self._basic_token}",
            "Accept": "application/json",
        }
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        """One Session per thread — requests.Session is not thread-safe,
        and backup work is fanned out across a thread pool."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._headers)
            self._local.session = session
        return session

    @staticmethod
    def _extract_org_name(org_url: str) -> str:
        parsed = urlparse(org_url)
        if parsed.netloc.endswith(".visualstudio.com"):
            return parsed.netloc.split(".")[0]
        path = parsed.path.strip("/")
        if path:
            return path.split("/")[0]
        return parsed.netloc

    # ------------------------------------------------------------------ low-level

    def _full_url(self, path_or_url: str, project: str | None = None) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        if project:
            base = urljoin(self.org_url, f"{project}/")
        else:
            base = self.org_url
        return urljoin(base, path_or_url.lstrip("/"))

    @staticmethod
    def _looks_like_signin(resp: requests.Response) -> bool:
        """An invalid/expired PAT yields a 203 or a redirect to an HTML sign-in
        page instead of a 401."""
        if resp.status_code == 203:
            return True
        ctype = resp.headers.get("Content-Type", "").lower()
        if "text/html" not in ctype:
            return False
        # Only flag HTML that landed on a sign-in host/path — a legitimately
        # backed-up .html attachment must not trip this.
        parsed = urlparse(resp.url.lower())
        return parsed.netloc.startswith("login.") or "_signin" in parsed.path

    def request(
        self,
        method: str,
        path: str,
        *,
        project: str | None = None,
        params: dict | None = None,
        json: Any = None,
        data: Any = None,
        headers: dict | None = None,
        api_version: str | None = DEFAULT_API_VERSION,
        stream: bool = False,
        idempotent: bool | None = None,
        tries: int = 5,
    ) -> requests.Response:
        """Issue a request with idempotency-aware retries.

        GET/HEAD/OPTIONS retry on network errors and 5xx. Mutating calls are
        NOT retried on those (the server may have applied the change and a
        replay would duplicate it); callers doing read-only POSTs (WIQL,
        workitemsbatch) pass ``idempotent=True``. A 429 is always retryable —
        the server rejected the request — honoring ``Retry-After``.
        """
        if idempotent is None:
            idempotent = method.upper() in ("GET", "HEAD", "OPTIONS")
        url = self._full_url(path, project=project)
        merged_params = dict(params or {})
        if api_version and "api-version" not in {k.lower() for k in merged_params}:
            merged_params["api-version"] = api_version

        attempt = 0
        while True:
            attempt += 1
            backoff = min(30.0, 2.0 ** (attempt - 1))
            if hasattr(data, "seek"):
                data.seek(0)  # replaying a consumed file object sends an empty body
            try:
                resp = self.session.request(
                    method, url,
                    params=merged_params, json=json, data=data,
                    headers=headers, timeout=self.timeout, stream=stream,
                )
            except requests.RequestException as exc:
                if idempotent and attempt < tries:
                    log.warning("%s %s network error (attempt %d/%d): %s — retrying in %.0fs",
                                method, url, attempt, tries, exc, backoff)
                    time.sleep(backoff)
                    continue
                raise AzDoError(f"network error for {method} {url}: {exc}") from exc

            if resp.status_code == 429:
                if attempt < tries:
                    retry_after = backoff
                    try:
                        retry_after = float(resp.headers.get("Retry-After", backoff))
                    except ValueError:
                        pass
                    delay = min(max(retry_after, backoff), 300.0)
                    log.warning("%s %s rate-limited (attempt %d/%d) — waiting %.0fs",
                                method, url, attempt, tries, delay)
                    time.sleep(delay)
                    continue
                raise AzDoError(f"{method} {url} -> 429 after {tries} attempts",
                                status_code=429)

            if resp.status_code >= 500:
                if idempotent and attempt < tries:
                    log.warning("%s %s -> %d (attempt %d/%d) — retrying in %.0fs",
                                method, url, resp.status_code, attempt, tries, backoff)
                    time.sleep(backoff)
                    continue
                raise AzDoError(
                    f"{method} {url} -> {resp.status_code}: {resp.text[:200]}",
                    status_code=resp.status_code,
                )

            if resp.status_code >= 400:
                raise AzDoError(
                    f"{method} {url} -> {resp.status_code}: {resp.text[:500]}",
                    status_code=resp.status_code,
                )

            if self._looks_like_signin(resp):
                raise AzDoAuthError(
                    "Authentication failed: the service returned a sign-in page. "
                    "The PAT is invalid, expired, or lacks access to this organization.",
                    status_code=resp.status_code,
                )
            return resp

    def get_json(self, path: str, **kw) -> Any:
        resp = self.request("GET", path, **kw)
        try:
            return resp.json()
        except ValueError as exc:
            raise AzDoAuthError(
                f"Non-JSON response from {resp.url} "
                "(an invalid PAT typically causes this)."
            ) from exc

    def post_json(self, path: str, body: Any, **kw) -> Any:
        resp = self.request("POST", path, json=body, **kw)
        if resp.content:
            return resp.json()
        return None

    def patch_json(self, path: str, body: Any, content_type: str = "application/json-patch+json", **kw) -> Any:
        headers = kw.pop("headers", None) or {}
        headers.setdefault("Content-Type", content_type)
        resp = self.request("PATCH", path, json=body, headers=headers, **kw)
        if resp.content:
            return resp.json()
        return None

    def iter_paged(self, path: str, *, project: str | None = None,
                   params: dict | None = None, value_key: str = "value",
                   api_version: str | None = DEFAULT_API_VERSION) -> Iterator[dict]:
        """Iterate a paginated list endpoint (uses $top/$skip)."""
        params = dict(params or {})
        top = int(params.get("$top", 200))
        params["$top"] = top
        skip = int(params.get("$skip", 0))
        while True:
            params["$skip"] = skip
            data = self.get_json(path, project=project, params=params, api_version=api_version)
            items = data.get(value_key, []) if isinstance(data, dict) else data
            if not items:
                return
            yield from items
            if len(items) < top:
                return
            skip += len(items)

    def iter_continuation(self, path: str, *, project: str | None = None,
                          params: dict | None = None, value_key: str = "value",
                          api_version: str | None = DEFAULT_API_VERSION) -> Iterator[dict]:
        """Iterate a list endpoint paginated via ``x-ms-continuationtoken``.

        Used by the Test Plans APIs, which do not support ``$top``/``$skip``.
        """
        params = dict(params or {})
        while True:
            resp = self.request("GET", path, project=project, params=params,
                                api_version=api_version)
            data = resp.json()
            items = data.get(value_key, []) if isinstance(data, dict) else data
            yield from items
            token = resp.headers.get("x-ms-continuationtoken")
            if not token:
                return
            params["continuationToken"] = token

    def download(self, url: str, dest_path) -> None:
        """Stream a binary URL to disk atomically (temp file + rename).

        The whole operation (including the body stream) is retried: a
        connection dropped mid-body would otherwise fail permanently even
        though the GET is idempotent.
        """
        dest = Path(dest_path)
        tmp = dest.with_name(dest.name + ".part")
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with self.request("GET", url, stream=True, api_version=None,
                                  headers={"Accept": "application/octet-stream"}) as resp:
                    with open(tmp, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=64 * 1024):
                            if chunk:
                                f.write(chunk)
                tmp.replace(dest)
                return
            except (requests.RequestException, OSError) as exc:
                last_exc = exc
                log.warning("download %s failed (attempt %d/3): %s", url, attempt + 1, exc)
                time.sleep(2.0 * (attempt + 1))
            finally:
                if tmp.exists():
                    tmp.unlink()
        raise AzDoError(f"download failed for {url}: {last_exc}")

    def upload_attachment(self, project: str, file_path, name: str) -> str:
        """Upload a work-item attachment, using the chunked protocol for
        large files (single-shot uploads are capped by the service).
        Returns the new attachment URL."""
        path = Path(file_path)
        size = path.stat().st_size
        if size <= CHUNKED_UPLOAD_THRESHOLD:
            with open(path, "rb") as f:
                resp = self.request(
                    "POST", "_apis/wit/attachments", project=project,
                    params={"fileName": name}, data=f,
                    headers={"Content-Type": "application/octet-stream"},
                )
            return resp.json()["url"]

        start = self.request(
            "POST", "_apis/wit/attachments", project=project,
            params={"fileName": name, "uploadType": "chunked"},
        ).json()
        url = start["url"]
        sent = 0
        with open(path, "rb") as f:
            while sent < size:
                chunk = f.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                end = sent + len(chunk) - 1
                self.request(
                    "PUT", url, data=chunk,
                    params={"uploadType": "chunked", "fileName": name},
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes {sent}-{end}/{size}",
                    },
                )
                sent += len(chunk)
        return url

    # ------------------------------------------------------------------ helpers

    def list_projects(self) -> list[dict]:
        return list(self.iter_paged("_apis/projects", params={"stateFilter": "all"}))

    def get_project(self, name_or_id: str) -> dict:
        return self.get_json(
            f"_apis/projects/{name_or_id}",
            params={"includeCapabilities": "true"},
        )

    def git_auth_env(self) -> dict[str, str]:
        """Environment variables that authenticate a git invocation.

        Env-based config keeps the credential out of both the repository's
        ``config`` file and the process table (``ps``).
        """
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: Basic {self._basic_token}",
        }

    @staticmethod
    def strip_url_credentials(remote_url: str) -> str:
        """Remove any ``user:pass@`` userinfo from an HTTP(S) remote URL."""
        parsed = urlparse(remote_url)
        if parsed.scheme not in ("http", "https") or "@" not in parsed.netloc:
            return remote_url
        host = parsed.netloc.rsplit("@", 1)[-1]
        return parsed._replace(netloc=host).geturl()
