import pytest
from jinja2 import UndefinedError

from src.prompts import Prompts, extract_json, jira_escape


def test_jira_escape():
    assert jira_escape("a*b_c [x] {y} |z| ~s~ ^t^ \\") == r"a\*b\_c \[x\] \{y\} \|z\| \~s\~ \^t\^ \\"
    assert jira_escape(None) == "" and jira_escape(3) == "3"
    assert jira_escape("Plain text, no markup.") == "Plain text, no markup."


@pytest.mark.parametrize("raw", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    '```\n{"a": 1}\n```',
    'Here you go:\n{"a": 1}\nThanks!',
    '  {"a": 1}  ',
])
def test_extract_json_variants(raw):
    assert extract_json(raw) == {"a": 1}


def test_extract_json_passthrough_and_errors():
    assert extract_json({"a": 1}) == {"a": 1}
    assert extract_json('[{"a": 1}]') == [{"a": 1}]
    for bad in ["no json here", '{"a": 1', '{"screens": [{"id": "S1"}', ""]:
        with pytest.raises(ValueError):
            extract_json(bad)
    with pytest.raises(ValueError):
        extract_json(42)


@pytest.fixture
def prompts(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "inputs").mkdir()
    # trim_blocks removes the newline right after a block tag, so templates use a blank line after a block.
    (tmp_path / "agents" / "base.j2").write_text("RULES\n{% block lens %}{% endblock %}\n\nEND")
    (tmp_path / "agents" / "ds.j2").write_text('{% extends "base.j2" %}{% block lens %}DATA {{ tag }}{% endblock %}')
    (tmp_path / "inputs" / "panel_input.j2").write_text("N={{ n }}{% if extra %} {{ extra }}{% endif %}")
    (tmp_path / "jira_comment.j2").write_text("h2. {{ title | jira }}")
    return Prompts(tmp_path)


def test_render_roots_and_extends(prompts):
    assert prompts.render_agent("ds", tag="DS") == "RULES\nDATA DS\nEND"
    assert prompts.render_input("panel_input", n=3, extra="") == "N=3"
    assert prompts.render_template("jira_comment", title="a*b") == r"h2. a\*b"


def test_strict_undefined_fails(prompts):
    with pytest.raises(UndefinedError):
        prompts.render_input("panel_input", n=3)  # `extra` missing
