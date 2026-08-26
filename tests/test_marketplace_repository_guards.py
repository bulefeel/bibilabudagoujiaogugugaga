from __future__ import annotations

from pathlib import Path

import pytest

from ziniao_automation.config import Settings
from ziniao_automation.db import create_sqlite_engine, init_database, make_session_factory
from ziniao_automation.models import StoreMarketplace
from ziniao_automation.repositories import ScheduleRepository, StoreRepository


@pytest.fixture()
def sessions(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'marketplaces.db').as_posix()}",
        testing=True,
    )
    engine = create_sqlite_engine(settings)
    init_database(engine)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


def _store(factory) -> int:
    with factory() as db:
        store = StoreRepository(db).create(
            name="Marketplace Guard Store",
            selector_type="oauth",
            selector_value="marketplace-guard-oauth",
            expected_seller_id="SELLER-GUARD",
        )
        db.commit()
        return store.id


def _schedule_values(store_id: int, codes: list[str]) -> dict[str, object]:
    return {
        "store_id": store_id,
        "name": "Guarded schedule",
        "workflow": "amazon_disbursement",
        "mode": "dry_run",
        "local_time": "09:00",
        "marketplace_codes": codes,
        "enabled": False,
    }




def test_replace_marketplaces_rejects_duplicate_codes(sessions) -> None:
    store_id = _store(sessions)
    with sessions() as db:
        with pytest.raises(ValueError, match="站点 CA 重复"):
            StoreRepository(db).replace_marketplaces(
                store_id,
                [{"code": "CA", "enabled": True}, {"code": "ca", "enabled": False}],
            )










def test_new_seller_can_be_explicitly_confirmed_in_the_same_patch(sessions) -> None:
    store_id = _store(sessions)
    with sessions() as db:
        repo = StoreRepository(db)
        store = repo.get(store_id)
        store.identity_confirmed = True
        store.enabled = True
        db.flush()

        updated = repo.update(
            store_id,
            expected_seller_id="SELLER-CHANGED",
            identity_confirmed=True,
        )

        assert updated.expected_seller_id == "SELLER-CHANGED"
        assert updated.identity_confirmed is True
        # Availability is workflow-neutral. Payout schedules still require
        # the newly confirmed identity, while identity-free workflows remain
        # allowed to use this Ziniao environment.
        assert updated.enabled is True


def test_ziniao_environment_change_always_revokes_same_patch_confirmation(
    sessions,
) -> None:
    store_id = _store(sessions)
    with sessions() as db:
        repo = StoreRepository(db)
        store = repo.get(store_id)
        store.identity_confirmed = True
        store.enabled = True
        db.flush()

        updated = repo.update(
            store_id,
            selector_value="different-ziniao-environment",
            identity_confirmed=True,
            enabled=True,
        )

        assert updated.identity_confirmed is False
        assert updated.enabled is False








def test_schedule_allows_empty_account_for_dry_run_and_approval(sessions) -> None:
    store_id = _store(sessions)
    with sessions() as db:
        db.add(
            StoreMarketplace(
                store_id=store_id,
                code="CA",
                domain="sellercentral.amazon.ca",
                currency="CAD",
                enabled=True,
            )
        )
        db.flush()
        dry_run = ScheduleRepository(db).create(**_schedule_values(store_id, ["CA"]))
        assert dry_run.mode == "dry_run"
        approval_values = _schedule_values(store_id, ["CA"])
        approval_values["mode"] = "approval"
        approval = ScheduleRepository(db).create(**approval_values)
        assert approval.mode == "approval"
