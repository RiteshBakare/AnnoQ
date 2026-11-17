import asyncio
import json
import threading
from types import SimpleNamespace

from src.budget import LLMBudget, make_callbacks
from src.config import load_settings
from src.image_utils import load_flow
from src.run_context import RunContext


def test_tenth_call_refused():
    b = LLMBudget(9)
    assert all(b.consume(f"a{i}") for i in range(9))
    assert b.consume("a9") is False and b.used == 9 and b.refused == ["a9"] and b.remaining == 0


def test_cap_never_above_nine():
    assert LLMBudget(50).max == 9


def test_concurrent_threads_consume_exactly_cap():
    b, results = LLMBudget(9), []
    threads = [threading.Thread(target=lambda: results.append(b.consume("t"))) for _ in range(64)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 9 and b.used == 9


def test_concurrent_async_consume_exactly_cap():
    b = LLMBudget(9)

    async def run():
        async def one(i):
            await asyncio.sleep(0)
            return b.consume(f"a{i}")
        return await asyncio.gather(*(one(i) for i in range(20)))

    assert sum(asyncio.run(run())) == 9


def ctx(name):
    return SimpleNamespace(agent_name=name)


def request(model="m"):
    return SimpleNamespace(contents=[], model=model)


def response(prompt=10, out=5):
    return SimpleNamespace(partial=False, usage_metadata=SimpleNamespace(prompt_token_count=prompt,
                                                                         candidates_token_count=out))


def make_rc(make_flow_dir, max_calls=9):
    s = load_settings(env_file=None, environ={"MAX_LLM_CALLS": str(max_calls)})
    return RunContext(settings=s, flow=load_flow(make_flow_dir(["1.png", "2.png"])), jira_key="P-1")


def test_callbacks_log_attach_images_and_refuse(make_flow_dir):
    rc = make_rc(make_flow_dir, max_calls=2)
    vision, text = make_callbacks(rc, attach_images=True), make_callbacks(rc, attach_images=False)

    req = request("gemini-x")
    assert vision["before_model_callback"](ctx("screen_analyst"), req) is None
    assert sum(1 for p in req.contents[-1].parts if p.inline_data) == 2  # all screens attached
    vision["after_model_callback"](ctx("screen_analyst"), response(100, 20))

    req = request("groq/y")
    assert text["before_model_callback"](ctx("data_source_agent"), req) is None
    assert req.contents == []  # text agents never get images
    text["after_model_callback"](ctx("data_source_agent"), response())

    stub = text["before_model_callback"](ctx("flow_curator"), request("groq/y"))  # 3rd call, cap 2
    assert json.loads(stub.content.parts[0].text) == {"error": "budget_exhausted"}
    text["after_model_callback"](ctx("flow_curator"), SimpleNamespace(partial=False, usage_metadata=None))

    assert [(c.agent, c.model, c.prompt_tokens, c.ok) for c in rc.calls] == [
        ("screen_analyst", "gemini-x", 100, True), ("data_source_agent", "groq/y", 10, True)]
    assert any("budget exhausted" in w for w in rc.warnings)


def test_error_callback_stub_logged_once(make_flow_dir):
    rc = make_rc(make_flow_dir)
    cbs = make_callbacks(rc, attach_images=False)
    cbs["before_model_callback"](ctx("validation_agent"), request("groq/y"))
    stub = cbs["on_model_error_callback"](ctx("validation_agent"), request("groq/y"), RuntimeError("down"))
    assert json.loads(stub.content.parts[0].text)["error"] == "model_error"
    cbs["after_model_callback"](ctx("validation_agent"), SimpleNamespace(partial=False, usage_metadata=None))
    assert [(c.agent, c.ok) for c in rc.calls] == [("validation_agent", False)]
    assert rc.budget.used == 1 and "validation_agent: model error RuntimeError" in rc.warnings
