import pytest

from ziniao_automation.config import PROJECT_ROOT
from ziniao_automation.workflows.amazon_inventory_gpsr.config import InventoryGpsrPolicy
from ziniao_automation.workflows.amazon_inventory_gpsr.page import InventoryMarketplaceError
from ziniao_automation.workflows.amazon_inventory_gpsr.workflow import _safe_reconciliation_failure


@pytest.mark.parametrize('field,value', [
    ('marketplace_menu_stable_seconds', -1), ('marketplace_menu_stable_seconds', 45),
    ('marketplace_menu_stable_seconds', float('inf')),
    ('marketplace_menu_reopen_delay_seconds', 0), ('marketplace_menu_reopen_delay_seconds', float('nan')),
    ('marketplace_menu_reopen_attempts', 2), ('marketplace_menu_reopen_attempts', True),
])
def test_menu_policy_rejects_invalid_parameters(field, value):
    with pytest.raises(ValueError): InventoryGpsrPolicy(**{field: value})


def test_root_policy_keeps_menu_distinct_from_sidebar_stability():
    policy = InventoryGpsrPolicy.from_file(PROJECT_ROOT / 'inventory-gpsr-policy.toml')
    assert policy.marketplace_menu_stable_seconds == 1
    assert policy.stable_seconds == 3
    assert policy.marketplace_menu_reopen_attempts == 1
    assert policy.marketplace_menu_reopen_delay_seconds == 3


def test_error_dto_keeps_menu_reason_even_when_caused_by_timeout():
    try:
        try: raise TimeoutError('TOKEN=PRIVATE signed URL')
        except TimeoutError as exc: raise InventoryMarketplaceError('closed') from exc
    except InventoryMarketplaceError as exc:
        details, message = _safe_reconciliation_failure(exc)
    assert details['reason_code'] == 'marketplace_menu_closed'
    assert '菜单' in message and '收起' in message and 'PRIVATE' not in str((details, message))
