import pytest
import requests

from src.jira_client import CONTEXT_MAX_CHARS, JiraClient, JiraError

TOKEN = "tok-SECRET-123"


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, gets=(), posts=()):
        self.gets, self.posts, self.calls = list(gets), list(posts), []
        self.headers, self.auth = {}, None

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        r = self.gets.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        r = self.posts.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(session):
    return JiraClient("https://x.atlassian.net/", "a@b.c", TOKEN, session=session)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("src.jira_client.time.sleep", lambda s: None)


def test_issue_context_compact_and_truncated():
    s = FakeSession([FakeResp(200, {"fields": {"summary": " Checkout ", "description": "line1\n\n\nline2   x" * 300}})])
    ctx = client(s).get_issue_context("proj-12")
    assert ctx.startswith("Summary: Checkout\nDescription: line1\nline2 x")
    assert len(ctx) == CONTEXT_MAX_CHARS
    assert s.calls[0][1] == "https://x.atlassian.net/rest/api/2/issue/PROJ-12"
    assert s.auth == ("a@b.c", TOKEN)


def test_get_retried_once_on_5xx_and_network_error():
    s = FakeSession([FakeResp(503), FakeResp(200, {"fields": {"summary": "S"}})])
    assert client(s).get_issue_context("P-1") == "Summary: S"
    s = FakeSession([requests.ConnectionError(), requests.ConnectionError()])
    with pytest.raises(JiraError, match="Cannot reach Jira"):
        client(s).get_issue_context("P-1")
    assert len(s.calls) == 2


def test_has_marker_paginates():
    page1 = {"comments": [{"body": "x"}] * 100, "total": 101}
    page2 = {"comments": [{"body": "... ai-screen-questions:v2:abc ..."}], "total": 101}
    s = FakeSession([FakeResp(200, page1), FakeResp(200, page2)])
    assert client(s).has_marker("P-1", "ai-screen-questions:v2:abc") is True
    assert s.calls[1][2]["startAt"] == 100
    s = FakeSession([FakeResp(200, {"comments": [], "total": 0})])
    assert client(s).has_marker("P-1", "m") is False


def test_post_never_retried():
    s = FakeSession(posts=[FakeResp(503, {"errorMessages": ["busy"]})])
    with pytest.raises(JiraError, match="busy") as e:
        client(s).post_comment("P-1", "body")
    assert e.value.status == 503 and len(s.calls) == 1
    s = FakeSession(posts=[requests.Timeout()])
    with pytest.raises(JiraError, match="not retried"):
        client(s).post_comment("P-1", "body")
    assert len(s.calls) == 1


def test_post_ok():
    s = FakeSession(posts=[FakeResp(201, {"id": "10001"})])
    assert client(s).post_comment("P-1", "hello") == {"id": "10001"}
    assert s.calls[0][2] == {"body": "hello"}


@pytest.mark.parametrize("status, msg", [(401, "authentication failed"), (403, "No permission"), (404, "not found")])
def test_error_mapping_never_leaks_token(status, msg):
    s = FakeSession([FakeResp(status)])
    with pytest.raises(JiraError, match=msg) as e:
        client(s).get_issue_context("P-1")
    assert TOKEN not in str(e.value)


def test_invalid_key_and_missing_config():
    with pytest.raises(JiraError, match="Invalid Jira issue key"):
        client(FakeSession()).get_issue_context("../../admin")
    with pytest.raises(JiraError, match="not configured"):
        JiraClient("", "a@b.c", TOKEN)
