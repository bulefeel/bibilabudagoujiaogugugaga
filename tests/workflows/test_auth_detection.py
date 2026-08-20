from __future__ import annotations

import pytest

from ziniao_automation.workflows.amazon_disbursement import AmazonPaymentsPage
from ziniao_automation.workflows.errors import HumanAuthRequired
from tests.workflows.fixture_dom import FixturePage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "html", "expected_kind"),
    (
        ("https://www.amazon.ca/ap/signin", "<html><body></body></html>", "sign_in"),
        ("https://sellercentral.amazon.ca/ap/mfa", "<html><body></body></html>", "challenge"),
        (
            "https://sellercentral.amazon.ca/payments/dashboard/index.html",
            '<html><body><img id="auth-captcha-image" src="captcha.jpg"></body></html>',
            "challenge",
        ),
        (
            "https://sellercentral.amazon.ca/payments/dashboard/index.html",
            '<html><body><input autocomplete="one-time-code"></body></html>',
            "challenge",
        ),
        (
            "https://sellercentral.amazon.ca/payments/dashboard/index.html",
            '<html><body><div data-testid="webauthn-challenge"></div></body></html>',
            "challenge",
        ),
        (
            "https://sellercentral.amazon.ca/payments/dashboard/index.html",
            "<html><body>Authenticator app one-time code</body></html>",
            "challenge",
        ),
    ),
)
async def test_login_captcha_otp_and_passkey_are_detected(
    url: str, html: str, expected_kind: str
) -> None:
    page = FixturePage(html, url)
    with pytest.raises(HumanAuthRequired) as caught:
        await AmazonPaymentsPage()._raise_if_auth(page)
    assert caught.value.kind == expected_kind


