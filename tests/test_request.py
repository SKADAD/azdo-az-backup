"""Tests for the idempotency-aware request loop in AzDoClient.request."""
import pytest
import requests

from azdo_backup.client import AzDoAuthError, AzDoClient, AzDoError


class FakeResponse:
    def __init__(self, status_code=200, headers=None, text="{}", url="https://dev.azure.com/x"):
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}
        self.text = text
        self.url = url

    def json(self):
        import json
        return json.loads(self.text)


def make_client(monkeypatch, responses):
    """Client whose session returns queued responses (or raises exceptions)."""
    c = AzDoClient("https://dev.azure.com/myorg", pat="x")
    calls = {"n": 0}

    def fake_request(method, url, **kw):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        item = responses[i]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(c.session, "request", fake_request)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return c, calls


def test_get_retries_5xx_then_succeeds(monkeypatch):
    c, calls = make_client(monkeypatch, [FakeResponse(500), FakeResponse(200)])
    resp = c.request("GET", "_apis/projects")
    assert resp.status_code == 200
    assert calls["n"] == 2


def test_post_does_not_retry_5xx(monkeypatch):
    c, calls = make_client(monkeypatch, [FakeResponse(502), FakeResponse(200)])
    with pytest.raises(AzDoError) as ei:
        c.request("POST", "_apis/wit/workitems/$Bug")
    assert ei.value.status_code == 502
    assert calls["n"] == 1


def test_post_marked_idempotent_retries(monkeypatch):
    c, calls = make_client(monkeypatch, [FakeResponse(500), FakeResponse(200)])
    resp = c.request("POST", "_apis/wit/wiql", idempotent=True)
    assert resp.status_code == 200
    assert calls["n"] == 2


def test_429_retried_for_all_methods(monkeypatch):
    c, calls = make_client(monkeypatch, [
        FakeResponse(429, headers={"Retry-After": "1",
                                   "Content-Type": "application/json"}),
        FakeResponse(200),
    ])
    resp = c.request("POST", "_apis/wit/workitems/$Bug")
    assert resp.status_code == 200
    assert calls["n"] == 2


def test_4xx_raises_with_status_code(monkeypatch):
    c, _ = make_client(monkeypatch, [FakeResponse(404, text="nope")])
    with pytest.raises(AzDoError) as ei:
        c.request("GET", "_apis/projects/DoesNotExist")
    assert ei.value.status_code == 404


def test_203_signin_page_raises_auth_error(monkeypatch):
    c, _ = make_client(monkeypatch, [
        FakeResponse(203, headers={"Content-Type": "text/html"},
                     text="<html>Sign In</html>"),
    ])
    with pytest.raises(AzDoAuthError):
        c.request("GET", "_apis/projects")


def test_html_redirect_to_signin_raises_auth_error(monkeypatch):
    c, _ = make_client(monkeypatch, [
        FakeResponse(200, headers={"Content-Type": "text/html; charset=utf-8"},
                     text="<html>", url="https://spsprod.example/_signin?realm=x"),
    ])
    with pytest.raises(AzDoAuthError):
        c.request("GET", "_apis/projects")


def test_network_error_not_retried_for_mutations(monkeypatch):
    c, calls = make_client(monkeypatch, [
        requests.ConnectionError("boom"), FakeResponse(200),
    ])
    with pytest.raises(AzDoError):
        c.request("PATCH", "_apis/wit/workitems/1")
    assert calls["n"] == 1


def test_network_error_retried_for_gets(monkeypatch):
    c, calls = make_client(monkeypatch, [
        requests.ConnectionError("boom"), FakeResponse(200),
    ])
    resp = c.request("GET", "_apis/projects")
    assert resp.status_code == 200
    assert calls["n"] == 2


def test_get_json_non_json_raises_auth_error(monkeypatch):
    c, _ = make_client(monkeypatch, [
        FakeResponse(200, headers={"Content-Type": "application/octet-stream"},
                     text="<binary>"),
    ])
    with pytest.raises(AzDoAuthError):
        c.get_json("_apis/projects")
