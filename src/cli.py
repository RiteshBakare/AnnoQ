"""Command-line entry point (spec §4, §13): config -> flow images -> Jira context -> graph workflow -> exit code.

Exit codes: 0 success (posted, dry-run, or marker skip) · 1 config/input error · 2 pipeline failure · 3 Jira failure
(the comment is still saved under outputs/).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .config import ConfigError, load_settings
from .image_utils import load_flow
from .jira_client import ISSUE_KEY_RE, JiraClient, JiraError
from .models import validate_slots
from .run_context import RunContext
from .workflow import run_workflow

EXIT_OK, EXIT_CONFIG, EXIT_PIPELINE, EXIT_JIRA = 0, 1, 2, 3
log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Generate clarification questions for a flow of screens and post them to Jira.",
    )
    p.add_argument("--file-path", help="Folder of screens (one flow) or one image. Env: FILE_PATH")
    p.add_argument("--jira-id", help="Existing Jira issue key. Env: JIRA_ISSUE_KEY")
    p.add_argument("--dry-run", action="store_true", help="Do not post; print comment and save artifacts.")
    p.add_argument("--force", action="store_true", help="Post even if the idempotency marker exists.")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logs.")
    return p


def _setup_console(level: str) -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; the comment uses → · —
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # not a TextIOWrapper (e.g. captured by pytest)
            pass
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("src").setLevel(level)


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(vars(args))
        _setup_console(settings.log_level)
        slots = validate_slots(settings)
        settings.require_run_inputs()
        key = settings.jira_issue_key.strip().upper()
        if not ISSUE_KEY_RE.match(key):
            raise ConfigError(f"Invalid Jira issue key {key!r} (expected e.g. PROJ-123).")
        flow = load_flow(settings.file_path, settings.max_screens, settings.max_image_mb, settings.max_image_edge)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return EXIT_CONFIG

    jira, ticket_context, context_warning = None, "", None
    if settings.has_jira_credentials:  # read-only GET even in dry-run (FR-5); failure is only a warning
        jira = JiraClient(settings.jira_base_url, settings.jira_email, settings.jira_api_token.get_secret_value())
        try:
            ticket_context = jira.get_issue_context(key)
        except JiraError as e:
            context_warning = f"Jira ticket context unavailable: {e}"
    rc = RunContext(settings=settings, flow=flow, jira_key=key, ticket_context=ticket_context, jira=jira)
    if context_warning:
        rc.warn(context_warning)

    mode = "dry-run" if settings.dry_run else ("post, --force" if settings.force else "post")
    print(f"Flow '{flow.hint}': {rc.n_screens} screen(s) -> {key} ({mode}) · run {rc.run_id}")
    print("Models: " + ", ".join(f"{k}={v}" for k, v in slots.items()))
    try:
        state = asyncio.run(run_workflow(rc))
    except Exception as e:  # noqa: BLE001 - last-resort guard so the CLI always ends with a clear exit code
        log.debug("pipeline crashed", exc_info=True)
        print(f"Pipeline error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_PIPELINE

    result = state.get("result") or {"ok": False, "error": "workflow ended without a result"}
    _print_summary(rc, result)
    if not result.get("ok"):
        print(f"Pipeline failed: {result.get('error')}", file=sys.stderr)
        return EXIT_PIPELINE
    if settings.dry_run:
        print("\n" + state.get("comment", ""))
    if result.get("jira_error"):
        print(f"Jira error: {result['jira_error']} (comment saved to {result.get('comment_path')})", file=sys.stderr)
        return EXIT_JIRA
    return EXIT_OK


def _print_summary(rc: RunContext, result: dict) -> None:
    gemini = sum(c.model.startswith("gemini") for c in rc.calls)
    print(f"LLM calls: {rc.budget.used} (Gemini {gemini}, Groq {len(rc.calls) - gemini})")
    if result.get("report_path"):
        with open(result["report_path"], encoding="utf-8") as f:
            report = json.load(f)
        print(f"Questions: {report['question_count']} ({report['common_count']} common) · "
              f"posted: {report['posted']}" + (f" · skipped: {report['skipped_reason']}"
                                                 if report["skipped_reason"] else ""))
        print(f"Artifacts: {result.get('comment_path') or result['report_path']}")
    for w in rc.warnings:
        print(f"  warning: {w}")


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run(argv))
