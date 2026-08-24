"""User-facing probe errors must never contain secret-shaped values."""

from __future__ import annotations

from ziniao_automation.web import _safe_probe_error


def test_safe_probe_error_keeps_field_names_and_redacts_values() -> None:
    rendered = _safe_probe_error(
        RuntimeError(
            "token=TOKEN_VALUE secret:SECRET_VALUE "
            "password PASSWORD_VALUE app_secret='APP_SECRET_VALUE' end"
        )
    )

    assert "\x01" not in rendered
    assert "TOKEN_VALUE" not in rendered
    assert "SECRET_VALUE" not in rendered
    assert "PASSWORD_VALUE" not in rendered
    assert "APP_SECRET_VALUE" not in rendered
    assert "token=***" in rendered.lower()
    assert "secret=***" in rendered.lower()
    assert "password=***" in rendered.lower()
    assert "app_secret=***" in rendered.lower()


def test_safe_probe_error_redacts_quoted_spaces_and_json_values() -> None:
    rendered = _safe_probe_error(
        RuntimeError(
            '{"app_secret": "alpha beta", "token": "gamma delta"}; '
            "password='two words'; secret=unquoted value with spaces"
        )
    )

    for value in ("alpha", "beta", "gamma", "delta", "two words", "unquoted"):
        assert value not in rendered
    assert '"app_secret": "' not in rendered
    assert "app_secret=***" in rendered.lower()
    assert "token=***" in rendered.lower()
