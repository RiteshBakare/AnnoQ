"""T5.2 / T5.3: the CLI end to end, offline. Stub agents replace the LLMs (src.workflow.build_agents is patched) and a
fake Jira replaces the REST client. Checks exit codes 0/1/2/3, env/CLI precedence, ticket context and artifacts."""

import json
from pathlib import Path

import pytest

from src import cli
from src.jira_client import JiraError
from tests.conftest import png_bytes
from tests.test_postprocess import INV_DICT
from tests.test_workflow import FILES, Script, stub_agents

ENV = {"VISION_MODEL": "gemini-v", "TEXT_MODEL": "groq/t", "GOOGLE_API_KEY": "g-secret", "GROQ_API_KEY": "q-secret",
       "JIRA_BASE_URL": "https://x.atlassian.net", "JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "j-secret",
       "JIRA_ISSUE_KEY": "SCRUM-1", "DRY_RUN": "false"}


class FakeJira:
    instances: list = []

    def __init__(self, *args, marker=False, context_error=None, post_error=None):
        self.marker, self.context_error, self.post_error, self.posts = marker, context_error, post_error, []

    def get_issue_context(self, key):
        if self.context_error:
            raise self.context_error
        return "Summary: Checkout v2"

    def has_marker(self, key, marker):
        return self.marker

    def post_comment(self, key, body):
        if self.post_error:
            raise self.post_error
        self.posts.append((key, body))
        return {"id": "1"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated cwd (no real .env), a 4-screen flow, stub agents, fake Jira. Returns a configurable runner."""
    monkeypatch.chdir(tmp_path)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    for k in ("FILE_PATH", "MODEL_ANALYST", "MODEL_AUDITOR", "MODEL_SPECIALISTS", "MODEL_CURATOR", "MAX_LLM_CALLS",
              "LOG_LEVEL"):
        monkeypatch.delenv(k, raising=False)
    flow = tmp_path / "flows" / "shopping"
    flow.mkdir(parents=True)
    for i, name in enumerate(FILES):
        (flow / name).write_bytes(png_bytes(40 + i, 80))
    monkeypatch.setenv("FILE_PATH", str(flow))
    state = {"overrides": {}, "jira": FakeJira(), "rc": None}

    def fake_agents(rc):
        state["rc"] = rc
        return stub_agents(rc, state["overrides"])[0]

    monkeypatch.setattr("src.workflow.build_agents", fake_agents)
    monkeypatch.setattr(cli, "JiraClient", lambda *a, **k: state["jira"])

    def run(*argv, overrides=None, jira=None):
        state["overrides"] = overrides or {}
        if jira is not None:
            state["jira"] = jira
        return cli.run(list(argv))

    run.state = state
    return run


def latest_out(tmp_path: Path) -> Path:
    return max((tmp_path / "outputs").iterdir())


def test_dry_run_exit_0_prints_comment_and_writes_artifacts(env, tmp_path, capsys):
    assert env("--dry-run") == 0
    out = capsys.readouterr().out
    assert "4 screen(s) -> SCRUM-1 (dry-run)" in out and "LLM calls: 8 (Gemini 2, Groq 6)" in out
    assert "h2. Clarification questions" in out and "Questions: 6 (2 common)" in out
    assert "secret" not in out
    run_dir = latest_out(tmp_path)
    assert run_dir.name.startswith("SCRUM-1_")
    # T5.3: all six artifacts, with content
    report = json.loads((run_dir / "run_report.json").read_text())
    assert report["llm_calls"] == 8 and report["screens"] == 4 and report["posted"] is False
    assert report["skipped_reason"] == "dry-run" and len(report["calls"]) == 8
    assert sorted(c["model"] for c in report["calls"]) == ["gemini-stub"] * 2 + ["groq/stub"] * 6
    assert json.loads((run_dir / "flow_inventory.json").read_text())["screens"][0]["file"] == FILES[0]
    assert len(json.loads((run_dir / "candidates.json").read_text())) == 6
    assert len(json.loads((run_dir / "curated.json").read_text())) == 6
    assert json.loads((run_dir / "audit.json").read_text())["verdict"] == "ok"
    comment = (run_dir / "comment.txt").read_text(encoding="utf-8")
    assert "ai-screen-questions:v2:" in comment and comment in out
    assert not env.state["jira"].posts  # dry-run never posts
    assert env.state["rc"].ticket_context == "Summary: Checkout v2"  # read-only context still fetched


def test_post_exit_0_and_marker_skip_and_force(env, capsys):
    jira = FakeJira()
    assert env(jira=jira) == 0 and len(jira.posts) == 1 and jira.posts[0][0] == "SCRUM-1"
    marked = FakeJira(marker=True)
    env.state["rc"] = None
    assert env(jira=marked) == 0 and not marked.posts
    out = capsys.readouterr().out
    assert "Skipped: SCRUM-1 already has the questions" in out and "LLM calls: 0" in out
    assert env.state["rc"] is None  # pre-check: agents never built, no LLM call spent
    assert env("--force", jira=marked) == 0 and len(marked.posts) == 1


def test_jira_post_failure_exit_3_keeps_comment(env, tmp_path, capsys):
    jira = FakeJira(post_error=JiraError("Jira HTTP 500 for SCRUM-1", 500))
    assert env(jira=jira) == 3
    err = capsys.readouterr().err
    assert "Jira error: Jira HTTP 500" in err
    assert (latest_out(tmp_path) / "comment.txt").read_text(encoding="utf-8").startswith("h2.")


def test_ticket_context_failure_is_only_a_warning(env, capsys):
    jira = FakeJira(context_error=JiraError("Issue SCRUM-1 not found", 404))
    assert env("--dry-run", jira=jira) == 0
    assert "warning: Jira ticket context unavailable: Issue SCRUM-1 not found" in capsys.readouterr().out


def test_pipeline_failure_exit_2(env, capsys):
    bad = {**INV_DICT, "screens": INV_DICT["screens"][:2]}
    assert env("--dry-run", overrides={"screen_analyst": Script(bad)}) == 2
    assert "Pipeline failed: analyst output invalid" in capsys.readouterr().err


def test_unexpected_crash_exit_2(env, monkeypatch, capsys):
    async def boom(rc):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "run_workflow", boom)
    assert env("--dry-run") == 2 and "Pipeline error: RuntimeError: kaboom" in capsys.readouterr().err


@pytest.mark.parametrize("argv, setenv, msg", [
    (["--file-path", "nope"], {}, "Input path not found"),
    (["--jira-id", "not a key"], {}, "Invalid Jira issue key"),
    ([], {"TEXT_MODEL": "gemini-x"}, "must be a Groq model"),
    ([], {"GROQ_API_KEY": ""}, "GROQ_API_KEY"),
    ([], {"JIRA_API_TOKEN": ""}, "JIRA_API_TOKEN"),  # required when not dry-run
])
def test_config_errors_exit_1(env, monkeypatch, capsys, argv, setenv, msg):
    for k, v in setenv.items():
        monkeypatch.setenv(k, v)
    assert env(*argv) == 1
    err = capsys.readouterr().err
    assert msg in err and "secret" not in err


def test_cli_overrides_env_and_single_image(env, tmp_path, capsys):
    single = tmp_path / "login.png"
    single.write_bytes(png_bytes())
    one = {**INV_DICT, "screens": [{**INV_DICT["screens"][0], "file": "login.png"}], "recurring_components": [],
           "transitions": []}
    assert env("--file-path", str(single), "--jira-id", "proj-7", "--dry-run",
               overrides={"screen_analyst": Script(one)}) == 0
    out = capsys.readouterr().out
    assert "1 screen(s) -> PROJ-7 (dry-run)" in out and "Common to multiple screens" not in out
    assert latest_out(tmp_path).name.startswith("PROJ-7_")
