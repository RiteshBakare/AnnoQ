# CLAUDE.md — Flow-to-Questions Pipeline

Guidance for Claude Code when working in this repo. Read this first, then the docs it points to.

## What this project is

A Python script that takes a **folder of screen images (one flow = one Jira ticket)**, uses a multi-agent **Google ADK 2.0 graph workflow** to generate a small, deduplicated set of business-logic questions, and posts them as **one comment** on the existing Jira ticket (addressed to `@product team` / `@tech team` / `@design team`). It replaces the manual "annotator interrogates Product/Tech/UX on a call" step that precedes the Figma → React pipeline.

## Source of truth (read before coding)

- `docs/architecture.md` — design, graph, budget, decisions
- `docs/spec.md` — schemas, prompts, graph code, rules, acceptance criteria
- `docs/tasks.md` — ordered task list; work top to bottom, tick boxes as you finish

If code and docs disagree, **stop and ask**; don't silently diverge. If you change a decision, update the doc in the same change.

## Hard rules (never break)

1. **LLM budget:** 8 planned calls per run (2 Gemini + 6 Groq), hard cap 9. Never add an LLM call without asking. Everything that doesn't need reasoning is plain Python (validation, dedupe, caps, numbering, rendering, posting).
2. **Model routing:** Gemini only for image work (`screen_analyst`, `auditor`); Groq only for text (5 specialists + `flow_curator`). Groq agents must never receive image parts.
3. **Models come from `.env`** (`VISION_MODEL`, `TEXT_MODEL`, optional per-agent overrides). Never hardcode a model name outside `.env.example`.
4. **All code lives in `src/`.** `run.py` at the repo root is only a 3-line shim.
5. **One Jira comment per run, on an existing ticket.** Never create tickets. POST is never auto-retried (avoids duplicates). Respect the idempotency marker unless `--force`.
6. **No real @mentions** — plain text `@product team`, `@tech team`, `@design team` only.
7. **No API catalog exists.** Prompts and questions must never invent endpoints or field names; they ask the PMO for endpoint, method and response field.
8. **Secrets** only in `.env` (gitignored). Never log or print API keys/tokens; never commit `.env`.
9. **Ask once:** a question about something repeated across screens is one `scope=FLOW` question with an "applies to" screen list. No duplicates across screens.

## Architecture in one glance

```
START → build_analyst_input → screen_analyst (Gemini, ALL images) → validate_inventory
      → build_panel_input → specialist_panel [DS, IA, VL, ST, BR in parallel → JoinNode]
      → merge_candidates → build_curator_input → flow_curator (Groq) → validate_curated
      → coverage_check (+ cross-screen dedupe) → build_audit_input → auditor (Gemini, ALL images)
      → apply_patch → finalize (caps, numbering, Jinja2 render) → publish (Jira / dry-run / artifacts)
```
Routers return `OK | RETRY | FALLBACK | ABORT`; one retry max (analyst first, then curator); curator failure falls back to deterministic code; auditor failure is skipped.

## Repo layout

```
run.py                     # shim → src.cli.main()
src/cli.py config.py models.py schemas.py run_context.py budget.py prompts.py
src/agents.py nodes.py workflow.py postprocess.py image_utils.py jira_client.py
src/templates/{agents,inputs}/*.j2  src/templates/jira_comment.j2
tests/                     # offline unit tests + graph tests with stub agents
outputs/<KEY>_<ts>/        # run artifacts (gitignored)
docs/                      # architecture.md spec.md tasks.md
```

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env                      # then fill keys / models / Jira
python run.py --file-path flows/shopping --jira-id PROJ-123 --dry-run   # safe default while developing
python run.py                             # uses FILE_PATH and JIRA_ISSUE_KEY from .env
python run.py --file-path flows/shopping --jira-id PROJ-123 --force     # post even if marker exists
pytest -q                                 # offline tests, no network, no API keys
```
Always use `--dry-run` until the output looks right; only post to a **sandbox ticket** during development.

## ADK 2.0 conventions

- Graph orchestration with `from google.adk import Agent, Event, Workflow` and `from google.adk.workflow import JoinNode, START`. Requires `google-adk>=2.0`.
- **Function nodes:** first parameter `node_input` is the previous node's output; other parameters named like state keys are bound from session state; return/yield `Event(output=…, state={…}, route="OK")`.
- **Agent nodes** run `mode="single_turn"`; their user message is the previous node's output. Render dynamic user messages in `build_*_input` function nodes from `templates/inputs/*.j2`; render system instructions once at build time from `templates/agents/*.j2`.
- `specialist_panel` is a nested `Workflow` (5 agents → `JoinNode`), `max_concurrency=PANEL_CONCURRENCY`.
- **Images never go in session state.** They live in `RunContext` and are attached to Gemini requests in `before_model_callback`. State holds JSON-serializable text/dicts only.
- Budget guard, usage logging and error stubs are model callbacks (`before_model_callback`, `after_model_callback`, `on_model_error_callback`), so every call (including retries) is counted.
- Agent outputs may arrive as `str` or `dict`; validators accept both via `extract_json()` + pydantic.
- The spec's assumptions A1–A8 (spec §9.1) are **unverified until the M0 spike passes**. Run the spike tasks first and record outcomes; if a fallback is needed, note it in `docs/spec.md`.

## Prompts and Jinja2

- Prompts are `.j2` files, not Python strings. Use `StrictUndefined`; a missing variable must fail tests.
- Specialists share `specialist_base.j2` and `{% extends %}` it with their lens block. Keep lens boundaries non-overlapping (DS data, IA actions/transitions, VL input rules, ST states/conditions, BR business rules).
- LLM-generated text going into the Jira comment must pass the `jira` escape filter.
- Every agent returns **JSON only**. When changing a prompt, re-run the golden-set dry runs and compare question quality, not just that it parses.

## Coding conventions

- Python 3.11+, type hints, pydantic v2 models in `schemas.py`; keep code simple — target ≲ 1000 LOC in `src/`, no extra frameworks beyond ADK, LiteLLM, Jinja2, pydantic, requests, Pillow, python-dotenv.
- Deterministic logic (`postprocess.py`) is pure functions with no I/O so it is unit-testable.
- Fail soft inside the pipeline (drop invalid candidates with a warning), fail loud at the edges (config/input errors exit 1, pipeline failure exit 2, Jira failure exit 3 with the comment still saved to `outputs/`).
- Natural-sort filenames define flow order; screen ids are `S1…Sn`; element ids are `S<k>.E<m>`; recurring components are `RC<n>`.
- Don't add features outside the current task. Phase 2 items (reading answers back, real mentions, API-catalog lookup) are in the backlog in `tasks.md` — don't start them.

## Testing

- Unit tests run offline: folder scan, `extract_json`, validators, `dedupe_across_screens`, `coverage`, `fallback_curate`, `apply_patch`, caps/numbering/size guard, `jira` filter, template snapshot renders, `LLMBudget` (10th call refused, concurrent safe).
- Graph tests use **stub agents** (no network): analyst invalid → retry → ok / abort; 2 specialists fail → proceed; 3 fail → abort; curator invalid → retry → fallback; auditor invalid → skipped; total calls never exceed 9.
- Manual integration needs real keys and the golden flows (shopping, login/OTP, onboarding, single screen, repeated-component flow). `run_report.json` must show `llm_calls` = 8 on a normal run with 2 Gemini + 6 Groq.

## Working agreement

1. Follow `docs/tasks.md` in order; start with **M0 spikes** (ADK graph behaviours) before building on them.
2. Before large changes, state the plan briefly and confirm if it touches the budget, model routing, graph topology, or Jira behaviour.
3. After each task: run `pytest -q`, run a `--dry-run` if the task touches the pipeline, then tick the task in `docs/tasks.md`.
4. Keep commits small and scoped to one task. Don't commit `.env`, `outputs/`, or screenshots with confidential designs.
5. If something in the docs is wrong or ambiguous, ask — especially about: `@design team` vs folding UI/UX into `@product team`, max screens per flow, and any need to exceed the LLM-call cap.
