from azdo_backup.client import AzDoClient


class FakeResponse:
    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


def make_client():
    return AzDoClient("https://dev.azure.com/myorg", pat="x")


def test_iter_continuation_follows_token(monkeypatch):
    c = make_client()
    pages = [
        FakeResponse({"value": [{"id": 1}, {"id": 2}]},
                     {"x-ms-continuationtoken": "tok1"}),
        FakeResponse({"value": [{"id": 3}]}),
    ]
    seen_params = []

    def fake_request(method, path, **kw):
        seen_params.append(dict(kw.get("params") or {}))
        return pages[len(seen_params) - 1]

    monkeypatch.setattr(c, "request", fake_request)
    items = list(c.iter_continuation("_apis/testplan/plans", project="P"))
    assert [i["id"] for i in items] == [1, 2, 3]
    assert "continuationToken" not in seen_params[0]
    assert seen_params[1]["continuationToken"] == "tok1"


def test_iter_paged_stops_on_short_page(monkeypatch):
    c = make_client()
    calls = []

    def fake_get_json(path, **kw):
        calls.append(dict(kw.get("params") or {}))
        skip = kw["params"]["$skip"]
        if skip == 0:
            return {"value": [{"id": i} for i in range(3)]}
        return {"value": [{"id": 3}]}

    monkeypatch.setattr(c, "get_json", fake_get_json)
    items = list(c.iter_paged("_apis/things", params={"$top": 3}))
    assert [i["id"] for i in items] == [0, 1, 2, 3]
    assert len(calls) == 2
