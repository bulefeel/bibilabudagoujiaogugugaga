import pytest

from ziniao_automation.workflows.amazon_login import AmazonLoginAdvancer


def test_inventory_return_ends_login_without_widening_payment_handoff():
    host = "sellercentral.amazon.de"
    assert AmazonLoginAdvancer.is_supported_business_url(
        f"https://{host}/myinventory/inventory?status=approval_required&page=1",
        expected_host=host,
    )
    assert not AmazonLoginAdvancer.is_supported_business_url(
        f"https://{host}/payments/disburse/details", expected_host=host
    )


@pytest.mark.parametrize("url", [
    "https://sellercentral.amazon.de.evil.invalid/myinventory/inventory",
    "https://sellercentral.amazon.co.uk/myinventory/inventory",
    "https://sellercentral.amazon.de/myinventory/inventory/",
    "https://sellercentral.amazon.de/myinventory/inventory-else",
    "https://sellercentral.amazon.de/myinventory/inventory;session=1",
])
def test_inventory_handoff_rejects_other_hosts_and_paths(url):
    assert not AmazonLoginAdvancer.is_supported_business_url(
        url, expected_host="sellercentral.amazon.de"
    )


@pytest.mark.parametrize("country", ["it", "fr", "es", "pl", "de"])
def test_marketplace_switch_landing_is_exact_business_path(country):
    host = f"sellercentral.amazon.{country}"
    assert AmazonLoginAdvancer.is_supported_business_url(
        f"https://{host}/amazonsell/business", expected_host=host,
    )
    for suffix in ("/", "-else", ";session=1"):
        assert not AmazonLoginAdvancer.is_supported_business_url(
            f"https://{host}/amazonsell/business{suffix}", expected_host=host,
        )
