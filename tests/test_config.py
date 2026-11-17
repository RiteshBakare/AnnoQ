import pytest

from src.config import ConfigError, load_settings, parse_owner_labels

BASE = {"VISION_MODEL": "gemini-a", "TEXT_MODEL": "groq/b", "GOOGLE_API_KEY": "gk-secret", "GROQ_API_KEY": "qk-secret",
        "FILE_PATH": "./flows/x", "JIRA_ISSUE_KEY": "PROJ-1", "JIRA_BASE_URL": "https://x.atlassian.net/",
        "JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "jt-secret"}


def test_precedence_cli_over_process_env_over_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FILE_PATH=./from_dotenv\nJIRA_ISSUE_KEY=DOT-1\nMAX_SCREENS=5  # inline comment\n")
    s = load_settings(env_file=env_file, environ={"JIRA_ISSUE_KEY": "ENV-2"})
    assert (s.file_path, s.jira_issue_key, s.max_screens) == ("./from_dotenv", "ENV-2", 5)
    s = load_settings({"file_path": "cli/path", "jira_id": "CLI-3", "dry_run": True, "verbose": True},
                      env_file=env_file, environ={"JIRA_ISSUE_KEY": "ENV-2"})
    assert (s.file_path, s.jira_issue_key, s.dry_run, s.log_level) == ("cli/path", "CLI-3", True, "DEBUG")


def test_cli_none_values_do_not_override_env():
    s = load_settings({"file_path": None, "jira_id": None, "dry_run": False}, env_file=None, environ=BASE)
    assert s.file_path == "./flows/x" and s.jira_issue_key == "PROJ-1" and s.dry_run is False


@pytest.mark.parametrize("raw, expected", [("50", 9), ("9", 9), ("3", 3), ("0", 1)])
def test_max_llm_calls_clamped_to_hard_cap(raw, expected):
    assert load_settings(env_file=None, environ={"MAX_LLM_CALLS": raw}).max_llm_calls == expected


@pytest.mark.parametrize("raw, expected", [("true", True), ("TRUE", True), ("1", True), ("false", False), ("no", False)])
def test_dry_run_env_parsing(raw, expected):
    assert load_settings(env_file=None, environ={"DRY_RUN": raw}).dry_run is expected


def test_owner_labels():
    assert parse_owner_labels("tech:@devs, product:@pm") == {"product": "@pm", "tech": "@devs", "design": "@design team"}
    with pytest.raises(ConfigError, match="unknown owner"):
        parse_owner_labels("qa:@qa team")
    with pytest.raises(ConfigError, match="owner:label"):
        parse_owner_labels("product")
    with pytest.raises(ConfigError):
        load_settings(env_file=None, environ={"OWNER_LABELS": "qa:@qa"})


def test_invalid_number_is_config_error():
    with pytest.raises(ConfigError, match="MAX_SCREENS"):
        load_settings(env_file=None, environ={"MAX_SCREENS": "lots"})


def test_secrets_never_in_repr():
    s = load_settings(env_file=None, environ=BASE)
    text = repr(s) + str(s.model_dump())
    assert "secret" not in text
    assert s.groq_api_key.get_secret_value() == "qk-secret"
    assert s.jira_base_url == "https://x.atlassian.net"


def test_require_run_inputs():
    load_settings(env_file=None, environ=BASE).require_run_inputs()
    no_jira = {k: v for k, v in BASE.items() if not k.startswith("JIRA_") or k == "JIRA_ISSUE_KEY"}
    with pytest.raises(ConfigError, match="JIRA_API_TOKEN"):
        load_settings(env_file=None, environ=no_jira).require_run_inputs()
    load_settings({"dry_run": True}, env_file=None, environ=no_jira).require_run_inputs()  # dry-run: Jira optional
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        load_settings(env_file=None, environ={**BASE, "GROQ_API_KEY": ""}).require_run_inputs()
