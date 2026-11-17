import pytest

from google.adk.models.lite_llm import LiteLlm

from src.cli import main
from src.config import ConfigError, load_settings
from src.models import build_model, model_name, validate_slots


def settings(**env):
    return load_settings(env_file=None, environ={"VISION_MODEL": "gemini-v", "TEXT_MODEL": "groq/t", **env})


def test_slots_resolve_with_overrides():
    s = settings(MODEL_CURATOR="groq/big", MODEL_AUDITOR="gemini-pro")
    assert validate_slots(s) == {"analyst": "gemini-v", "auditor": "gemini-pro",
                                 "specialists": "groq/t", "curator": "groq/big"}
    assert model_name(s, "specialists") == "groq/t"


@pytest.mark.parametrize("env, msg", [
    ({"VISION_MODEL": "groq/x"}, "analyst: VISION_MODEL='groq/x' must be a Gemini"),
    ({"TEXT_MODEL": "gemini-x"}, "specialists: TEXT_MODEL='gemini-x' must be a Groq"),
    ({"MODEL_AUDITOR": "gpt-4o"}, "auditor: MODEL_AUDITOR='gpt-4o' must be a Gemini"),
    ({"MODEL_SPECIALISTS": "gemini-x"}, "specialists: MODEL_SPECIALISTS='gemini-x' must be a Groq"),
    ({"VISION_MODEL": ""}, "analyst: no model set (VISION_MODEL is empty)"),
])
def test_misconfigured_slot_raises(env, msg):
    with pytest.raises(ConfigError) as e:
        validate_slots(settings(**env))
    assert msg in str(e.value)


def test_build_model():
    assert build_model("gemini-v") == "gemini-v"
    groq = build_model("groq/t", json_mode=True)
    assert isinstance(groq, LiteLlm) and groq.model == "groq/t"
    assert groq._additional_args["response_format"] == {"type": "json_object"}
    with pytest.raises(ConfigError):
        build_model("openai/gpt")


def test_cli_misconfigured_slot_exits_1(monkeypatch, capsys):
    monkeypatch.setattr("src.cli.load_settings", lambda cli: settings(TEXT_MODEL="gemini-wrong"))
    with pytest.raises(SystemExit) as e:
        main(["--dry-run"])
    assert e.value.code == 1
    assert "must be a Groq model" in capsys.readouterr().err
