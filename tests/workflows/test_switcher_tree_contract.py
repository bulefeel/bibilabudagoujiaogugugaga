"""Replay sanitized real tree samples; synthetic variants are labeled explicitly."""
from dataclasses import replace
from pathlib import Path
import json
import hashlib

import pytest

from ziniao_automation.workflows.amazon_switcher_contract import (
    AccountCandidate, MarketplaceCandidate, SwitcherSnapshot,
    read_switcher_snapshot, choose_marketplace, parse_marketplace_text,
)
from ziniao_automation.workflows.amazon_account_switcher import MARKETPLACE_NAMES
from ziniao_automation.workflows.setup_diagnostics import switch_diagnostic
from ziniao_automation.workflows.store_setup.identity import SetupRejected
from tests.workflows.fixture_dom import FixturePage
from tests.workflows.test_store_setup_adapter import adapter, site

FIXTURES = Path(__file__).parents[1] / 'fixtures/amazon_account_switcher'
CA, UK, AU = 'sellercentral.amazon.ca', 'sellercentral.amazon.co.uk', 'sellercentral.amazon.com.au'

def fixture(sample):
    return (FIXTURES / f'{sample.lower()}-20260927.html').read_text(encoding='utf-8')

def page(sample):
    return FixturePage(fixture(sample), 'https://sellercentral.amazon.ca/account-switcher/default/merchantMarketplace')


@pytest.mark.parametrize('sample,count,current', [('S01',8,False),('S02',15,False),('S03',15,True)])
async def test_real_tree_separates_account_from_countries(sample,count,current):
    snapshot = await read_switcher_snapshot(page(sample),candidate_limit=50)
    assert snapshot.ready
    assert len(snapshot.accounts) == 1
    assert len(snapshot.marketplaces) == count
    assert bool(snapshot.accounts[0].current_identity) is current
    assert snapshot.accounts[0].text not in [row.text for row in snapshot.marketplaces]
    assert {row.account_index for row in snapshot.marketplaces} == {0}


@pytest.mark.parametrize('sample,domain,reason,state', [
    ('S01',CA,'','AVAILABLE'), ('S01',UK,'registration_required','PENDING_REGISTRATION'),
    ('S01',AU,'registration_required','PENDING_REGISTRATION'),
    ('S02',CA,'not_offered',None), ('S02',UK,'','AVAILABLE'),
    ('S02',AU,'registration_required','PENDING_REGISTRATION'),
    ('S03',UK,'','CURRENT'),
])
async def test_real_countries_have_distinct_states(sample,domain,reason,state):
    snapshot=await read_switcher_snapshot(page(sample))
    index, refusal=choose_marketplace(snapshot,MARKETPLACE_NAMES[domain])
    assert refusal == reason
    if not reason:
        assert snapshot.marketplaces[index].name_and_status[1] == state
    else:
        assert index is None


@pytest.mark.parametrize('sample',['S01','S02'])
async def test_real_single_account_without_current_is_not_auto_bound(sample):
    subject,nav=adapter('<body></body>',fixture(sample))
    with pytest.raises(SetupRejected) as raised:
        await subject.detect_identity(nav.page,site())
    assert raised.value.diagnostic.reason_code == 'IDENTITY_CURRENT_NOT_FOUND'
    assert subject.last_identity_evidence == {}


async def test_real_current_seller_is_read_even_with_same_class_as_country():
    subject,nav=adapter('<body></body>',fixture('S03'))
    identity,source=await subject.detect_identity(nav.page,site())
    assert identity == 'ACCOUNT_REDACTED_1'
    assert source == 'account_switcher:current'
    assert '英国' not in identity


async def test_current_country_does_not_prove_current_account():
    # Synthetic change to the saved current sample: remove only root current.
    html=fixture('S03').replace('ACCOUNT_REDACTED_1 (当前)','ACCOUNT_REDACTED_1')
    subject,nav=adapter('<body></body>',html)
    with pytest.raises(SetupRejected) as raised:
        await subject.detect_identity(nav.page,site())
    assert raised.value.diagnostic.reason_code == 'IDENTITY_CURRENT_NOT_FOUND'


async def test_unrelated_global_current_button_is_ignored():
    # Synthetic outside-root button must not contaminate account selection.
    html=fixture('S03').replace('</body>','<button>OTHER (current)</button></body>')
    subject,nav=adapter('<body></body>',html)
    identity,_=await subject.detect_identity(nav.page,site())
    assert identity == 'ACCOUNT_REDACTED_1'


async def test_account_named_like_a_country_is_not_a_marketplace_candidate():
    html=fixture('S01').replace('ACCOUNT_REDACTED_1','英国')
    snapshot=await read_switcher_snapshot(FixturePage(html,page('S01').url))
    assert choose_marketplace(snapshot,MARKETPLACE_NAMES[UK]) == (None,'registration_required')


async def test_candidate_limit_is_not_a_partial_success():
    snapshot=await read_switcher_snapshot(page('S01'),candidate_limit=3)
    assert snapshot.truncated
    assert choose_marketplace(snapshot,MARKETPLACE_NAMES[CA]) == (None,'candidate_limit')


@pytest.mark.parametrize('text,name,state', [
    (' 加拿大 （ 当前 ） ','加拿大','CURRENT'),
    ('United Kingdom (current)','United Kingdom','CURRENT'),
    ('英国（待注册）','英国','PENDING_REGISTRATION'),
    ('英國 (待註冊)','英國','PENDING_REGISTRATION'),
    ('Australia (Pending registration)','Australia','PENDING_REGISTRATION'),
    ('英国（暂停）','英国','UNKNOWN'),
    ('英国站','英国站','AVAILABLE'),
    ('英国（当前）（待注册）','英国（当前）（待注册）','UNKNOWN'),
])
def test_only_whitelisted_status_suffixes_are_normalized(text,name,state):
    assert parse_marketplace_text(text) == (name,state)


async def test_two_accounts_cannot_select_a_unique_country_owned_by_other_account():
    snapshot=SwitcherSnapshot(
        (AccountCandidate('A (current)'),AccountCandidate('B')),
        (MarketplaceCandidate(0,1,'加拿大'),),True)
    assert choose_marketplace(snapshot,MARKETPLACE_NAMES[CA]) == (None,'account_mismatch')
    assert choose_marketplace(replace(snapshot,accounts=(AccountCandidate('A'),AccountCandidate('B'))),MARKETPLACE_NAMES[CA]) == (None,'ambiguous')


def test_duplicates_and_disabled_targets_never_select():
    snapshot=SwitcherSnapshot((AccountCandidate('A (current)'),),
        (MarketplaceCandidate(0,0,'英国'),MarketplaceCandidate(1,0,'英国（待注册）')),True)
    assert choose_marketplace(snapshot,MARKETPLACE_NAMES[UK]) == (None,'ambiguous')
    snapshot=replace(snapshot,marketplaces=(MarketplaceCandidate(0,0,'英国',True),))
    assert choose_marketplace(snapshot,MARKETPLACE_NAMES[UK]) == (None,'unavailable')


@pytest.mark.parametrize('reason,expected', [
    ('registration_required','REGISTRATION_REQUIRED'),('unavailable','MARKETPLACE_UNAVAILABLE'),
    ('layout_unknown','MARKETPLACE_LAYOUT_UNKNOWN'),('candidate_limit','MARKETPLACE_CANDIDATE_LIMIT'),
    ('account_mismatch','IDENTITY_MISMATCH'),
])
def test_new_reasons_are_fixed_safe_diagnostics(reason,expected):
    diagnostic=switch_diagnostic(reason)
    assert diagnostic.reason_code == expected


def test_real_fixture_provenance_matches_bytes():
    for item in json.loads((FIXTURES/'provenance.json').read_text(encoding='utf-8')):
        content=(FIXTURES/item['fixture']).read_bytes()
        assert hashlib.sha256(content).hexdigest() == item['sha256']
        text=content.decode('utf-8')
        assert not any(value in text.lower() for value in ('https://','<script','token=','cookie=','password='))


async def test_two_same_named_current_accounts_are_still_ambiguous():
    # Synthetic duplicate roots: same display name does not prove same seller.
    row='<div class="full-page-account-switcher-account"><button class="full-page-account-switcher-account-details">Same (current)</button></div>'
    html=('<body><div class="full-page-account-switcher"><div class="full-page-account-switcher-inner">'
          '<div class="full-page-account-switcher-accounts-wrapper"><div class="full-page-account-switcher-accounts">'
          + row + row + '</div></div></div></div></body>')
    subject,nav=adapter('<body></body>',html)
    with pytest.raises(SetupRejected) as raised:
        await subject.detect_identity(nav.page,site())
    assert raised.value.diagnostic.reason_code == 'IDENTITY_AMBIGUOUS'
    assert raised.value.diagnostic.candidate_count == 2
