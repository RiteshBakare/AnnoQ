# Tasks — Flow-to-Questions Pipeline (Phase 1, v2)

Legend: `[ ]` todo · **Dep** = depends on.
Refs: `architecture.md` (A), `spec.md` (S).

---

## M0 — Spike & scaffold (de-risk the ADK 2.0 graph first)

- [x] **T0.1 Scaffold repo** — folders per A§14, `requirements.txt` (`google-adk>=2.0`, litellm, jinja2, pydantic, python-dotenv, requests, pillow, pytest), `.env.example` (S§5), `.gitignore`, `run.py` shim.
  *Done when:* `python run.py --help` works; `python -c "from google.adk import Workflow"` works.
- [x] **T0.2 Spike: graph basics** — tiny `Workflow` with function nodes: `START → fn → fn`, `Event(output=…, state=…)`, running via `Runner` + `InMemorySessionService`, reading final state. Verify S§9.1 **A1, A8**.
- [x] **T0.3 Spike: fan-out/join + nested workflow** — `specialist_panel` as nested `Workflow` with 3 dummy agents + `JoinNode`, used as a node in the parent graph; check join output shape, `max_concurrency`. Verify **A2, A3, A7**.
- [x] **T0.4 Spike: routing + retry loop** — router returning `Event(route=…)`, dict edges, loop edge back to an earlier node (node with 2 inbound edges), retry counter in state. Verify **A4**.
  *Fallback decision:* if cycles/multi-inbound don't work, switch retries to a dynamic node (`@node` + `ctx.run_node`) — record the decision.
  *Result:* loops + multi-inbound work in google-adk 2.14.1; no fallback needed (S§9.2).
- [x] **T0.5 Spike: Groq agent node** — `Agent(model=LiteLlm("groq/…"), mode="single_turn")` receiving a rendered Jinja2 user message, JSON output; test with and without `response_format={"type":"json_object"}`; `on_model_error_callback` stub so a join still completes. Verify **A5, A6**.
- [x] **T0.6 Spike: Gemini multi-image agent node** — Gemini `Agent` node; attach 4 images via `before_model_callback`; confirm Groq nodes never receive images; budget callback returns stub on call #10 (thread-safe under parallel run). 
  *Done when:* all A1–A8 are confirmed or have a recorded fallback.

---

## M1 — Foundations (no LLM)

- [ ] **T1.1 `config.py`** — load `.env`, `Settings` (pydantic), CLI-over-env precedence, clamp `MAX_LLM_CALLS ≤ 9`, parse `OWNER_LABELS`, limits. **Dep:** T0.1
- [ ] **T1.2 `models.py`** — `build_model()`, `validate_slots()`; per-agent overrides. **Dep:** T1.1, T0.5–T0.6
  *Done when:* misconfigured slot exits 1 with clear message.
- [ ] **T1.3 `schemas.py`** — pydantic models from S§6 (FlowInventory, RecurringComponent, Candidate with scope/screens, AuditPatch with rescope, RunReport). **Dep:** T0.1
- [ ] **T1.4 `image_utils.py`** — folder scan (natural sort, hidden/non-image ignored, `MAX_SCREENS`), single-file mode, per-image validation/resize, per-image SHA + flow hash, `types.Part` list in order, screen ids S1…Sn with filename-derived hints. **Dep:** T1.1
- [ ] **T1.5 `prompts.py`** — Jinja2 env (`StrictUndefined`) with two roots (`agents/`, `inputs/`), `render_agent()`, `render_input()`, `jira` filter, `extract_json()`. **Dep:** T1.3
- [ ] **T1.6 `budget.py` + `run_context.py`** — `LLMBudget` (locked), `RunContext` (settings, image parts, budget, call log), before/after/error callbacks. **Dep:** T0.6
- [ ] **T1.7 `jira_client.py`** — `get_issue_context`, `has_marker`, `post_comment`, error mapping (S§12.3). **Dep:** T1.1
  *Done when:* verified against a sandbox ticket.
- [ ] **T1.8 Unit tests for M1** — folder scan cases, escape filter, `extract_json`, budget (10th call refused, concurrent), slot validation, config precedence.

---

## M2 — Prompts & agents

- [ ] **T2.1 Agent templates** — `agents/analyst.j2`, `specialist_base.j2` + `ds/ia/vl/st/br.j2`, `curator.j2`, `auditor.j2` (S§8). **Dep:** T1.5
- [ ] **T2.2 Input templates** — `inputs/analyst_input.j2`, `panel_input.j2`, `curator_input.j2`, `auditor_input.j2`. Snapshot-render tests with `StrictUndefined`. **Dep:** T1.5
- [ ] **T2.3 `agents.py`** — build 8 `Agent` nodes (names per S§7), callbacks (budget/image/log/error), temperatures, `mode="single_turn"`; nested `specialist_panel` pieces. **Dep:** T1.2, T1.6, T2.1
- [ ] **T2.4 Analyst on 3 sample flows** — run `screen_analyst` alone; check: all screens returned, verbatim labels, transitions plausible, **recurring components detected** (address, header, order summary). Tune prompt. **Dep:** T2.3
- [ ] **T2.5 Panel + curator on saved inventories** — inspect per-agent output: lens boundaries respected, no cross-screen duplicates, FLOW scope used correctly. Tune prompts. **Dep:** T2.3

---

## M3 — Post-processing (pure Python)

- [ ] **T3.1 Validators** — inventory & candidate validators (S§6 rules), scope/screens coercion, single-screen forcing to SCREEN.
- [ ] **T3.2 `dedupe_across_screens`** — RC-based and Jaccard-based merge → FLOW with union of screens (S§11.2).
- [ ] **T3.3 `coverage`, `fallback_curate`, `apply_patch`** — incl. `rescope`, allowed reasons, unknown `n` handling.
- [ ] **T3.4 Caps, ordering, numbering, size guard** — per-screen/flow/total caps, drop order, FLOW-first numbering, 30k-char loop.
- [ ] **T3.5 Unit tests for M3** — include: same address question from 3 screens → 1 FLOW question; patch edge cases; caps drop P2 first; zero candidates; 1-screen flow.

---

## M4 — Graph workflow

- [ ] **T4.1 `nodes.py`** — function nodes/routers: `build_analyst_input`, `validate_inventory`, `build_panel_input`, `merge_candidates` (≥3 of 5 usable), `build_curator_input`, `validate_curated`, `fallback_curate`, `coverage_check`, `build_audit_input`, `apply_patch`, `finalize`, `publish`, `fail`. **Dep:** T2.2, T3.x
- [ ] **T4.2 `workflow.py`** — edge list per S§9 incl. nested `specialist_panel` (`max_concurrency`), retry loops, fallback route. **Dep:** T4.1, T2.3
- [ ] **T4.3 Graph tests with stub agents** — scenarios in S§14 (retry, abort, partial panel, curator fallback, auditor invalid, budget ≤ 9). **Dep:** T4.2
- [ ] **T4.4 Graph visualization check** — open the workflow in `adk web` (or print the graph) and compare with the Mermaid diagram in A§5; fix drift.

---

## M5 — CLI, rendering, Jira

- [ ] **T5.1 `jira_comment.j2` + render helper** — Common section + per-screen sections, owner index, blocker marker, screen names; snapshot tests for 1- and 4-screen flows. **Dep:** T1.5, T3.4
- [ ] **T5.2 `cli.py`** — args, env fallbacks, folder/single-file detection, build `RunContext`, fetch Jira context, run workflow via `Runner`, read `state["result"]`, exit codes (S§13). **Dep:** T4.2
- [ ] **T5.3 Artifacts** — `flow_inventory.json`, `candidates.json`, `curated.json`, `audit.json`, `comment.txt`, `run_report.json`.
- [ ] **T5.4 End-to-end `--dry-run`** — one 4-screen shopping flow; verify `llm_calls == 8` (2 Gemini + 6 Groq), models per call, comment structure.

---

## M6 — Quality loop & hardening

- [ ] **T6.1 Golden set** — 5 flows: shopping (4 screens: cart, shipping, place order, success), login + OTP, onboarding, a single screen, a flow with a repeated component; with the annotation team write the questions they'd normally ask (recall reference).
- [ ] **T6.2 Evaluate & tune** — annotators rate each question (useful / generic / answerable-from-image / duplicate / wrong scope); measure recall; tune prompts, targets, dedupe thresholds (Jaccard 0.6), lens boundaries. **Dep:** T6.1, T5.4
  *Targets:* ≥ 80 % useful, ≥ 90 % recall, 0 cross-screen duplicates, 0 answerable-from-image.
- [ ] **T6.3 Rate limits** — verify 5 parallel Groq calls on free tier; tune `PANEL_CONCURRENCY`; HTTP backoff for 429/5xx (not counted as logical calls); friendly quota message.
- [ ] **T6.4 Real post + idempotency** — post to sandbox ticket; re-run → marker skip; `--force` → second comment; confirm `@product team` plain text doesn't notify anyone; test a comment near the size guard.
- [ ] **T6.5 Edge cases** — 1 image, 8 images, 9 images (exit 1), unsorted filenames, non-image files in folder, very tall image (downscale only).
- [ ] **T6.6 README + demo script** — setup, run examples, graph diagram, before/after Jira comment, known limitations (file-order = flow order, plain-text mentions, ≤ 8 screens).
- [ ] **T6.7 Final acceptance check** — tick every box in S§14; keep `run_report.json` (shows 8 calls, 2 Gemini / 6 Groq) for the demo.

---

## Definition of done (Phase 1)

1. `python run.py --file-path flows/shopping --jira-id KEY` posts one well-formed comment (Common + per-screen sections); `python run.py` works with `.env` only.
2. 8 planned / ≤ 9 hard-cap LLM calls regardless of screen count; Gemini only for the two image calls; Groq for the 6 text calls; all models from `.env`.
3. Orchestration is an ADK 2.0 graph `Workflow` with nested `specialist_panel`.
4. Common questions appear once with an "applies to" screen list; no cross-screen duplicates.
5. All S§14 acceptance criteria met on the golden set; offline tests pass.

---

## Phase 2 backlog (not now)

- [ ] Parse PMO answers from the comment thread (`Q7: …`) and map to Annotator annotations.
- [ ] Skip questions already answered in earlier comments / post only a delta when the flow changes (marker hash diff).
- [ ] Real @mentions via label → Jira accountId map.
- [ ] API-catalog (OpenAPI/Swagger) lookup node before the panel; turn "what is the endpoint?" into "confirm this endpoint".
- [ ] Optional `context.txt` / `flow.yaml` in the flow folder (explicit screen order, names, notes).
- [ ] Batch mode over many flow folders mapped to Jira keys; flows with > 8 screens via chunked analysis.
- [ ] Optional tiling for very tall screens.
