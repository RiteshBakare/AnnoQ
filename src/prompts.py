"""Jinja2 environments (StrictUndefined), the `jira` escape filter, and tolerant JSON extraction."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, Undefined

TEMPLATES_DIR = Path(__file__).parent / "templates"

_JIRA_SPECIAL = re.compile(r"([\\{}\[\]*_|~^])")


def jira_escape(value: Any) -> str:
    """Escape Jira wiki-markup characters (\\ { } [ ] * _ | ~ ^) in LLM-generated text."""
    if value is None:
        return ""
    return _JIRA_SPECIAL.sub(r"\\\1", str(value))


def make_env(root: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(root)),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )
    env.filters["jira"] = jira_escape
    env.filters["tojson_compact"] = tojson_compact
    return env


def tojson_compact(value: Any) -> str:
    if isinstance(value, Undefined):  # keep StrictUndefined semantics: a missing variable fails loudly
        value._fail_with_undefined_error()
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Prompts:
    """Three roots: agents/ (system instructions, rendered once at build time), inputs/ (per-run user messages,
    rendered in function nodes) and the templates root (jira_comment.j2)."""

    def __init__(self, root: Path = TEMPLATES_DIR):
        self.agents = make_env(root / "agents")
        self.inputs = make_env(root / "inputs")
        self.root = make_env(root)

    def render_agent(self, name: str, **ctx: Any) -> str:
        return self.agents.get_template(f"{name}.j2").render(**ctx).strip()

    def render_input(self, name: str, **ctx: Any) -> str:
        return self.inputs.get_template(f"{name}.j2").render(**ctx).strip()

    def render_template(self, name: str, **ctx: Any) -> str:
        return self.root.get_template(f"{name}.j2").render(**ctx).strip()


_default: Prompts | None = None


def _prompts() -> Prompts:
    global _default
    if _default is None:
        _default = Prompts()
    return _default


def render_agent(name: str, **ctx: Any) -> str:
    return _prompts().render_agent(name, **ctx)


def render_input(name: str, **ctx: Any) -> str:
    return _prompts().render_input(name, **ctx)


def render_template(name: str, **ctx: Any) -> str:
    return _prompts().render_template(name, **ctx)


_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json(value: Any) -> Any:
    """Agent output (dict/list, or str with optional code fences/prose) -> parsed JSON. Raises ValueError."""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        raise ValueError(f"expected str or dict, got {type(value).__name__}")
    text = value.strip()
    candidates = [m.group(1).strip() for m in _FENCE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for chunk in candidates:
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            pass
        # JSON wrapped in prose: decode from the first "{" / "[" only. Trying later positions could return an
        # inner object of truncated output, which would look valid but be wrong.
        for start in sorted(i for i in (chunk.find("{"), chunk.find("[")) if i >= 0):
            try:
                return decoder.raw_decode(chunk, start)[0]
            except json.JSONDecodeError:
                continue
    raise ValueError("no valid JSON object found in model output")
