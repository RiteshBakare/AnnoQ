"""Command-line entry point. Pipeline wiring is added in T5.2; this is the T0.1 scaffold."""

from __future__ import annotations

import argparse


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
    build_parser().parse_args(argv)
    raise SystemExit("Pipeline not implemented yet (see docs/tasks.md M1+).")
