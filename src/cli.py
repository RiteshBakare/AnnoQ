"""Command-line entry point. Full pipeline wiring arrives in T5.2; for now: config + slot + input validation."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, load_settings
from .models import validate_slots

EXIT_OK, EXIT_CONFIG, EXIT_PIPELINE, EXIT_JIRA = 0, 1, 2, 3


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


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(vars(args))
        logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
        slots = validate_slots(settings)
        settings.require_run_inputs()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG) from None
    print("Config OK. Models: " + ", ".join(f"{k}={v}" for k, v in slots.items()))
    print("Pipeline not implemented yet (docs/tasks.md M2+).")
