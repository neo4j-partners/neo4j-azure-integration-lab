"""
Unit tests for src/password.py.

Covers generated password properties, environment variable reading,
interactive prompt validation, and caching behavior.
"""

import os
import string
from unittest.mock import patch

import pytest

from src.models import PasswordStrategy, Settings
from src.password import PasswordManager


def _settings(strategy: PasswordStrategy) -> Settings:
    return Settings(
        subscription_id="00000000-0000-0000-0000-000000000000",
        subscription_name="Test Subscription",
        owner_email="test@example.com",
        password_strategy=strategy,
    )


# ---------------------------------------------------------------------------
# Password generation
# ---------------------------------------------------------------------------

class TestGeneratePassword:
    def _generate(self) -> str:
        mgr = PasswordManager(_settings(PasswordStrategy.GENERATE))
        with patch("src.password.console"):
            return mgr._generate_password()

    def test_length_is_24(self):
        assert len(self._generate()) == 24

    def test_has_lowercase(self):
        assert any(c.islower() for c in self._generate())

    def test_has_uppercase(self):
        assert any(c.isupper() for c in self._generate())

    def test_has_digit(self):
        assert any(c.isdigit() for c in self._generate())

    def test_has_special_char(self):
        specials = set("!@#$%^&*()-_=+[]{}|;:,.<>?")
        assert any(c in specials for c in self._generate())

    def test_only_allowed_characters(self):
        allowed = set(string.ascii_letters + string.digits + "!@#$%^&*()-_=+[]{}|;:,.<>?")
        for _ in range(20):
            pw = self._generate()
            unexpected = [c for c in pw if c not in allowed]
            assert not unexpected, f"Unexpected chars {unexpected!r} in: {pw!r}"

    def test_unique_across_calls(self):
        """Cryptographic generation should not repeat in practice."""
        passwords = {self._generate() for _ in range(20)}
        assert len(passwords) > 1


# ---------------------------------------------------------------------------
# get_password — GENERATE strategy
# ---------------------------------------------------------------------------

class TestGetPasswordGenerate:
    def test_returns_24_char_string(self):
        mgr = PasswordManager(_settings(PasswordStrategy.GENERATE))
        with patch("src.password.console"):
            pw = mgr.get_password("test-scenario")
        assert isinstance(pw, str)
        assert len(pw) == 24

    def test_caches_across_scenarios(self):
        mgr = PasswordManager(_settings(PasswordStrategy.GENERATE))
        with patch("src.password.console"):
            pw1 = mgr.get_password("scenario-a")
            pw2 = mgr.get_password("scenario-b")
        assert pw1 == pw2

    def test_clear_cache_allows_new_generation(self):
        mgr = PasswordManager(_settings(PasswordStrategy.GENERATE))
        with patch("src.password.console"):
            mgr.get_password("scenario-a")
            mgr.clear_cache()
            pw2 = mgr.get_password("scenario-a")
        assert isinstance(pw2, str)
        assert len(pw2) == 24

    def test_clear_cache_resets_to_none(self):
        mgr = PasswordManager(_settings(PasswordStrategy.GENERATE))
        with patch("src.password.console"):
            mgr.get_password("scenario-a")
        mgr.clear_cache()
        assert mgr._cached_password is None


# ---------------------------------------------------------------------------
# get_password — ENVIRONMENT strategy
# ---------------------------------------------------------------------------

class TestGetPasswordEnvironment:
    def test_reads_from_env_var(self):
        mgr = PasswordManager(_settings(PasswordStrategy.ENVIRONMENT))
        with patch.dict(os.environ, {"NEO4J_ADMIN_PASSWORD": "StrongPass123!"}):
            with patch("src.password.console"):
                pw = mgr.get_password("scenario")
        assert pw == "StrongPass123!"

    def test_raises_when_var_not_set(self):
        mgr = PasswordManager(_settings(PasswordStrategy.ENVIRONMENT))
        env = {k: v for k, v in os.environ.items() if k != "NEO4J_ADMIN_PASSWORD"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="NEO4J_ADMIN_PASSWORD"):
                mgr.get_password("scenario")

    def test_raises_when_var_is_empty(self):
        mgr = PasswordManager(_settings(PasswordStrategy.ENVIRONMENT))
        with patch.dict(os.environ, {"NEO4J_ADMIN_PASSWORD": ""}):
            with pytest.raises(ValueError, match="NEO4J_ADMIN_PASSWORD"):
                mgr.get_password("scenario")

    def test_warns_on_short_password(self):
        mgr = PasswordManager(_settings(PasswordStrategy.ENVIRONMENT))
        with patch.dict(os.environ, {"NEO4J_ADMIN_PASSWORD": "short"}):
            with patch("src.password.console") as mock_console:
                pw = mgr.get_password("scenario")
        assert pw == "short"
        call_text = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "Warning" in call_text or "shorter" in call_text

    def test_no_warning_on_long_enough_password(self):
        mgr = PasswordManager(_settings(PasswordStrategy.ENVIRONMENT))
        with patch.dict(os.environ, {"NEO4J_ADMIN_PASSWORD": "StrongPass123!"}):
            with patch("src.password.console") as mock_console:
                mgr.get_password("scenario")
        call_text = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "Warning" not in call_text

    def test_caches_env_result(self):
        mgr = PasswordManager(_settings(PasswordStrategy.ENVIRONMENT))
        with patch.dict(os.environ, {"NEO4J_ADMIN_PASSWORD": "StrongPass123!"}):
            with patch("src.password.console"):
                pw1 = mgr.get_password("scenario")
        # Env var unset — cache should still serve the value
        env = {k: v for k, v in os.environ.items() if k != "NEO4J_ADMIN_PASSWORD"}
        with patch.dict(os.environ, env, clear=True):
            with patch("src.password.console"):
                pw2 = mgr.get_password("scenario")
        assert pw1 == pw2 == "StrongPass123!"


# ---------------------------------------------------------------------------
# _prompt_for_password validation
# ---------------------------------------------------------------------------

class TestPromptValidation:
    """
    Tests for _prompt_for_password. Uses mock to inject passwords
    without interactive input so we can test validation in isolation.
    """

    def _prompt(self, password: str) -> str:
        mgr = PasswordManager(_settings(PasswordStrategy.PROMPT))
        with patch("src.password.Prompt.ask", return_value=password):
            with patch("src.password.console"):
                return mgr._prompt_for_password("test-scenario")

    def test_valid_password_returned(self):
        pw = self._prompt("ValidPass123!")
        assert pw == "ValidPass123!"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            self._prompt("")

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            self._prompt("Short1!")  # 7 chars

    def test_too_long_raises(self):
        with pytest.raises(ValueError, match="too long"):
            self._prompt("A" * 73)

    def test_exactly_12_chars_is_valid(self):
        pw = self._prompt("ValidPass12!")
        assert len(pw) == 12

    def test_exactly_72_chars_is_valid(self):
        pw = self._prompt("ValidPass12!" + "a" * 60)
        assert len(pw) == 72

    def test_only_lowercase_fails_complexity(self):
        # 1 complexity category — fails
        with pytest.raises(ValueError, match="3 of"):
            self._prompt("alllowercaseee")  # 14 chars, only lowercase

    def test_two_categories_fails_complexity(self):
        # lowercase + uppercase only — fails
        with pytest.raises(ValueError, match="3 of"):
            self._prompt("LowerAndUpperOnly")  # no digit or special

    def test_three_categories_passes(self):
        # lowercase + uppercase + digit (no special) — passes
        pw = self._prompt("LowercaseMixed1234")
        assert pw == "LowercaseMixed1234"

    def test_all_four_categories_passes(self):
        pw = self._prompt("ValidPass123!@#")
        assert pw == "ValidPass123!@#"
