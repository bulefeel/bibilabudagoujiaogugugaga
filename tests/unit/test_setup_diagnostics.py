from __future__ import annotations

import ast
import inspect
import json

import pytest

from ziniao_automation.workflows import amazon_login
from ziniao_automation.workflows.errors import HumanAuthRequired
from ziniao_automation.workflows.setup_diagnostics import (
    SetupDiagnostic, classify_switch_landing, switch_diagnostic,
)

DOMAIN = "sellercentral.amazon.com.au"
TARGET = "/payments/dashboard/index.html"


@pytest.mark.parametrize("dispatch_recorded", [None, False, True])
def test_legacy_auth_exception_keeps_money_dispatch_semantics(dispatch_recorded):
    exc = HumanAuthRequired("legacy message", kind="challenge",
                            dispatch_recorded=dispatch_recorded)
    assert str(exc) == "legacy message"
    assert exc.kind == "challenge"
    assert exc.dispatch_recorded is dispatch_recorded
    assert exc.diagnostic.reason_code == "AUTH_UNKNOWN"


@pytest.mark.parametrize("raw", ["Token=secret Cookie=private OTP=123456", [], {}, 7])
def test_diagnostic_dto_rejects_raw_text_and_untyped_fields(raw):
    diagnostic = SetupDiagnostic(
        reason_code=raw, stage=raw, action=raw, attempts=raw,
        candidate_count=raw, page_category=raw,
    )
    payload = diagnostic.to_payload()
    assert payload["reason_code"] == "AUTH_UNKNOWN"
    assert payload["stage"] == "unknown"
    assert payload["action"] is None
    assert payload["page_category"] == "UNKNOWN_PAGE"
    assert "Token=" not in json.dumps(payload)
    assert set(payload) == {
        "reason_code", "stage", "action", "attempts", "candidate_count", "page_category",
    }


@pytest.mark.parametrize("value", [True, -1, 4, "3", 999999])
def test_attempt_counter_is_bounded_without_inventing_an_observation(value):
    assert SetupDiagnostic(attempts=value).attempts is None


def test_switch_reason_is_not_inferred_from_exception_text():
    assert switch_diagnostic("not_offered").reason_code == "MARKETPLACE_NOT_OFFERED"
    assert "开通情况待确认" in switch_diagnostic("not_offered").message
    assert switch_diagnostic("not_offered Cookie=SECRET").reason_code == "MARKETPLACE_UNKNOWN"
    assert HumanAuthRequired("not_offered", kind="marketplace_switch").diagnostic.reason_code == "MARKETPLACE_UNKNOWN"


@pytest.mark.parametrize("suffix,category", [
    (TARGET, "TARGET_PAGE"),
    (TARGET + "?token=SECRET", "TARGET_PAGE"),
    (TARGET + "/", "TARGET_PAGE"),
    ("/inactiveAccount", "INACTIVE_ACCOUNT"),
    ("/inactiveAccount?secret=SECRET", "INACTIVE_ACCOUNT"),
    ("/inactiveAccount/", "UNKNOWN_PAGE"),
    ("/inactiveAccount;token=SECRET", "UNKNOWN_PAGE"),
    ("/inactiveAccountExtra", "UNKNOWN_PAGE"),
    ("/inactiveaccount", "UNKNOWN_PAGE"),
    ("/ap/signin?token=SECRET", "LOGIN"),
    ("/ap/mfa", "LOGIN"),
    ("/appliance", "UNKNOWN_PAGE"),
    ("/account-switcher/default/merchantMarketplace", "SITE_SWITCHER"),
    ("/account-switcher-imitation", "UNKNOWN_PAGE"),
])
def test_landing_categories_do_not_confuse_routes(suffix, category):
    assert classify_switch_landing(
        "https://" + DOMAIN + suffix, domain=DOMAIN, expected_path=TARGET,
    ) == category


@pytest.mark.parametrize("url", [
    "http://sellercentral.amazon.com.au/inactiveAccount",
    "https://sellercentral.amazon.com.au.evil.invalid/inactiveAccount",
    "https://sellercentral.amazon.ca/inactiveAccount",
    "https://sellercentral.amazon.com.au@evil.invalid/inactiveAccount",
    "https://user:secret@sellercentral.amazon.com.au/inactiveAccount",
    "https://sellercentral.amazon.com.au:8765/inactiveAccount",
    "https://sellercentral.amazon.com.au:invalid/inactiveAccount",
    "https://[invalid/inactiveAccount", "about:blank", "",
])
def test_untrusted_origins_do_not_become_known_account_pages(url):
    assert classify_switch_landing(url, domain=DOMAIN, expected_path=TARGET) == "DOMAIN_MISMATCH"


def test_each_login_stop_declares_its_reason_at_the_decision_point():
    tree = ast.parse(inspect.getsource(amazon_login))
    stops = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "HumanAuthRequired"]
    assert len(stops) >= 50
    for node in stops:
        fields = {kw.arg for kw in node.keywords}
        assert {"reason_code", "stage"} <= fields, node.lineno
