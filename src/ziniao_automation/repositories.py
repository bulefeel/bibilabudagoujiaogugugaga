"""Transactional repositories around the SQLite source of truth."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .db import utc_now
from .models import (
    ApprovalRequest,
    Evidence,
    OperationGuard,
    Run,
    RunEvent,
    RunQueueEntry,
    Schedule,
    SiteRun,
    Store,
    StoreMarketplace,
    ZiniaoAccount,
)


V1_MARKETPLACES: dict[str, tuple[str, str]] = {
    "CA": ("sellercentral.amazon.ca", "CAD"),
    "UK": ("sellercentral.amazon.co.uk", "GBP"),
    "AU": ("sellercentral.amazon.com.au", "AUD"),
}


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


# One vocabulary for "may this run stop something else", because four copies of
# it drifted apart three separate times. The rule that matters is not which
# statuses sound alarming, it is which ones an operator can still get out of.
#
# Still going: holds a queue slot, a browser or a lock. Anything that follows
# genuinely has to wait, and waiting ends by itself.
ACTIVE_RUN_STATUSES = frozenset(
    {"QUEUED", "RUNNING", "WAITING_APPROVAL", "WAITING_AUTH", "RECONCILING"}
)
# Nothing here any more, and that is the point: no run status blocks new work.
# ``UNCERTAIN_FINANCIAL`` used to, on the theory that an unread-back payout
# could be sent twice. It cannot. What actually makes a second payout
# impossible is the per-site, per-day guard in the disbursement workflow:
# ``guard_key`` hashes the operator-local day, and a site whose key already has
# ANY guard — including UNCERTAIN — is skipped and never re-armed. Across days
# the key legitimately differs, and by then Amazon's own rolling 24-hour cap has
# also passed and the run re-reads the current payable balance, so yesterday's
# money is simply not there to send again.
#
# So the run-level block never prevented a double payment. It only stopped the
# schedule while a bookkeeping question was open, and made an operator open the
# browser by hand to answer it. Removed on the operator's instruction: a payout
# that silently failed is fixed by the next scheduled run succeeding.
UNRESOLVED_FUNDS_RUN_STATUSES: frozenset[str] = frozenset()
# Dead ends: ``finished_at`` is already stamped, no lock or browser is held, and
# nothing will ever move them along on its own. Blocking on one of these is a
# permanent veto, and it lands precisely when the operator most needs to act.
# ``NEEDS_HUMAN_AUTH`` only means an assisted login ran out of time, and
# ``UNCERTAIN_FINANCIAL`` only means the read-back could not see the payout —
# neither costs anything to leave alone, and both are listed here so the guard
# test below refuses any future attempt to block on them.
#
# This has now been reintroduced three times — on 建档重置, on schedule-driven
# creation, and on manual 「立即执行」 — because each rule kept its own literal
# list and the reasoning lived in a comment attached to only one of them.
# ``test_no_blocking_rule_ever_vetoes_on_a_dead_end_status`` is the guard.
DEAD_END_RUN_STATUSES = frozenset({"NEEDS_HUMAN_AUTH", "UNCERTAIN_FINANCIAL"})


def canonical_hash(value: Any) -> str:
    body = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return sha256(body).hexdigest()


class StoreRepository:
    # Runs that still hold the browser, or that a scheduler may still pick up.
    # Resetting under one of these pulls ``expected_seller_id`` out from under
    # a live identity check, so they legitimately block.
    #
    # Two dead-end statuses are deliberately absent.  ``NEEDS_HUMAN_AUTH`` is
    # written when a login needs a human and nobody came.  ``UNCERTAIN_FINANCIAL``
    # is written when the read-back could not see the payout yet — and Amazon
    # often does not publish one until the next day, so the read-back structurally
    # cannot resolve it.  Neither has an automatic exit, neither holds a lock,
    # and both already stamp ``finished_at``.  Blocking on them made every such
    # run a permanent veto on re-enrolling the store, which is the one action an
    # operator needs precisely when a site got stuck.  Observed in the field:
    # run RUN_EXAMPLE_A sat in UNCERTAIN_FINANCIAL after its only guard had already
    # reached CONFIRMED, and no sequence of operator actions could clear it.
    SETUP_RESET_BLOCKING_RUN_STATUSES = ACTIVE_RUN_STATUSES
    # Guard states no longer block a reset, and there is no set to relax back
    # into.  The rule they enforced protected a locally stored payout-account
    # baseline that ``reset_setup`` used to clear; migration 0005 deleted that
    # baseline entirely.  What remains of reset touches only store identity and
    # schedules — it never reads, writes or deletes ``operation_guards`` — so a
    # money record can no longer be damaged by re-enrolling a store.  Guards
    # stay protected where they always were: ``guard_key`` UNIQUE stops a second
    # payout, and a non-NULL ``submitted_at`` makes the row undeletable by
    # anyone, automatic or human.

    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self, *, enabled: bool | None = None) -> list[Store]:
        stmt = select(Store).options(selectinload(Store.marketplaces)).order_by(Store.name)
        if enabled is not None:
            stmt = stmt.where(Store.enabled.is_(enabled))
        return list(self.session.scalars(stmt).unique())

    def get(self, store_id: int) -> Store:
        store = self.session.scalar(
            select(Store)
            .where(Store.id == store_id)
            .options(selectinload(Store.marketplaces))
        )
        if store is None:
            raise NotFoundError(f"店铺 {store_id} 不存在")
        return store

    def create(
        self,
        *,
        name: str,
        selector_type: str,
        selector_value: str,
        browser_oauth: str | None = None,
        browser_id: str | None = None,
        account_id: int | None = None,
        expected_seller_id: str | None = None,
    ) -> Store:
        if selector_type not in {"oauth", "id"}:
            raise ValueError("selector_type 只能是 oauth 或 id")
        if not selector_value.strip():
            raise ValueError("selector_value 不可为空")
        store = Store(
            name=name.strip(),
            selector_type=selector_type,
            selector_value=selector_value.strip(),
            browser_oauth=browser_oauth,
            browser_id=browser_id,
            account_id=account_id,
            expected_seller_id=expected_seller_id,
            # Merely typing an expected ID is not confirmation.  Financial
            # workflows enforce their own confirmed-identity requirement;
            # store availability itself remains usable by workflows that do
            # not require a Seller Central identity.
            identity_confirmed=False,
            enabled=False,
        )
        self.session.add(store)
        self.session.flush()
        return store

    def update(self, store_id: int, **changes: Any) -> Store:
        store = self.get(store_id)
        allowed = {
            "name",
            "expected_seller_id",
            "identity_confirmed",
            "enabled",
            "browser_oauth",
            "browser_id",
            "selector_type",
            "selector_value",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"不允许修改字段：{', '.join(sorted(unknown))}")
        previous_identity = _seller_identity(store.expected_seller_id)
        previous_selector = (str(store.selector_type), str(store.selector_value))
        seller_identity_changed = False
        if "expected_seller_id" in changes:
            raw_seller = changes["expected_seller_id"]
            normalized_seller = raw_seller.strip() if isinstance(raw_seller, str) else raw_seller
            changes["expected_seller_id"] = normalized_seller or None
            seller_identity_changed = (
                _seller_identity(normalized_seller) != previous_identity
            )
            if seller_identity_changed:
                # A previous confirmation only covers the previous ID.  The
                # editor may, however, submit the newly detected ID together
                # with an explicit human confirmation in one PATCH.  Treat
                # that explicit True as confirmation of the *new* exact ID so
                # the operator does not need a second save.  Omitting the
                # confirmation (or sending False) still revokes both flags.
                explicitly_confirmed = changes.get("identity_confirmed") is True
                changes["identity_confirmed"] = explicitly_confirmed
                # Store availability is independent from Seller Central
                # identity. Workflows that require a confirmed seller remain
                # blocked by their own registry contract and schedule checks.
        future_selector = (
            str(changes.get("selector_type", store.selector_type)),
            str(changes.get("selector_value", store.selector_value)).strip(),
        )
        if future_selector != previous_selector:
            # A confirmed seller identity belongs to one exact Ziniao browser
            # environment. Moving the record to another environment must
            # revoke that confirmation before any workflow can run.
            changes["identity_confirmed"] = False
            changes["enabled"] = False
        future_identity = changes.get("identity_confirmed", store.identity_confirmed)
        future_seller = changes.get("expected_seller_id", store.expected_seller_id)
        if future_identity and not future_seller:
            raise ValueError("确认身份前必须填写预期卖家 ID")
        if changes.get("selector_type", store.selector_type) not in {"oauth", "id"}:
            raise ValueError("selector_type 只能是 oauth 或 id")
        for key, value in changes.items():
            setattr(store, key, value.strip() if isinstance(value, str) else value)
        self.session.flush()
        return store

    def upsert_profiles(
        self,
        profiles: Iterable[dict[str, Any]],
        *,
        account_id: int | None = None,
    ) -> tuple[int, int]:
        """Synchronise browser profiles without enabling money movement."""

        created = updated = 0
        now = utc_now()
        for profile in profiles:
            selector_type = str(profile["selector_type"])
            selector_value = str(profile["selector_value"]).strip()
            existing = self.session.scalar(
                select(Store).where(Store.selector_value == selector_value)
            )
            if existing is None:
                self.create(
                    name=str(profile.get("name") or selector_value),
                    selector_type=selector_type,
                    selector_value=selector_value,
                    browser_oauth=profile.get("browser_oauth"),
                    browser_id=profile.get("browser_id"),
                    account_id=account_id,
                )
                existing = self.session.scalar(
                    select(Store).where(Store.selector_value == selector_value)
                )
                assert existing is not None
                created += 1
            else:
                existing.name = str(profile.get("name") or existing.name)
                existing.selector_type = selector_type
                existing.browser_oauth = profile.get("browser_oauth")
                existing.browser_id = profile.get("browser_id")
                if account_id is not None:
                    existing.account_id = account_id
                updated += 1
            existing.raw_profile = dict(profile.get("raw") or {})
            existing.last_seen_at = now
        self.session.flush()
        return created, updated

    def replace_marketplaces(
        self, store_id: int, definitions: Sequence[dict[str, Any]]
    ) -> list[StoreMarketplace]:
        """Replace the complete V1 marketplace configuration for one store.

        The request is an authoritative snapshot, not a partial patch.  A code
        omitted by the caller is retained for audit/history but is explicitly
        disabled so an old site cannot remain operational by accident.
        """

        store = self.get(store_id)
        incoming: dict[str, dict[str, Any]] = {}
        for item in definitions:
            code = str(item["code"]).upper()
            if code in incoming:
                raise ValueError(f"站点 {code} 重复，每个站点只能提交一次")
            incoming[code] = item
        if not set(incoming).issubset(V1_MARKETPLACES):
            raise ValueError("V1 仅允许 CA、UK、AU")
        by_code = {item.code: item for item in store.marketplaces}
        for code, (canonical_domain, currency) in V1_MARKETPLACES.items():
            definition = incoming.get(code)
            marketplace = by_code.get(code)
            if definition is None:
                # Preserve the stable row id because historical SiteRun rows
                # may reference it, but revoke all operational state.
                if marketplace is not None:
                    marketplace.enabled = False
                continue
            domain = str(definition.get("domain") or canonical_domain).lower().strip(" /.")
            if domain != canonical_domain:
                raise ValueError(f"{code} 域名必须精确为 {canonical_domain}")
            if marketplace is None:
                marketplace = StoreMarketplace(store_id=store.id, code=code, domain=domain, currency=currency)
                self.session.add(marketplace)
            enabled = bool(definition.get("enabled", False))
            marketplace.domain = domain
            marketplace.currency = currency
            marketplace.enabled = enabled
        self.session.flush()
        # Do not return a possibly stale relationship collection from the
        # session identity map (notably after a caller rollback/retry).
        return list(
            self.session.scalars(
                select(StoreMarketplace)
                .where(StoreMarketplace.store_id == store_id)
                .order_by(StoreMarketplace.code)
            )
        )

    def reset_setup(self, store_id: int) -> tuple[Store, list[int]]:
        """Clear the store's identity baseline without deleting anything else.

        Only store identity and schedules are touched: ``expected_seller_id``,
        ``identity_confirmed``, ``enabled``, and any schedule that could still
        fire.  Marketplace rows, evidence, run history and — critically —
        ``operation_guards`` are all left exactly as they are.

        Still fail-closed around work in flight.  A queued item is checked
        independently of ``Run.status`` so an inconsistent crash snapshot cannot
        slip through.  Schedules are retained for audit and later
        reconfiguration, but disabled before identity is removed.
        """

        store = self.get(store_id)
        # Name the blockers.  A bare "there is still an unfinished task" leaves
        # the operator with nowhere to look, and the UI does not surface every
        # status; the short id is enough to find the run and cancel it.
        blockers: list[str] = []
        blockers.extend(
            f"任务 {str(run_id)[:8]}（{status}）"
            for run_id, status in self.session.execute(
                select(Run.id, Run.status)
                .where(
                    Run.store_id == store_id,
                    Run.status.in_(self.SETUP_RESET_BLOCKING_RUN_STATUSES),
                )
                .order_by(Run.id)
                .limit(10)
            )
        )
        blockers.extend(
            f"队列 {str(run_id)[:8]}（{action}/{state}）"
            for run_id, action, state in self.session.execute(
                select(RunQueueEntry.run_id, RunQueueEntry.action, RunQueueEntry.state)
                .join(Run, Run.id == RunQueueEntry.run_id)
                .where(
                    Run.store_id == store_id,
                    RunQueueEntry.state.in_(("READY", "CLAIMED")),
                )
                .order_by(RunQueueEntry.id)
                .limit(10)
            )
        )
        if blockers:
            raise ConflictError(
                "该店铺仍有未结束的任务，请先完成或取消后再重置建档："
                + "；".join(blockers)
            )

        schedule_ids = list(
            self.session.scalars(
                select(Schedule.id).where(
                    Schedule.store_id == store_id,
                    or_(Schedule.enabled.is_(True), Schedule.next_run_at.is_not(None)),
                )
            )
        )
        if schedule_ids:
            self.session.execute(
                update(Schedule)
                .where(Schedule.id.in_(schedule_ids))
                .values(enabled=False, next_run_at=None)
            )

        store.expected_seller_id = None
        store.identity_confirmed = False
        store.enabled = False
        self.session.flush()
        return store, schedule_ids


class ScheduleRepository:

    def __init__(self, session: Session, workflow_registry: Any | None = None) -> None:
        self.session = session
        self.workflow_registry = workflow_registry

    def _definition(self, workflow: str) -> Any:
        if self.workflow_registry is not None:
            try:
                return self.workflow_registry.definition(workflow)
            except LookupError as exc:
                raise ValueError("工作流不在当前版本的代码白名单中") from exc
        if workflow != "amazon_disbursement":
            raise ValueError("工作流不在当前版本的代码白名单中")
        return SimpleNamespace(
            key="amazon_disbursement",
            supported_modes=("dry_run", "approval", "auto"),
            config_version=1,
            requires_marketplace_targets=True,
            requires_confirmed_identity=True,
        )

    def _normalise_config(
        self, definition: Any, workflow: str, config: Mapping[str, Any]
    ) -> dict[str, Any]:
        if self.workflow_registry is not None:
            try:
                return dict(self.workflow_registry.validate_config(workflow, config))
            except (TypeError, ValueError) as exc:
                raise ValueError("工作流参数不符合当前版本的固定定义") from exc
        unknown = set(config) - {"marketplace_codes"}
        if unknown:
            raise ValueError("工作流参数包含未注册字段")
        del definition
        return dict(config)

    def list(self) -> list[Schedule]:
        return list(
            self.session.scalars(
                select(Schedule).options(selectinload(Schedule.store)).order_by(Schedule.id.desc())
            )
        )

    def get(self, schedule_id: int) -> Schedule:
        schedule = self.session.get(Schedule, schedule_id)
        if schedule is None:
            raise NotFoundError(f"计划 {schedule_id} 不存在")
        return schedule

    def existing_workflow_schedule_id(self, store_id: int, workflow: str) -> int | None:
        """Any rule this store already has for this workflow, paused or not.

        A paused rule is still a rule: recreating it leaves two schedules for
        one job, and the row toggle makes resuming the existing one a click.
        """

        return self.session.scalar(
            select(Schedule.id)
            .where(
                Schedule.store_id == int(store_id),
                Schedule.workflow == str(workflow),
            )
            .limit(1)
        )

    def create(self, **values: Any) -> Schedule:
        store = self.session.get(Store, values["store_id"])
        if store is None:
            raise NotFoundError("店铺不存在")
        workflow = str(values.get("workflow") or "amazon_disbursement")
        definition = self._definition(workflow)
        # Two disbursement schedules on one store cannot both win: Amazon caps
        # the account at one payout per rolling 24 hours, so the second is
        # refused every time and turns into pure noise. Workflows that do not
        # take the funds lock may legitimately have several rules.
        if bool(getattr(definition, "requires_financial_lock", False)):
            duplicate = self.existing_workflow_schedule_id(store.id, workflow)
            if duplicate is not None:
                raise ConflictError(
                    f"该店铺已有「{workflow}」排期（#{duplicate}），资金类流程每家店只允许一条；"
                    "请直接编辑或启用那一条。"
                )
        mode = str(values.get("mode") or "dry_run")
        if mode not in {str(item) for item in definition.supported_modes}:
            raise ValueError(f"工作流不支持运行模式：{mode}")
        if values.get("enabled", False):
            if bool(getattr(definition, "requires_confirmed_identity", False)) and (
                not store.identity_confirmed
                or not str(store.expected_seller_id or "").strip()
            ):
                raise ValueError("未确认身份或未绑定卖家身份，不能启用该排期")
            if not store.enabled:
                raise ValueError("店铺尚未启用，不能启用该排期")
        if (
            mode == "auto"
            and values.get("enabled", False)
            and bool(getattr(definition, "requires_confirmed_identity", False))
        ):
            if not store.identity_confirmed:
                raise ValueError("未确认身份的店铺不可设置自动模式")
            # The "two consecutive approval-mode successes" prerequisite was
            # removed on the operator's instruction.  Identity confirmation and
            # the store enable flag remain the gate for automatic mode; every
            # per-run guard (ARMED barrier, DOM contract, plan/snapshot hash,
            # payout-account baseline) is unchanged and still fail-closed.

        config = self._normalise_config(
            definition, workflow, dict(values.get("workflow_config") or {})
        )
        config_codes = config.get("marketplace_codes")
        legacy_codes = values.get("marketplace_codes")
        if (
            bool(getattr(definition, "requires_marketplace_targets", False))
            and not config_codes
            and legacy_codes
        ):
            config_codes = _normalized_codes(legacy_codes)
            config["marketplace_codes"] = config_codes
        if config_codes is not None and legacy_codes is not None:
            if _normalized_codes(config_codes) != _normalized_codes(legacy_codes):
                raise ValueError("workflow_config 与 marketplace_codes 不一致")
        codes = config_codes if config_codes is not None else (legacy_codes or [])
        normalized_codes = _normalized_codes(codes)
        if bool(getattr(definition, "requires_marketplace_targets", False)):
            self._validate_marketplace_codes(
                store.id,
                normalized_codes,
                require_payment_account=False,
            )

            config["marketplace_codes"] = normalized_codes
        values["marketplace_codes"] = normalized_codes
        values["workflow_config"] = config
        expected_version = int(getattr(definition, "config_version", 1))
        supplied_version = int(values.get("workflow_config_version", expected_version))
        if supplied_version != expected_version:
            raise ValueError("工作流配置版本与当前代码不兼容")
        values["workflow_config_version"] = expected_version
        schedule = Schedule(**values)
        self.session.add(schedule)
        self.session.flush()
        return schedule

    def update(self, schedule_id: int, **changes: Any) -> Schedule:
        schedule = self.get(schedule_id)
        definition = self._definition(schedule.workflow)
        allowed = {
            "name", "mode", "first_run_at", "interval_minutes", "timezone",
            "marketplace_codes", "workflow_config", "workflow_config_version", "enabled",
        }
        if set(changes) - allowed:
            raise ValueError("包含不允许修改的计划字段")
        expected_version = int(getattr(definition, "config_version", 1))
        config_was_changed = "workflow_config" in changes
        legacy_codes_were_changed = "marketplace_codes" in changes
        saved_version_is_current = (
            int(schedule.workflow_config_version) == expected_version
        )
        disable_only = set(changes) == {"enabled"} and changes["enabled"] is False
        # A stale schedule must always remain stoppable.  Disabling it is a
        # pure safety action, so do not force its old snapshot through the new
        # schema first.  Every other edit is an explicit migration and must
        # carry the complete current workflow_config; the legacy site list is
        # not a versioned configuration contract.
        if not saved_version_is_current and disable_only:
            schedule.enabled = False
            self.session.flush()
            return schedule
        if not saved_version_is_current and not config_was_changed:
            raise ValueError(
                "排期的工作流配置版本与当前代码不兼容，请重新保存流程参数（需完整配置）"
            )
        if (
            "workflow_config_version" in changes
            and int(changes["workflow_config_version"]) != expected_version
        ):
            raise ValueError("工作流配置版本与当前代码不兼容")
        future_config = self._normalise_config(
            definition,
            schedule.workflow,
            dict(changes.get("workflow_config", schedule.workflow_config) or {}),
        )
        config_codes = (
            future_config.get("marketplace_codes") if config_was_changed else None
        )
        changed_codes = changes.get("marketplace_codes")
        if config_was_changed and legacy_codes_were_changed:
            if _normalized_codes(config_codes) != _normalized_codes(changed_codes):
                raise ValueError("workflow_config 与 marketplace_codes 不一致")
        future_codes = (
            config_codes
            if config_was_changed
            else (
                changed_codes
                if legacy_codes_were_changed
                else future_config.get(
                    "marketplace_codes", schedule.marketplace_codes
                )
            )
        )
        future_mode = changes.get("mode", schedule.mode)
        if str(future_mode) not in {str(item) for item in definition.supported_modes}:
            raise ValueError(f"工作流不支持运行模式：{future_mode}")
        future_enabled = changes.get("enabled", schedule.enabled)
        normalized_codes = _normalized_codes(future_codes)
        codes_are_changing = config_was_changed or legacy_codes_were_changed
        if bool(getattr(definition, "requires_marketplace_targets", False)):
            # Validate the targets when they are being CHANGED, or when the rule
            # is being switched ON — never merely because a pause happened to
            # pass through here.
            #
            # Pausing used to be blocked twice over. A schedule whose site had
            # been turned off failed the enabled-site check, and a bare
            # ``{"enabled": false}`` — the one shape the repository's own
            # "must always remain stoppable" escape hatch was written for —
            # failed on 「必须明确选择至少一个站点」, because a schedule whose
            # workflow_config never carried the codes normalises to an empty
            # list here. Either way the only way to stop a running rule was to
            # delete it, which also detaches its run history.
            #
            # Only the "is this site switched on" half is relaxed for a pause.
            # An empty, unknown, duplicated or un-enrolled code is a broken rule
            # whether or not it runs, and a first attempt that skipped the whole
            # check was caught by
            # ``test_schedule_requires_an_explicit_marketplace_selection``.
            if codes_are_changing or future_enabled:
                self._validate_marketplace_codes(
                    schedule.store_id,
                    normalized_codes,
                    require_payment_account=False,
                    require_enabled_sites=bool(future_enabled),
                )
            future_config["marketplace_codes"] = normalized_codes
        if "marketplace_codes" in changes or "workflow_config" in changes:
            changes["marketplace_codes"] = normalized_codes
            changes["workflow_config"] = future_config
            changes["workflow_config_version"] = int(
                getattr(definition, "config_version", 1)
            )
        if future_enabled:
            store = self.session.get(Store, schedule.store_id)
            if not store or not store.enabled:
                raise ValueError("店铺尚未启用，不能启用该排期")
            if bool(getattr(definition, "requires_confirmed_identity", False)) and (
                not store.identity_confirmed
                or not str(store.expected_seller_id or "").strip()
            ):
                raise ValueError("卖家身份尚未绑定并确认，不能启用该排期")
        if (
            future_mode == "auto"
            and future_enabled
            and bool(getattr(definition, "requires_confirmed_identity", False))
        ):
            store = self.session.get(Store, schedule.store_id)
            if not store or not store.identity_confirmed:
                raise ValueError("未确认身份的店铺不可设置自动模式")
            # See ``create``: the two-approval prerequisite was removed on
            # purpose.  Do not reintroduce it here.
        for key, value in changes.items():
            setattr(schedule, key, value)
        self.session.flush()
        return schedule

    def _validate_marketplace_codes(
        self,
        store_id: int,
        codes: Sequence[str],
        *,
        require_payment_account: bool = True,
        require_enabled_sites: bool = True,
    ) -> None:
        """Require every scheduled code to exist and be enabled for the store.

        ``require_enabled_sites`` separates two rules that used to travel
        together.  Whether a code is well-formed, unique and configured on this
        store is about the rule itself and always applies.  Whether that site is
        switched on right now only decides if the rule may *run* — demanding it
        before allowing a pause is what made a schedule unstoppable once one of
        its sites was turned off.
        """

        normalized = [str(code).upper() for code in codes]
        if not normalized:
            raise ValueError("排期必须明确选择至少一个站点")
        unknown = set(normalized) - set(V1_MARKETPLACES)
        if unknown:
            raise ValueError(
                "计划站点只能是 CA、UK、AU；无效站点："
                + "、".join(sorted(unknown))
            )
        if len(normalized) != len(set(normalized)):
            raise ValueError("排期站点不可重复选择")

        rows = {
            row.code: row
            for row in self.session.scalars(
                select(StoreMarketplace).where(
                    StoreMarketplace.store_id == store_id,
                    StoreMarketplace.code.in_(tuple(set(normalized))),
                )
            )
        }
        missing = set(normalized) - set(rows)
        if missing:
            raise ValueError(
                "排期包含尚未在该店铺建档的站点："
                + "、".join(sorted(missing))
                + "；请先在店铺建档中配置并启用"
            )
        disabled = (
            {code for code, row in rows.items() if not row.enabled}
            if require_enabled_sites
            else set()
        )
        if disabled:
            raise ValueError(
                "排期包含该店铺尚未启用的站点："
                + "、".join(sorted(disabled))
                + "；请先在店铺建档中启用"
            )
        # A missing account tail is not a scheduling error. Seller Central may
        # legitimately hide the account-details route when the site has no
        # payment data or no payable funds. The browser workflow reads the
        # balance first and skips those sites normally; a positive submission
        # path still performs the existing fail-closed account checks later.
        del require_payment_account

    def delete(self, schedule_id: int) -> None:
        """Delete a rule without deleting or rewriting its run history.

        Historical runs are deliberately detached first because ``runs`` is
        the audit ledger and must outlive an editable scheduling rule.  An
        active run keeps its schedule link for the single-instance guard, so
        deleting the rule while one is active is rejected.
        """

        schedule = self.get(schedule_id)
        active_run_id = self.session.scalar(
            select(Run.id)
            .where(
                Run.schedule_id == schedule.id,
                Run.status.in_(ACTIVE_RUN_STATUSES),
            )
            .limit(1)
        )
        if active_run_id:
            raise ConflictError(
                f"该排期仍有未完成任务（{active_run_id}），请等待完成或取消后再删除"
            )

        # Preserve every terminal run, event, screenshot and financial guard.
        # Only the optional pointer to this now-deleted scheduling rule is
        # cleared.  The run's result_summary still contains the original rule
        # id/name snapshot where applicable.
        self.session.execute(
            update(Run)
            .where(Run.schedule_id == schedule.id)
            .values(schedule_id=None)
        )
        self.session.delete(schedule)
        self.session.flush()

    def create_run_from_schedule(
        self,
        schedule_id: int,
        *,
        require_enabled: bool,
        trigger: str,
        requested_by: str,
        scheduled_for_at: datetime | None = None,
    ) -> Run:
        """Validate a saved rule and create exactly one queued run from it.

        This is shared by clock-based execution and the administrator's
        ``run now`` action.  ``require_enabled=False`` only relaxes the timing
        switch; it does not relax store identity, enabled marketplace, workflow
        allow-list, single-instance, or automatic-mode qualification checks.
        """

        schedule = self.get(schedule_id)
        if require_enabled and not schedule.enabled:
            raise ConflictError("该排期尚未启用")

        store = self.session.get(Store, schedule.store_id)
        if store is None:
            raise NotFoundError("排期绑定的店铺不存在")
        definition = self._definition(schedule.workflow)
        expected_version = int(getattr(definition, "config_version", 1))
        if int(schedule.workflow_config_version) != expected_version:
            raise ValueError("排期的工作流配置版本与当前代码不兼容")
        run_config = self._normalise_config(
            definition,
            schedule.workflow,
            dict(schedule.workflow_config or {}),
        )
        requested_codes = set(
            run_config.get("marketplace_codes") or schedule.marketplace_codes or ()
        )
        if bool(getattr(definition, "requires_marketplace_targets", False)) and not requested_codes:
            raise ValueError("排期没有明确选择站点，暂时不能运行")
        # Revalidate at trigger time as well as create/update time. This blocks
        # legacy schedules and rows changed outside the repository from
        # reaching a browser with a disabled or account-less site.
        if bool(getattr(definition, "requires_marketplace_targets", False)):
            self._validate_marketplace_codes(
                store.id,
                tuple(requested_codes),
                require_payment_account=False,
            )

        # 这里曾经按 run 状态拦过新实例。现在没有任何状态该拦：防重复付款靠的是
        # 工作流里「每站每日一条 guard、绝不重新 arm」，不是这道闸。见文件顶部的
        # UNRESOLVED_FUNDS_RUN_STATUSES。

        run = WorkflowRepository(self.session, self.workflow_registry).create_run(
            store_id=schedule.store_id,
            schedule_id=schedule.id,
            workflow=schedule.workflow,
            mode=schedule.mode,
            trigger=trigger,
            requested_by=requested_by,
            scheduled_for_at=scheduled_for_at,
            workflow_config=run_config,
            workflow_config_version=schedule.workflow_config_version,
        )
        run.result_summary = {
            "requested_marketplaces": sorted(requested_codes),
            "schedule_id": schedule.id,
            "schedule_name": schedule.name,
            "schedule_trigger": "run_now" if trigger == "manual" else "scheduled",
            # Queue recovery must not depend on a mutable/deleted Schedule
            # relationship. These are non-business routing snapshots only.
            "schedule_batch_id": schedule.batch_id,
            "schedule_batch_order": schedule.batch_order,
            "schedule_batch_created_at": (
                schedule.batch.created_at.isoformat()
                if schedule.batch is not None and schedule.batch.created_at is not None
                else None
            ),
        }
        schedule.last_run_at = utc_now()
        self.session.flush()
        return run


class WorkflowRepository:
    """Atomic persistence contract used by all fixed-code workflows."""

    FINANCIAL_STATES = frozenset({"ARMED", "SUBMITTED", "CONFIRMED", "UNCERTAIN"})

    def __init__(self, session: Session, workflow_registry: Any | None = None) -> None:
        self.session = session
        self.workflow_registry = workflow_registry

    def _definition(self, workflow: str) -> Any:
        if self.workflow_registry is not None:
            try:
                return self.workflow_registry.definition(workflow)
            except LookupError as exc:
                raise ValueError("工作流不在当前版本的代码白名单中") from exc
        if workflow != "amazon_disbursement":
            raise ValueError("工作流不在当前版本的代码白名单中")
        return SimpleNamespace(
            supported_modes=("dry_run", "approval", "auto"),
            config_version=1,
            requires_confirmed_identity=True,
        )

    def create_run(
        self,
        *,
        store_id: int,
        workflow: str,
        mode: str,
        schedule_id: int | None = None,
        trigger: str = "manual",
        requested_by: str = "admin",
        scheduled_for_at: datetime | None = None,
        workflow_config: Mapping[str, Any] | None = None,
        workflow_config_version: int = 1,
    ) -> Run:
        store = self.session.get(Store, store_id)
        if store is None:
            raise NotFoundError("店铺不存在")
        definition = self._definition(workflow)
        if mode not in {str(item) for item in definition.supported_modes}:
            raise ValueError(f"工作流不支持运行模式：{mode}")
        expected_version = int(getattr(definition, "config_version", 1))
        if int(workflow_config_version) != expected_version:
            raise ValueError("工作流配置版本与当前代码不兼容")
        config = dict(workflow_config or {})
        if self.workflow_registry is not None:
            try:
                config = dict(self.workflow_registry.validate_config(workflow, config))
            except (TypeError, ValueError) as exc:
                raise ValueError("工作流参数不符合当前版本的固定定义") from exc
        if bool(getattr(definition, "requires_confirmed_identity", False)) and (
            not store.identity_confirmed
            or not (store.expected_seller_id or "").strip()
        ):
            raise ValueError("未绑定并确认卖家身份，禁止创建该流程任务")
        if not store.enabled:
            raise ValueError("店铺尚未启用")
        # An ``auto`` run no longer requires two prior approval-mode successes;
        # that prerequisite was removed on the operator's instruction.  The
        # identity/enable checks above and every per-run financial guard below
        # are untouched.
        # Manual "run now" keeps the administrator-facing single-instance
        # guard.  A clock occurrence is instead identified by its immutable
        # ``scheduled_for_at`` value, so a slow previous occurrence must not
        # make the next day's durable queue item disappear.
        if schedule_id is not None and scheduled_for_at is None:
            active = self.session.scalar(
                select(Run.id).where(
                    Run.schedule_id == schedule_id,
                    Run.status.in_(
                        ACTIVE_RUN_STATUSES
                    ),
                ).limit(1)
            )
            if active:
                raise ConflictError("同一计划已有未完成实例")
        run = Run(
            store_id=store_id,
            schedule_id=schedule_id,
            workflow=workflow,
            mode=mode,
            trigger=trigger,
            requested_by=requested_by,
            scheduled_for_at=scheduled_for_at,
            workflow_config=config,
            workflow_config_version=expected_version,
        )
        self.session.add(run)
        self.session.flush()
        self.append_event(run.id, "RUN_CREATED", to_status="QUEUED", message="任务已进入队列")
        return run

    def get_run(self, run_id: str, *, full: bool = False) -> Run:
        stmt: Select[tuple[Run]] = select(Run).where(Run.id == run_id)
        if full:
            stmt = stmt.options(
                selectinload(Run.store), selectinload(Run.site_runs),
                selectinload(Run.approvals), selectinload(Run.events), selectinload(Run.evidence),
            )
        run = self.session.scalar(stmt)
        if run is None:
            raise NotFoundError(f"任务 {run_id} 不存在")
        return run

    def list_runs(self, *, limit: int = 100, status: str | None = None) -> list[Run]:
        stmt = select(Run).options(selectinload(Run.store)).order_by(Run.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(Run.status == status)
        return list(self.session.scalars(stmt))

    def set_run_status(
        self,
        run_id: str,
        to_status: str,
        *,
        allowed_from: Iterable[str],
        error: str | None = None,
    ) -> bool:
        allowed = tuple(allowed_from)
        old_status = self.session.scalar(select(Run.status).where(Run.id == run_id))
        if old_status is None:
            raise NotFoundError("任务不存在")
        if old_status in {
            "SUCCEEDED",
            "PARTIAL",
            "FAILED",
            "CANCELLED",
            "SKIPPED",
        } and to_status != old_status:
            # Terminal rows are immutable even if a stale caller accidentally
            # includes the current terminal value in ``allowed_from``.  This
            # is the database-side backstop for a worker that finishes after a
            # concurrent cancellation request.
            return False
        values: dict[str, Any] = {"status": to_status, "error": error, "updated_at": utc_now()}
        if to_status == "RUNNING":
            values["started_at"] = utc_now()
        if to_status in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "NEEDS_HUMAN_AUTH", "UNCERTAIN_FINANCIAL", "SKIPPED"}:
            values["finished_at"] = utc_now()
        result = self.session.execute(
            update(Run).where(Run.id == run_id, Run.status.in_(allowed)).values(**values)
        )
        if result.rowcount != 1:
            return False
        self.append_event(run_id, "STATUS_CHANGED", from_status=old_status, to_status=to_status, message=error or "")
        self.session.flush()
        return True

    def append_event(
        self,
        run_id: str,
        event_type: str,
        *,
        site_run_id: str | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> RunEvent:
        event = RunEvent(
            run_id=run_id, site_run_id=site_run_id, event_type=event_type,
            from_status=from_status, to_status=to_status, message=message,
            details=details or {},
        )
        self.session.add(event)
        self.session.flush()
        return event

    def save_site_plan(
        self,
        *,
        run_id: str,
        marketplace_id: int,
        marketplace_code: str,
        currency: str,
        payable_amount: Decimal,
        delayed_amount: Decimal,
        settlement_key: str,
        plan_hash: str,
        snapshot_hash: str,
        details: dict[str, Any] | None = None,
    ) -> SiteRun:
        site = self.session.scalar(
            select(SiteRun).where(SiteRun.run_id == run_id, SiteRun.marketplace_id == marketplace_id)
        )
        if site is None:
            site = SiteRun(run_id=run_id, marketplace_id=marketplace_id, marketplace_code=marketplace_code)
            self.session.add(site)
        site.currency = currency
        site.payable_amount = payable_amount
        site.delayed_amount = delayed_amount
        site.settlement_key = settlement_key
        site.plan_hash = plan_hash
        site.snapshot_hash = snapshot_hash
        site.details = details or {}
        # Planning is not allowed to erase an outcome already recorded by the
        # workflow.  In particular dry_run sets DRY_RUN_COMPLETE before the
        # engine persists the plan, while zero/no-data modes set SKIPPED.
        # Only a fresh actionable site advances from its initial check state.
        if site.status in ("PENDING", "PREFLIGHT"):
            site.status = "PLANNED"
        self.session.flush()
        return site

    def create_approval(
        self,
        *,
        run_id: str,
        plan: dict[str, Any],
        plan_hash: str,
        snapshot_hash: str,
        expires_at: datetime,
    ) -> ApprovalRequest:
        self.session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.run_id == run_id, ApprovalRequest.status == "PENDING")
            .values(status="INVALIDATED", invalidated_at=utc_now(), invalid_reason="新计划替代旧计划")
        )
        approval = ApprovalRequest(
            run_id=run_id, plan_json=plan, plan_hash=plan_hash,
            snapshot_hash=snapshot_hash, expires_at=expires_at,
        )
        self.session.add(approval)
        self.session.flush()
        return approval

    def get_approval(self, approval_id: str) -> ApprovalRequest:
        approval = self.session.get(ApprovalRequest, approval_id)
        if approval is None:
            raise NotFoundError("审批请求不存在")
        return approval

    def list_pending_approvals(self) -> list[ApprovalRequest]:
        now = utc_now()
        self.session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.status == "PENDING", ApprovalRequest.expires_at <= now)
            .values(status="EXPIRED", invalidated_at=now, invalid_reason="审批已超时")
        )
        return list(
            self.session.scalars(
                select(ApprovalRequest)
                .where(ApprovalRequest.status == "PENDING")
                .options(selectinload(ApprovalRequest.run).selectinload(Run.store))
                .order_by(ApprovalRequest.created_at)
            )
        )

    def approve(self, approval_id: str, *, approved_by: str = "admin") -> ApprovalRequest:
        now = utc_now()
        result = self.session.execute(
            update(ApprovalRequest)
            .where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.status == "PENDING",
                ApprovalRequest.expires_at > now,
            )
            .values(status="APPROVED", approved_at=now, approved_by=approved_by)
        )
        if result.rowcount != 1:
            raise ConflictError("审批已处理、失效或过期")
        self.session.flush()
        return self.get_approval(approval_id)

    def invalidate_approval(self, approval_id: str, reason: str) -> bool:
        result = self.session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == approval_id, ApprovalRequest.status.in_(("PENDING", "APPROVED")))
            .values(status="INVALIDATED", invalidated_at=utc_now(), invalid_reason=reason)
        )
        return result.rowcount == 1

    def cancel_approval(self, approval_id: str) -> bool:
        result = self.session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == approval_id, ApprovalRequest.status == "PENDING")
            .values(status="CANCELLED", cancelled_at=utc_now())
        )
        return result.rowcount == 1

    def arm_operation(
        self,
        *,
        guard_key: str,
        run_id: str,
        site_run_id: str,
        store_id: int,
        workflow: str,
        marketplace_code: str,
        settlement_key: str,
        amount: Decimal,
        currency: str,
        plan_hash: str,
        snapshot_hash: str,
        payout_account_tail: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[OperationGuard, bool]:
        """Atomically create ARMED before the one allowed platform click.

        The caller must commit this transaction before clicking.  A duplicate
        operation returns the existing guard and ``False``; it is never reset.

        ``guard_key`` is the whole identity of an operation — it hashes
        workflow, store, marketplace, settlement cycle AND the operator-local
        disbursement day.  Two guards that share a settlement cycle but fall on
        different days are different operations and both are allowed; a seller
        may request a payout on several days of one open cycle.
        """

        guard = OperationGuard(
            guard_key=guard_key, run_id=run_id, site_run_id=site_run_id,
            store_id=store_id, workflow=workflow, marketplace_code=marketplace_code,
            settlement_key=settlement_key, amount=amount, currency=currency,
            plan_hash=plan_hash, snapshot_hash=snapshot_hash,
            payout_account_tail=payout_account_tail or None,
            metadata_json=metadata or {},
        )
        nested = self.session.begin_nested()
        try:
            self.session.add(guard)
            self.session.flush()
            nested.commit()
            return guard, True
        except IntegrityError:
            nested.rollback()
            # Look up by guard_key alone.  This lookup previously also matched
            # any guard sharing the settlement cycle, mirroring a cycle-wide
            # unique key that has since been removed (migration 0004) because it
            # deadlocked sites permanently.  Left in place it would be worse
            # than dead: an IntegrityError raised for some *other* reason would
            # quietly hand back a different day's guard as if it were this one.
            existing = self.session.scalar(
                select(OperationGuard).where(OperationGuard.guard_key == guard_key)
            )
            if existing is None:
                raise
            return existing, False

    def transition_guard(
        self,
        guard_id: str,
        *,
        expected_states: Iterable[str],
        to_state: str,
        failure_reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        now = utc_now()
        values: dict[str, Any] = {
            "state": to_state, "failure_reason": failure_reason,
            "last_reconciled_at": now, "updated_at": now,
        }
        if metadata is not None:
            values["metadata_json"] = metadata
        if to_state == "SUBMITTED":
            values["submitted_at"] = now
        elif to_state == "CONFIRMED":
            values["confirmed_at"] = now
        result = self.session.execute(
            update(OperationGuard)
            .where(OperationGuard.id == guard_id, OperationGuard.state.in_(tuple(expected_states)))
            .values(**values)
        )
        self.session.flush()
        return result.rowcount == 1

    def get_guard(self, guard_id: str) -> OperationGuard:
        guard = self.session.get(OperationGuard, guard_id)
        if guard is None:
            raise NotFoundError("资金操作记录不存在")
        return guard

    def list_guards(self, *, states: Iterable[str] | None = None) -> list[OperationGuard]:
        stmt = select(OperationGuard).order_by(OperationGuard.armed_at)
        if states is not None:
            stmt = stmt.where(OperationGuard.state.in_(tuple(states)))
        return list(self.session.scalars(stmt))

    def recovery_guards(self) -> list[OperationGuard]:
        return self.list_guards(states=("ARMED", "SUBMITTED", "UNCERTAIN"))

    def has_financial_guard(self, run_id: str) -> bool:
        return bool(
            self.session.scalar(
                select(func.count(OperationGuard.id)).where(
                    OperationGuard.run_id == run_id,
                    OperationGuard.state.in_(self.FINANCIAL_STATES),
                )
            )
        )

    def add_evidence(
        self,
        *,
        run_id: str,
        kind: str,
        file_path: str,
        sha256_hex: str,
        site_run_id: str | None = None,
        size_bytes: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Evidence:
        evidence = Evidence(
            run_id=run_id, site_run_id=site_run_id, kind=kind,
            file_path=file_path, sha256=sha256_hex, size_bytes=size_bytes,
            metadata_json=metadata or {},
        )
        self.session.add(evidence)
        self.session.flush()
        return evidence


class DashboardRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def summary(self) -> dict[str, int]:
        scalar = self.session.scalar
        return {
            "stores": int(scalar(select(func.count(Store.id))) or 0),
            "enabled_stores": int(scalar(select(func.count(Store.id)).where(Store.enabled.is_(True))) or 0),
            "pending_approvals": int(scalar(select(func.count(ApprovalRequest.id)).where(ApprovalRequest.status == "PENDING")) or 0),
            "active_runs": int(scalar(select(func.count(Run.id)).where(Run.status.in_(("QUEUED", "RUNNING", "WAITING_AUTH", "RECONCILING")))) or 0),
            "uncertain": int(scalar(select(func.count(OperationGuard.id)).where(OperationGuard.state == "UNCERTAIN")) or 0),
        }


def _seller_identity(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _normalized_codes(values: Sequence[Any] | Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        values = [values]
    return list(
        dict.fromkeys(
            str(code).strip().upper()
            for code in values
            if str(code).strip()
        )
    )


# ``has_auto_mode_qualification`` used to live here and required the store's
# latest two terminal disbursement runs to both be approval-mode successes
# before automatic mode could be enabled.  It was removed on the operator's
# instruction, together with its three call sites in ``ScheduleRepository``
# and ``RunRepository``.  Automatic mode is now gated only by confirmed seller
# identity and an enabled store.
