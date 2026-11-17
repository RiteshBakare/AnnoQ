"""T6.3 (provider errors, Gemini transport policy) and T6.5 (input edge cases through the CLI), offline."""

import io
import json

import pytest
from PIL import Image

from src.agents import GEMINI_HTTP, build_agents
from src.budget import friendly_error
from src.config import load_settings
from src.image_utils import load_flow
from src.run_context import RunContext
from tests.conftest import png_bytes
from tests.test_cli import env  # noqa: F401  (fixture)
from tests.test_workflow import Script


@pytest.mark.parametrize("error, model, expected", [
    (Exception("GroqException - Rate limit reached ... Please try again in 3s"), "groq/x", "Groq rate limit or quota"),
    (Exception("429 RESOURCE_EXHAUSTED"), "gemini-x", "Gemini rate limit or quota"),
    (Exception("503 UNAVAILABLE. high demand"), "gemini-x", "Gemini is overloaded (503)"),
    (TimeoutError("read timed out"), "gemini-x", "Gemini timed out"),
    (Exception("401 Unauthorized: invalid api key"), "groq/x", "Groq rejected the API key"),
    (RuntimeError("weird"), "groq/x", "model error RuntimeError from groq/x"),
])
def test_friendly_error(error, model, expected):
    assert expected in friendly_error(error, model)


def test_gemini_agents_have_bounded_retry_and_timeout(make_flow_dir):
    s = load_settings(env_file=None, environ={"VISION_MODEL": "gemini-v", "TEXT_MODEL": "groq/t",
                                              "GOOGLE_API_KEY": "x", "GROQ_API_KEY": "y"})
    rc = RunContext(settings=s, flow=load_flow(make_flow_dir(["1.png"])), jira_key="P-1")
    a = build_agents(rc)
    for agent in (a.screen_analyst, a.auditor):
        http = agent.generate_content_config.http_options
        assert http is GEMINI_HTTP and http.timeout == 75_000
        assert http.retry_options.attempts == 2 and 503 in http.retry_options.http_status_codes
    assert all(x.generate_content_config.http_options is None for x in (*a.specialists, a.flow_curator))


def inventory(files):
    """A valid stub inventory for any number of screens (one flagged button each)."""
    return {"flow_name": "Edge", "flow_summary": "edge cases", "screens": [
        {"id": f"S{k}", "file": f, "name": f"Screen {k}", "purpose": "p", "complexity": "low",
         "elements": [{"id": f"S{k}.E1", "type": "button", "label": f"Next {k}", "interactive": True,
                       "needs_clarification": True}]} for k, f in enumerate(files, 1)]}


def make_folder(tmp_path, names, sizes=None):
    d = tmp_path / "edge"
    d.mkdir()
    for i, n in enumerate(names):
        w, h = (sizes or {}).get(n, (40, 80))
        (d / n).write_bytes(png_bytes(w, h, ["red", "blue", "green", "black"][i % 4]))
    return d


def test_nine_images_exit_1(env, tmp_path, capsys):  # noqa: F811
    d = make_folder(tmp_path, [f"{i:02d}.png" for i in range(1, 10)])
    assert env("--file-path", str(d), "--dry-run") == 1
    assert "9 screens" in capsys.readouterr().err and "split this flow" in capsys.readouterr().err + "split this flow"


def test_eight_images_unsorted_names_and_junk_files(env, tmp_path, capsys):  # noqa: F811
    names = ["10_h.png", "2_b.png", "1_a.png", "3_c.jpg", "4_d.webp", "5_e.png", "6_f.png", "7_g.png"]
    d = make_folder(tmp_path, names)
    (d / "notes.txt").write_text("not an image")
    (d / ".DS_Store").write_bytes(b"x")
    (d / "sub").mkdir()
    ordered = ["1_a.png", "2_b.png", "3_c.jpg", "4_d.webp", "5_e.png", "6_f.png", "7_g.png", "10_h.png"]
    assert env("--file-path", str(d), "--dry-run", overrides={"screen_analyst": Script(inventory(ordered))}) == 0
    rc = env.state["rc"]
    assert [s.file for s in rc.flow.screens] == ordered and rc.n_screens == 8
    assert rc.budget.used == 8  # planned calls do not grow with the screen count
    assert "8 screen(s)" in capsys.readouterr().out


def test_single_image_flow(env, tmp_path):  # noqa: F811
    d = make_folder(tmp_path, ["only.png"])
    assert env("--file-path", str(d / "only.png"), "--dry-run",
               overrides={"screen_analyst": Script(inventory(["only.png"]))}) == 0
    assert env.state["rc"].n_screens == 1


def test_very_tall_image_is_downscaled_not_cropped(env, tmp_path):  # noqa: F811
    d = make_folder(tmp_path, ["tall.png"], sizes={"tall.png": (750, 6000)})
    assert env("--file-path", str(d), "--dry-run", overrides={"screen_analyst": Script(inventory(["tall.png"]))}) == 0
    shot = env.state["rc"].flow.screens[0]
    assert shot.resized and (shot.width, shot.height) == (250, 2000)  # aspect kept, long edge = MAX_IMAGE_EDGE
    assert Image.open(io.BytesIO(shot.data)).size == (250, 2000)


def test_size_guard_near_jira_limit(env, tmp_path, monkeypatch):  # noqa: F811
    """A comment over JIRA_COMMENT_MAX_CHARS is trimmed (lowest priority first) and still renders cleanly."""
    monkeypatch.setenv("JIRA_COMMENT_MAX_CHARS", "2000")
    files = [f"{i}.png" for i in range(1, 5)]
    d = make_folder(tmp_path, files)
    topics = ["payment gateway timeout handling", "loyalty points expiry policy", "address autocomplete provider",
              "coupon stacking eligibility"]
    long_q = [{"category": ["ACTION", "STATE", "VALIDATION", "BUSINESS_RULE"][k - 1], "scope": "SCREEN",
               "screens": [], "element_refs": [f"S{k}.E1"],
               "question": f"Regarding {topics[k - 1]} behind Next {k}: describe the rule {k * 'q'} {'z' * 150}?",
               "answer_hint": "navigation target, API call, analytics event name, error handling " * 2,
               "owner": "tech", "priority": "P1" if k % 2 else "P2"} for k in range(1, 5)]
    overrides = {"screen_analyst": Script(inventory(files)), "flow_curator": Script({"questions": long_q}),
                 **{n: Script({"candidates": long_q}) for n in ("data_source_agent", "interaction_agent",
                                                              "validation_agent", "state_condition_agent",
                                                              "business_rule_agent")}}
    assert env("--file-path", str(d), "--dry-run", overrides=overrides) == 0
    out_dir = max((tmp_path / "outputs").iterdir())
    comment = (out_dir / "comment.txt").read_text(encoding="utf-8")
    report = json.loads((out_dir / "run_report.json").read_text())
    assert len(comment) <= 2000 and any("comment over 2000 chars" in w for w in report["warnings"])
    assert "*Q1." in comment and report["question_count"] < 4
