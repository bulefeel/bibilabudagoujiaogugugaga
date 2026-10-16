"""SQLAlchemy models for the local SQLite source of truth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    DDL,
    event,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utc_now


def new_id() -> str:
    return str(uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ZiniaoAccount(TimestampMixin, Base):
    __tablename__ = "ziniao_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), default="紫鸟主账号")
    company: Mapped[str | None] = mapped_column(String(180))
    username: Mapped[str | None] = mapped_column(String(180))
    credential_ref: Mapped[str | None] = mapped_column(String(255), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_error: Mapped[str | None] = mapped_column(Text)

    stores: Mapped[list["Store"]] = relationship(back_populates="account")


# 紫鸟 getBrowserList 里亚马逊的 platform_id。2026-09-26 在线上库里量过:34 家亚马逊
# 店全是 1,唯一一家 TikTok Shop 是 92。官方文档只说这是「平台id」,没给对照表。
AMAZON_PLATFORM_ID = "1"
UNKNOWN_PLATFORM_LABEL = "平台未知"


@dataclass(frozen=True, slots=True)
class StorePlatform:
    """一家紫鸟店铺环境属于哪个电商平台。

    紫鸟的公司账号下混着亚马逊、沃尔玛、TikTok 店铺,而这里的三条流程只对亚马逊
    生效。同步接口给的 ``platform_id`` / ``platform_name`` 原样躺在 ``raw_profile``
    里,这个值对象只是把它读出来 —— **不另存列**:每次同步都会整体覆盖
    ``raw_profile``,单独存一份反而会过期。

    ``platform_name`` 长成「亚马逊-加拿大」「TikTok Shop-全球跨境(自运营)(新)」:
    「-」前面是平台家族,后面是紫鸟登记该环境时选的站点/业务线。亚马逊那个
    「加拿大」只是环境注册地,**不等于**这家店开通了哪些站点(北美统一账号在
    「亚马逊-美国」环境里照样能开 CA),所以站点绑定仍由建档流程自己核验。
    """

    id: str
    name: str

    @property
    def known(self) -> bool:
        return bool(self.id or self.name)

    @property
    def family(self) -> str:
        return self.name.split("-", 1)[0].strip()

    @property
    def region(self) -> str:
        head, sep, tail = self.name.partition("-")
        return tail.strip() if sep else ""

    @property
    def label(self) -> str:
        return "亚马逊" if self.is_amazon else (self.family or UNKNOWN_PLATFORM_LABEL)

    @property
    def is_amazon(self) -> bool:
        # id 是主判据;家族文案兜底,免得 id 对照表哪天变了页面上写着「亚马逊」
        # 却被当成别的平台。两个都不像亚马逊才算不是。
        return self.id == AMAZON_PLATFORM_ID or self.family.lower() in {"亚马逊", "amazon"}

    @classmethod
    def from_raw_profile(cls, raw: Any) -> "StorePlatform":
        record = raw if isinstance(raw, dict) else {}
        platform_id = record.get("platform_id")
        platform_name = record.get("platform_name")
        return cls(
            id="" if platform_id is None else str(platform_id).strip(),
            name="" if platform_name is None else str(platform_name).strip(),
        )


class StoreHistory(Base):
    """Historical identity, never a runnable store or a hidden Store row.

    Re-granted environments reuse this ID so resync cannot reset financial
    deduplication. No credentials, raw profile or enabled flag are kept here.
    """
    __tablename__ = "store_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    selector_type: Mapped[str] = mapped_column(String(12), nullable=False)
    selector_value: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    runs: Mapped[list["Run"]] = relationship(back_populates="store")
    __table_args__ = {"sqlite_autoincrement": True}


class MarketplaceHistory(Base):
    """Minimal site identity for old results, not an executable site binding."""
    __tablename__ = "marketplace_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("store_history.id", ondelete="RESTRICT"), nullable=False)
    code: Mapped[str] = mapped_column(String(2), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    site_runs: Mapped[list["SiteRun"]] = relationship(back_populates="marketplace")
    __table_args__ = {"sqlite_autoincrement": True}


class Store(TimestampMixin, Base):
    __tablename__ = "stores"

    setup_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("ziniao_accounts.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    selector_type: Mapped[str] = mapped_column(String(12), nullable=False)
    selector_value: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    browser_oauth: Mapped[str | None] = mapped_column(String(255), unique=True)
    browser_id: Mapped[str | None] = mapped_column(String(80))
    expected_seller_id: Mapped[str | None] = mapped_column(String(120))
    identity_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_profile: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )

    account: Mapped[ZiniaoAccount | None] = relationship(back_populates="stores")
    marketplaces: Mapped[list["StoreMarketplace"]] = relationship(
        back_populates="store", cascade="all, delete-orphan"
    )
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="store")
    __table_args__ = (
        CheckConstraint("selector_type IN ('oauth','id')", name="ck_store_selector_type"),
        {"sqlite_autoincrement": True},
    )

    @property
    def platform(self) -> StorePlatform:
        """紫鸟登记的平台归属,读自最近一次同步的 ``raw_profile``。"""
        return StorePlatform.from_raw_profile(self.raw_profile)


class StoreMarketplace(TimestampMixin, Base):
    __tablename__ = "store_marketplaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(2), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # No payout-account baseline lives here.  Amazon owns the destination and
    # this automation only presses Request disbursement, so a stored
    # "expected" tail could only ever refuse a payout, never redirect one.
    # The tail Amazon actually showed is recorded per payout on OperationGuard.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    store: Mapped[Store] = relationship(back_populates="marketplaces")
    __table_args__ = (
        UniqueConstraint("store_id", "code", name="uq_store_marketplace_code"),
        # 迁移 0010 把 DE 加了进来。这里**故意保留 CHECK 而不是删掉**：SQLite 改
        # CHECK 只能整表重建，今天加和以后加成本一样，但「以后再说」的结果通常是
        # 永远没有约束。白名单本身在 repositories.V1_MARKETPLACES，两处必须同步。
        CheckConstraint(
            "code IN ('CA', 'UK', 'AU', 'DE', 'IT', 'FR', 'ES', 'PL')", name="ck_v1_marketplace_code"
        ),
        CheckConstraint("length(currency) = 3", name="ck_marketplace_currency"),
        {"sqlite_autoincrement": True},
    )


class ScheduleBatch(TimestampMixin, Base):
    """Idempotency and audit envelope for one multi-store schedule request.

    The executable unit deliberately remains :class:`Schedule`: every store
    gets its own row, run history and failure boundary.  This parent only
    proves which rows were created by one operator action and makes an HTTP
    retry safe.
    """

    __tablename__ = "schedule_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    definition_json: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    schedule_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    schedules: Mapped[list["Schedule"]] = relationship(back_populates="batch")

    __table_args__ = (
        CheckConstraint("schedule_count >= 0", name="ck_schedule_batch_count"),
        UniqueConstraint("request_id", name="uq_schedule_batches_request_id"),
        Index("ix_schedule_batches_created_at", "created_at"),
    )


class Schedule(TimestampMixin, Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedule_batches.id", ondelete="SET NULL"), index=True
    )
    batch_order: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    workflow: Mapped[str] = mapped_column(
        String(80), default="amazon_disbursement", nullable=False
    )
    mode: Mapped[str] = mapped_column(String(16), default="dry_run", nullable=False)
    # Amazon caps on-demand disbursement at once per ROLLING 24 hours, counted
    # from the previous request. A daily wall-clock schedule therefore fires a
    # few seconds short of the window every time and is refused, so it only
    # succeeded every other day. An absolute anchor plus a fixed period lets the
    # operator pick a period that actually clears the window (25h walks the
    # firing time forward an hour a day, which is the point).
    interval_minutes: Mapped[int] = mapped_column(
        Integer, default=1440, server_default=text("1440"), nullable=False
    )
    first_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Not a trigger input any more — the anchor above is an absolute instant, so
    # the timer is immune to DST. Kept because notifications render a run's
    # times in its schedule's zone (notifications/database.py).
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Singapore")
    marketplace_codes: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    workflow_config: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        server_default=text("'{}'"),
        nullable=False,
    )
    workflow_config_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    misfire_grace_seconds: Mapped[int] = mapped_column(Integer, default=1800, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    store: Mapped[Store] = relationship(back_populates="schedules")
    batch: Mapped[ScheduleBatch | None] = relationship(back_populates="schedules")
    runs: Mapped[list["Run"]] = relationship(back_populates="schedule")

    __table_args__ = (
        CheckConstraint("mode IN ('dry_run','approval','auto')", name="ck_schedule_mode"),
        CheckConstraint("misfire_grace_seconds BETWEEN 0 AND 1800", name="ck_misfire_grace"),
        # Deliberately no CHECK on ``interval_minutes``. Adding one to an
        # existing database means rebuilding this table, and 0005 already
        # documented why that is the worse path here: ``runs.schedule_id``
        # references it, so a rebuild trips the foreign key. A constraint that
        # only fresh installs carry would make upgraded and new databases behave
        # differently — the exact divergence the migration tests exist to catch.
        # An unusable period is rejected by ``_validated_interval`` (raising,
        # per-row, so one bad rule cannot stop the others) and by Pydantic.
        CheckConstraint(
            "workflow_config_version >= 1", name="ck_schedule_workflow_config_version"
        ),
        CheckConstraint(
            "batch_order IS NULL OR batch_order >= 1", name="ck_schedule_batch_order"
        ),
        UniqueConstraint("batch_id", "batch_order", name="uq_schedule_batch_order"),
    )


class Run(TimestampMixin, Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("store_history.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="SET NULL"), index=True
    )
    workflow: Mapped[str] = mapped_column(String(80), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="QUEUED", nullable=False, index=True)
    requested_by: Mapped[str] = mapped_column(String(80), default="admin", nullable=False)
    # The original clock occurrence is persisted separately from created_at.
    # It is intentionally nullable for manual runs and legacy records whose
    # exact intended fire time is unknowable.
    scheduled_for_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    result_summary: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    # Immutable input snapshot.  Runtime code must not reread mutable schedule
    # configuration after a run has been created.
    workflow_config: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        server_default=text("'{}'"),
        nullable=False,
    )
    workflow_config_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )

    store: Mapped[StoreHistory] = relationship(back_populates="runs")
    schedule: Mapped[Schedule | None] = relationship(back_populates="runs")
    site_runs: Mapped[list["SiteRun"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    approvals: Mapped[list["ApprovalRequest"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    events: Mapped[list["RunEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    queue_entries: Mapped[list["RunQueueEntry"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    notification_deliveries: Mapped[list["NotificationDelivery"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("mode IN ('dry_run','approval','auto')", name="ck_run_mode"),
        CheckConstraint("trigger IN ('manual','schedule','recovery')", name="ck_run_trigger"),
        CheckConstraint(
            "workflow_config_version >= 1", name="ck_run_workflow_config_version"
        ),
        Index("ix_runs_schedule_active", "schedule_id", "status"),
        Index(
            "uq_runs_schedule_occurrence",
            "schedule_id",
            "scheduled_for_at",
            unique=True,
            sqlite_where=text(
                "schedule_id IS NOT NULL AND scheduled_for_at IS NOT NULL"
            ),
        ),
    )


class RunQueueEntry(TimestampMixin, Base):
    """Durable work item consumed by the single automation worker.

    A run may have multiple historical entries (for example START followed by
    APPROVE), but only one READY/CLAIMED entry may exist at a time.  Lower
    numeric priorities run first; ties are ordered by scheduled time and ID.
    """

    __tablename__ = "run_queue_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(24), default="START", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    # Workflow priority is copied when the entry is created so later registry
    # edits cannot reorder work already waiting in the durable queue.
    business_priority: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    target_order: Mapped[int] = mapped_column(
        Integer,
        default=2_147_483_647,
        server_default=text("2147483647"),
        nullable=False,
    )
    # Snapshot only (not a FK): keeps one batch contiguous even if two batch
    # timestamps collide or the source schedules are later deleted.
    batch_scope_id: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="READY", nullable=False)
    scheduled_for_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cross_day_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[Run] = relationship(back_populates="queue_entries")

    __table_args__ = (
        CheckConstraint(
            "action IN ('START','APPROVE','CONTINUE_AUTH','RECONCILE')",
            name="ck_run_queue_action",
        ),
        CheckConstraint("priority IN (0,10,100)", name="ck_run_queue_priority"),
        CheckConstraint(
            "business_priority >= 0", name="ck_run_queue_business_priority"
        ),
        CheckConstraint("target_order >= 1", name="ck_run_queue_target_order"),
        CheckConstraint(
            "batch_scope_id IS NULL OR batch_scope_id >= 1",
            name="ck_run_queue_batch_scope_id",
        ),
        CheckConstraint(
            "state IN ('READY','CLAIMED','DONE','CANCELLED')",
            name="ck_run_queue_state",
        ),
        Index(
            "uq_run_queue_active_run",
            "run_id",
            unique=True,
            sqlite_where=text("state IN ('READY','CLAIMED')"),
        ),
        Index(
            "ix_run_queue_ready_order",
            "state",
            "priority",
            "scheduled_for_at",
            "business_priority",
            "enqueued_at",
            "batch_scope_id",
            "target_order",
            "id",
        ),
    )


class SiteRun(TimestampMixin, Base):
    __tablename__ = "site_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    marketplace_id: Mapped[int] = mapped_column(
        ForeignKey("marketplace_history.id", ondelete="RESTRICT"), nullable=False
    )
    marketplace_code: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="PENDING", nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3))
    payable_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    delayed_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    settlement_key: Mapped[str | None] = mapped_column(String(180))
    plan_hash: Mapped[str | None] = mapped_column(String(64))
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )

    run: Mapped[Run] = relationship(back_populates="site_runs")
    marketplace: Mapped[MarketplaceHistory] = relationship(back_populates="site_runs")
    guards: Mapped[list["OperationGuard"]] = relationship(back_populates="site_run")
    notification_deliveries: Mapped[list["NotificationDelivery"]] = relationship(
        back_populates="site_run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("run_id", "marketplace_id", name="uq_run_marketplace"),
    )


class OperationGuard(TimestampMixin, Base):
    __tablename__ = "operation_guards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    guard_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    site_run_id: Mapped[str] = mapped_column(
        ForeignKey("site_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    store_id: Mapped[int] = mapped_column(
        ForeignKey("store_history.id", ondelete="RESTRICT"), nullable=False
    )
    workflow: Mapped[str] = mapped_column(String(80), nullable=False)
    marketplace_code: Mapped[str] = mapped_column(String(2), nullable=False)
    settlement_key: Mapped[str] = mapped_column(String(180), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="ARMED", nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The masked tail Amazon displayed on the confirmation page for this
    # payout.  Recorded, never compared: this automation cannot choose or edit
    # a destination, so the value is evidence rather than a control input — and
    # it is the only durable answer to "where did that transfer go".
    payout_account_tail: Mapped[str | None] = mapped_column(String(8))
    armed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON), default=dict, nullable=False
    )

    site_run: Mapped[SiteRun] = relationship(back_populates="guards")

    # Duplicate protection is carried entirely by the UNIQUE ``guard_key``
    # above.  That key hashes workflow + store + marketplace + settlement cycle
    # + the operator-local disbursement DAY, which is the invariant this system
    # actually wants: at most one payout per site per day.
    #
    # A second constraint here once enforced uniqueness per settlement CYCLE
    # instead, and that is a different, stricter rule than anyone intended — an
    # open cycle stays open for weeks and a seller may legitimately request a
    # payout on several of those days.  Worse, it deadlocked: a cycle only rolls
    # over once a payout succeeds, so any leftover guard blocked the very payout
    # that would have cleared it, and the site stayed unpayable indefinitely.
    # Removed in migration 0004; do not reintroduce a cycle-wide unique key.
    __table_args__ = (
        CheckConstraint(
            "state IN ('ARMED','SUBMITTED','CONFIRMED','UNCERTAIN','CANCELLED')",
            name="ck_guard_state",
        ),
        CheckConstraint("amount >= 0", name="ck_guard_amount"),
    )


class ApprovalRequest(TimestampMixin, Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_json: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(80))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalid_reason: Mapped[str | None] = mapped_column(Text)

    run: Mapped[Run] = relationship(back_populates="approvals")

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','APPROVED','CANCELLED','INVALIDATED','EXPIRED')",
            name="ck_approval_status",
        ),
        Index("ix_approval_pending", "status", "expires_at"),
    )


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("site_runs.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(40))
    to_status: Mapped[str | None] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    run: Mapped[Run] = relationship(back_populates="events")


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("site_runs.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )

    run: Mapped[Run] = relationship(back_populates="evidence")


class NotificationDelivery(TimestampMixin, Base):
    """Persistent delivery receipt used to suppress duplicate Feishu cards.

    No rendered payload is stored here.  That keeps arbitrary errors, tokens,
    cookies and account data out of SQLite while retaining retry/audit state.
    """

    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("site_runs.id", ondelete="CASCADE"), index=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    run: Mapped[Run] = relationship(back_populates="notification_deliveries")
    site_run: Mapped[SiteRun | None] = relationship(
        back_populates="notification_deliveries"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','SENT','FAILED')",
            name="ck_notification_delivery_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_notification_delivery_attempts"),
        Index("ix_notification_delivery_status", "status", "created_at"),
    )


class AdminCredential(Base):
    __tablename__ = "admin_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    session_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    initialized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_id: Mapped[int] = mapped_column(
        ForeignKey("admin_credentials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InventoryGpsrItem(TimestampMixin, Base):
    """Durable inventory candidate and submission intent, outside financial guards.

    Keep submission history after a run is pruned, or deleting old reports would
    silently make the same product eligible for another declaration.
    """

    __tablename__ = "inventory_gpsr_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    store_id: Mapped[int] = mapped_column(ForeignKey("store_history.id", ondelete="RESTRICT"), nullable=False)
    marketplace_code: Mapped[str] = mapped_column(String(2), nullable=False)
    sku: Mapped[str] = mapped_column(String(255), nullable=False)
    asin: Mapped[str] = mapped_column(String(10), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    reason: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    intent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("run_id", "marketplace_code", "sku", name="uq_inventory_gpsr_run_sku"),
        Index("ix_inventory_gpsr_run_order", "run_id", "marketplace_code", "page_number", "position"),
        Index("ix_inventory_gpsr_history", "store_id", "marketplace_code", "sku", "asin"),
        Index(
            "uq_inventory_gpsr_submission", "run_id", "store_id", "marketplace_code", "sku", "asin",
            unique=True, sqlite_where=text("state IN ('SUBMITTING','UNCERTAIN','CONFIRMED')"),
        ),
        CheckConstraint("marketplace_code IN ('DE','IT','FR','ES','PL')", name="ck_inventory_gpsr_marketplace"),
        CheckConstraint("page_number >= 1 AND position >= 0", name="ck_inventory_gpsr_position"),
        CheckConstraint(
            "state IN ('PENDING','ELIGIBLE','NO_VIOLATION_ENTRY','NO_ATTESTATION',"
            "'ALREADY_SUBMITTED','MISSING','FAILED','SUBMITTING','CONFIRMED','UNCERTAIN','BLOCKED')",
            name="ck_inventory_gpsr_state",
        ),
        CheckConstraint(
            "state NOT IN ('SUBMITTING','UNCERTAIN','CONFIRMED') OR intent_at IS NOT NULL",
            name="ck_inventory_gpsr_intent",
        ),
    )


class InventoryGpsrRequirement(TimestampMixin, Base):
    """One durable attempt per run/product/requirement; retain earlier attempts."""

    __tablename__ = "inventory_gpsr_requirements"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(ForeignKey("inventory_gpsr_items.id", ondelete="RESTRICT"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    store_id: Mapped[int] = mapped_column(ForeignKey("store_history.id", ondelete="RESTRICT"), nullable=False)
    marketplace_code: Mapped[str] = mapped_column(String(2), nullable=False)
    sku: Mapped[str] = mapped_column(String(255), nullable=False)
    asin: Mapped[str] = mapped_column(String(10), nullable=False)
    requirement_type: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    reason: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    initial_selected: Mapped[bool | None] = mapped_column(Boolean)
    intent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    item: Mapped[InventoryGpsrItem] = relationship(lazy="joined")
    __table_args__ = (
        UniqueConstraint("item_id", "requirement_type", name="uq_inventory_requirement_item_type"),
        CheckConstraint("marketplace_code IN ('DE','IT','FR','ES','PL')", name="ck_inventory_requirement_marketplace"),
        CheckConstraint("requirement_type IN ('safety_attestation','responsible_person','manufacturer')", name="ck_inventory_requirement_type"),
        CheckConstraint("state IN ('PENDING','ELIGIBLE','NO_VIOLATION_ENTRY','NO_ATTESTATION',"
                        "'ALREADY_SUBMITTED','ALREADY_LINKED','MULTIPLE_RECORDS','NO_RECORDS','INVALID_RECORD',"
                        "'NOT_APPLICABLE','MISSING','FAILED','SUBMITTING','CONFIRMED','UNCERTAIN','BLOCKED')",
                        name="ck_inventory_requirement_state"),
        CheckConstraint("state NOT IN ('SUBMITTING','UNCERTAIN','CONFIRMED') OR intent_at IS NOT NULL",
                        name="ck_inventory_requirement_intent"),
        Index("ix_inventory_requirement_run", "run_id", "marketplace_code"),
        Index("uq_inventory_requirement_submission", "run_id", "store_id", "marketplace_code", "sku", "asin", "requirement_type",
              unique=True, sqlite_where=text("state IN ('SUBMITTING','UNCERTAIN','CONFIRMED')")),
    )


class FeedbackReview(TimestampMixin, Base):
    """One seller-feedback entry considered for removal.

    Amazon accepts a removal request for a given feedback exactly once, so this
    table — not ``operation_guards`` — is what makes "never retry" physical.
    Feedback carries no id of its own in the DOM (every row renders with
    ``id="feedback-details"``), and Amazon files one feedback per order, so the
    order id is the natural key.
    """

    __tablename__ = "feedback_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    store_id: Mapped[int] = mapped_column(
        ForeignKey("store_history.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    marketplace_code: Mapped[str] = mapped_column(String(2), nullable=False)
    order_id: Mapped[str] = mapped_column(String(40), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    order_date: Mapped[str | None] = mapped_column(String(20))
    # Amazon struck this feedback through and explained why in its own note.
    # The note is stripped out of ``comment`` so the buyer's words stand alone,
    # which means this column is the only place the fact survives.
    amazon_removed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    # Buyer wording is kept here for the operator to review.  It must never be
    # written to the JSONL logs; see the redaction policy in the logging module.
    comment: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(40))
    reason_code: Mapped[str | None] = mapped_column(String(8))
    decision_source: Mapped[str | None] = mapped_column(String(16))
    decision_note: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )

    store: Mapped[StoreHistory] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "store_id", "marketplace_code", "order_id", name="uq_feedback_review_order"
        ),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_feedback_review_rating"),
    )


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


# Triggers cover ORM and SQL writes, keeping the minimal history identity in
# the same transaction as the live store, before a run can reference it.
HISTORY_TRIGGERS = (
    ("stores", "store_history_insert", "INSERT"),
    ("stores", "store_history_update", "UPDATE"),
    ("store_marketplaces", "marketplace_history_insert", "INSERT"),
    ("store_marketplaces", "marketplace_history_update", "UPDATE"),
)

def history_trigger_sql(table: str, name: str, action: str) -> str:
    if table == "stores":
        target, columns = "store_history", ("id", "name", "selector_type", "selector_value")
    else:
        target, columns = "marketplace_history", ("id", "store_id", "code", "domain", "currency")
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns[1:])
    values = ", ".join(f"NEW.{column}" for column in columns)
    return (
        f"CREATE TRIGGER IF NOT EXISTS {name} AFTER {action} ON {table} BEGIN "
        f"INSERT INTO {target} ({', '.join(columns)}) VALUES ({values}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}; END"
    )


for _table, _name, _action in HISTORY_TRIGGERS:
    event.listen(
        Base.metadata.tables[_table], "after_create",
        DDL(history_trigger_sql(_table, _name, _action)).execute_if(dialect="sqlite"),
    )


class StoreSetupProbe(Base):
    __tablename__ = "store_setup_probes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(36))
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_aliases: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    resource_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    setup_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    config_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    requested_marketplace_codes: Mapped[list] = mapped_column(JSON, nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(20), nullable=False)
    phase: Mapped[str] = mapped_column(String(24), nullable=False)
    resume_phase: Mapped[str | None] = mapped_column(String(24))
    current_marketplace: Mapped[str | None] = mapped_column(String(2))
    outcome: Mapped[str | None] = mapped_column(String(24))
    identity_result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(400), nullable=False)
    diagnostics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    cleanup_state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("store_id", "request_id", name="uq_setup_request"),
        Index("uq_setup_active_store", "store_id", unique=True, sqlite_where=text("lifecycle != 'FINISHED'")),
        Index("ix_setup_history", "store_id", "created_at"),
        CheckConstraint("kind IN ('unified','identity','marketplace')"),
        CheckConstraint("scope IN ('full','retry_unverified')"),
        CheckConstraint("outcome IS NULL OR outcome IN ('SUCCEEDED','PARTIAL','FAILED','NEEDS_REVIEW','CANCELLED','TIMED_OUT','INTERRUPTED')"),
        CheckConstraint("(lifecycle = 'FINISHED' AND outcome IS NOT NULL) OR (lifecycle != 'FINISHED' AND outcome IS NULL)"),
        CheckConstraint("lifecycle IN ('QUEUED','CHECKING','WAITING_AUTH','FINISHED')"),
        CheckConstraint("cleanup_state IN ('PENDING','CONFIRMED','UNCONFIRMED')"),
        CheckConstraint("setup_revision >= 0"),
    )


class StoreSetupSiteResult(Base):
    __tablename__ = "store_setup_site_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    probe_id: Mapped[str] = mapped_column(ForeignKey("store_setup_probes.id", ondelete="CASCADE"), nullable=False)
    marketplace_code: Mapped[str] = mapped_column(String(2), nullable=False)
    attempted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(16))
    identity_check: Mapped[str] = mapped_column(String(16), nullable=False, default="NOT_CHECKED")
    access_check: Mapped[str] = mapped_column(String(16), nullable=False, default="NOT_CHECKED")
    payment_capability: Mapped[str] = mapped_column(String(24), nullable=False, default="NOT_CHECKED")
    reason_code: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(400), nullable=False)
    page_category: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN_PAGE")
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_state: Mapped[str] = mapped_column(String(16), default="unavailable", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("probe_id", "marketplace_code", name="uq_setup_site"),
        CheckConstraint("marketplace_code IN ('CA','UK','AU','DE','IT','FR','ES','PL')"),
        CheckConstraint("outcome IS NULL OR outcome IN ('PASSED','BLOCKED','FAILED','NOT_RUN','CANCELLED')"),
    )


# SQLite owns the revision counter, including direct SQL and sync updates.
# Unrelated rename / task enable switches deliberately do not change it.
_SETUP_REVISION_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS setup_store_revision AFTER UPDATE OF
    selector_type, selector_value, expected_seller_id, identity_confirmed ON stores
    WHEN OLD.selector_type IS NOT NEW.selector_type OR OLD.selector_value IS NOT NEW.selector_value
      OR OLD.expected_seller_id IS NOT NEW.expected_seller_id OR OLD.identity_confirmed IS NOT NEW.identity_confirmed
    BEGIN UPDATE stores SET setup_revision = setup_revision + 1 WHERE id = NEW.id; END""",
    """CREATE TRIGGER IF NOT EXISTS setup_site_insert AFTER INSERT ON store_marketplaces
    BEGIN UPDATE stores SET setup_revision = setup_revision + 1 WHERE id = NEW.store_id; END""",
    """CREATE TRIGGER IF NOT EXISTS setup_site_delete AFTER DELETE ON store_marketplaces
    BEGIN UPDATE stores SET setup_revision = setup_revision + 1 WHERE id = OLD.store_id; END""",
    """CREATE TRIGGER IF NOT EXISTS setup_site_update AFTER UPDATE ON store_marketplaces
    WHEN OLD.code IS NOT NEW.code OR OLD.domain IS NOT NEW.domain OR OLD.currency IS NOT NEW.currency
      OR OLD.enabled IS NOT NEW.enabled OR OLD.store_id IS NOT NEW.store_id
    BEGIN UPDATE stores SET setup_revision = setup_revision + 1 WHERE id IN (OLD.store_id, NEW.store_id); END""",
)

@event.listens_for(Base.metadata, "after_create")
def _install_setup_revision_triggers(metadata, connection, **kwargs):
    if connection.dialect.name == "sqlite":
        for statement in _SETUP_REVISION_TRIGGERS:
            connection.exec_driver_sql(statement)
