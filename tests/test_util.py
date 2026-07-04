import logging

import pytest

from azdo_backup.util import chunks, get_logger, retry, safe_filename


def test_safe_filename_strips_invalid_chars():
    assert safe_filename('a<b>:c"/d\\e|f?g*h') == "a_b__c__d_e_f_g_h"


def test_safe_filename_handles_empty_and_dots():
    assert safe_filename("") == "_"
    assert safe_filename("name...") == "name"


def test_safe_filename_truncates():
    assert len(safe_filename("x" * 500)) == 120


def test_chunks():
    assert list(chunks(range(5), 2)) == [[0, 1], [2, 3], [4]]
    assert list(chunks([], 3)) == []


def test_retry_eventually_succeeds(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    calls = {"n": 0}

    @retry(tries=3, base_delay=0)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("boom")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_retry_raises_after_exhaustion(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)

    @retry(tries=2, base_delay=0)
    def always_fails():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        always_fails()


def test_get_logger_children_share_root_handler():
    root = get_logger()
    child = get_logger("azdo_backup.client")
    other = get_logger("restore")
    assert root.handlers, "root package logger must have a handler"
    assert child.name == "azdo_backup.client"
    assert other.name == "azdo_backup.restore"
    # Children rely on propagation to the configured root logger.
    assert child.propagate and other.propagate
    assert isinstance(child, logging.Logger)
