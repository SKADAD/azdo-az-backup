"""Azure DevOps REST client.

Uses Basic auth with a PAT (Personal Access Token). The PAT is taken from the
``--pat`` CLI flag or the ``AZURE_DEVOPS_EXT_PAT`` environment variable
(matching the ``az devops`` CLI convention).
"""
from __future__ import annotations

import base64
import os
from typing import Any, Iterable, Iterator
from urllib.parse import urljoin, urlparse

import requests

from .util import get_logger, retry

log = get_logger(__name__)

DEFAULT_API_VERSION = "7.1"


class AzDoError(RuntimeError):
    pass


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
                "No PAT supplied. Pass --pat or set AZURE_DEVOPS_EXT_PAT."
            )
        self.timeout = timeout
        self._basic_token = base64.b64encode(f":{self.pat}".encode()).decode()
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Basic {self._basic_token}",
            "Accept": "application/json",
        })

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

    @retry(tries=5, exceptions=(requests.RequestException,))
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
    ) -> requests.Response:
        url = self._full_url(path, project=project)
        merged_params = dict(params or {})
        if api_version and "api-version" not in {k.lower() for k in merged_params}:
            merged_params["api-version"] = api_version
        resp = self.session.request(
            method, url,
            params=merged_params, json=json, data=data,
            headers=headers, timeout=self.timeout, stream=stream,
        )
        if resp.status_code == 429:
            raise requests.RequestException(f"Rate limited: {resp.text[:200]}")
        if resp.status_code >= 500:
            raise requests.RequestException(
                f"Server error {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise AzDoError(
                f"{method} {url} -> {resp.status_code}: {resp.text[:500]}"
            )
        return resp

    def get_json(self, path: str, **kw) -> Any:
        return self.request("GET", path, **kw).json()

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
            for item in items:
                yield item
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
            for item in items:
                yield item
            token = resp.headers.get("x-ms-continuationtoken")
            if not token:
                return
            params["continuationToken"] = token

    def download(self, url: str, dest_path) -> None:
        with self.request("GET", url, stream=True, api_version=None) as resp:
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)

    # ------------------------------------------------------------------ helpers

    def list_projects(self) -> list[dict]:
        return list(self.iter_paged("_apis/projects", params={"stateFilter": "all"}))

    def get_project(self, name_or_id: str) -> dict:
        return self.get_json(
            f"_apis/projects/{name_or_id}",
            params={"includeCapabilities": "true"},
        )

    def git_auth_args(self) -> list[str]:
        """``git -c`` arguments that authenticate a single git invocation.

        Unlike embedding the PAT in the remote URL, this never persists the
        credential into the cloned repository's ``config`` file.
        """
        return ["-c", f"http.extraheader=AUTHORIZATION: Basic {self._basic_token}"]

    @staticmethod
    def strip_url_credentials(remote_url: str) -> str:
        """Remove any ``user:pass@`` userinfo from an HTTP(S) remote URL."""
        parsed = urlparse(remote_url)
        if parsed.scheme not in ("http", "https") or "@" not in parsed.netloc:
            return remote_url
        host = parsed.netloc.rsplit("@", 1)[-1]
        return parsed._replace(netloc=host).geturl()
