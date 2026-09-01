"""Tests for config helpers."""

from __future__ import annotations

import pytest

from po.config import agent_env


class TestAgentEnv:
    """`agent_env()` must drop nesting markers without taking auth with them."""

    def test_preserves_oauth_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: stripping the whole CLAUDE* prefix logged the agent out.

        CLAUDE_CODE_OAUTH_TOKEN is how a spawned `claude` authenticates. Removing
        it makes the child report "Not logged in - Please run /login" and exit 1,
        even though the parent shell is authenticated.
        """
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token-value")
        assert agent_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "token-value"

    def test_preserves_config_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/.claude")
        assert agent_env()["CLAUDE_CONFIG_DIR"] == "/somewhere/.claude"

    @pytest.mark.parametrize(
        "var", ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"]
    )
    def test_drops_nesting_markers(self, var: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(var, "1")
        assert var not in agent_env()

    def test_preserves_unrelated_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PO_TEST_MARKER", "kept")
        env = agent_env()
        assert env["PO_TEST_MARKER"] == "kept"
        assert "PATH" in env
