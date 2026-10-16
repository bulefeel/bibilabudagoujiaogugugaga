from dataclasses import asdict

import pytest

from ziniao_automation.config import PROJECT_ROOT
from ziniao_automation.workflows.amazon_inventory_gpsr.config import InventoryGpsrPolicy
from ziniao_automation.workflows.store_setup.adapter import AmazonStoreSetupPage
from ziniao_automation.workflows.store_setup.policy import OnboardingPolicy


@pytest.mark.parametrize('field,value', [
    ('announcement_timeout_seconds',0), ('announcement_timeout_seconds',float('inf')),
    ('announcement_stable_seconds',-1), ('announcement_stable_seconds',12),
    ('announcement_max_closes',0), ('announcement_max_closes',6), ('announcement_max_closes',True),
])
def test_invalid_announcement_policy_rejected(field,value):
    with pytest.raises(ValueError):InventoryGpsrPolicy(**{field:value})


def test_shared_config_is_used_by_setup_inventory_adapter(tmp_path):
    values=asdict(InventoryGpsrPolicy(announcement_max_closes=1,announcement_timeout_seconds=7))
    path=tmp_path/'inventory-gpsr-policy.toml'
    path.write_text('[timing]\n'+'\n'.join(f'{k}={v}' for k,v in values.items()),encoding='utf-8')
    policy=OnboardingPolicy.load(PROJECT_ROOT/'onboarding-policy.toml')
    parent=AmazonStoreSetupPage(policy,inventory_policy_path=path)
    timing=parent._inventory_adapter().inventory.timing
    assert timing.announcement_max_closes==1 and timing.announcement_timeout_seconds==7
    assert timing.panel_timeout_seconds==policy.candidate_ready_timeout_ms/1000


def test_shipped_configuration_contains_announcement_parameters():
    policy=InventoryGpsrPolicy.from_file(PROJECT_ROOT/'inventory-gpsr-policy.toml')
    assert policy.announcement_max_closes==3
