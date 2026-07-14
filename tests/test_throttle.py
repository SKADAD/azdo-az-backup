"""Tests for adaptive rate-limit pacing (Azure DevOps global consumption
limit: X-RateLimit-* headers arrive BEFORE server-side delays start, and
Retry-After can arrive on an HTTP 200)."""
import pytest

from azdo_backup.client import AdaptiveThrottle, AzDoClient


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr("azdo_backup.client.time.monotonic", c.monotonic)
    monkeypatch.setattr("azdo_backup.client.time.sleep", c.sleep)
    return c


def test_no_headers_no_delay(clock):
    t = AdaptiveThrottle()
    t.update({})
    t.wait()
    t.wait()
    assert clock.sleeps == []


def test_low_remaining_paces_requests(clock):
    t = AdaptiveThrottle()
    t.update({"X-RateLimit-Limit": "200", "X-RateLimit-Remaining": "30"})
    t.wait()  # 30/200 = 15% -> 2s spacing, paid immediately
    assert clock.sleeps == [2.0]
    t.wait()
    assert clock.sleeps == [2.0, 2.0]


def test_critical_remaining_paces_hard(clock):
    t = AdaptiveThrottle()
    t.update({"X-RateLimit-Limit": "200", "X-RateLimit-Remaining": "5"})
    t.wait()
    assert clock.sleeps == [5.0]


def test_retry_after_on_200_gates_next_request(clock):
    t = AdaptiveThrottle()
    t.update({"Retry-After": "12"})
    t.wait()
    assert clock.sleeps == [12.0]


def test_server_delay_header_backs_off(clock):
    t = AdaptiveThrottle()
    t.update({"X-RateLimit-Delay": "4.5"})
    t.wait()
    assert clock.sleeps == [9.0]  # 2x observed delay


def test_pacing_recovers_when_headers_disappear(clock):
    t = AdaptiveThrottle()
    t.update({"X-RateLimit-Limit": "200", "X-RateLimit-Remaining": "10"})
    t.update({})  # healthy response again
    t.wait()
    t.wait()
    assert clock.sleeps == []


def test_max_rps_floor_always_applies(clock):
    t = AdaptiveThrottle(max_rps=2)
    t.update({})
    t.wait()
    t.wait()
    assert clock.sleeps == [0.5]


def test_client_paces_from_response_headers(clock, monkeypatch):
    """End-to-end through AzDoClient.request: a 200 with warning headers
    slows the following request."""
    c = AzDoClient("https://dev.azure.com/myorg", pat="x")

    class R:
        status_code = 200
        headers = {"Content-Type": "application/json",
                   "X-RateLimit-Limit": "200",
                   "X-RateLimit-Remaining": "20"}
        text = "{}"
        url = "https://dev.azure.com/myorg/x"

        def json(self):
            return {}

    monkeypatch.setattr(c.session, "request", lambda *a, **kw: R())
    c.request("GET", "_apis/projects")   # response arms the throttle
    c.request("GET", "_apis/projects")   # pays the 2s spacing (ratio 0.10)
    assert 2.0 in clock.sleeps
