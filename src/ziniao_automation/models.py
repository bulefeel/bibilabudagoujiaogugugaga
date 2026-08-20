"""SQLAlchemy models for the local SQLite source of truth."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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


class Store(TimestampMixin, Base):
    __tablename__ = "stores"

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
    runs: Mapped[list["Run"]] = relationship(back_populates="store")

    __table_args__ = (
        CheckConstraint("selector_type IN ('oauth','id')", name="ck_store_selector_type"),
        CheckConstraint(
            "enabled = 0 OR identity_confirmed = 1",
            name="ck_store_enabled_requires_identity",
        ),
    )


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
    site_runs: Mapped[list["SiteRun"]] = relationship(back_populates="marketplace")

    __table_args__ = (
        UniqueConstraint("store_id", "code", name="uq_store_marketplace_code"),
        CheckConstraint("code IN ('CA', 'UK', 'AU')", name="ck_v1_marketplace_code"),
        CheckConstraint("length(currency) = 3", name="ck_marketplace_currency"),
    )


class Schedule(TimestampMixin, Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    workflow: Mapped[str] = mapped_column(
        String(80), default="amazon_disbursement", nullable=False
    )
    mode: Mapped[str] = mapped_column(String(16), default="dry_run", nullable=False)
    local_time: Mapped[str] = mapped_column(String(5), default="09:00", nullable=False)
    days_of_week: Mapped[str] = mapped_column(String(32), default="mon,tue,wed,thu,fri")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Singapore")
    marketplace_codes: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    misfire_grace_seconds: Mapped[int] = mapped_column(Integer, default=1800, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    store: Mapped[Store] = relationship(back_populates="schedules")
    runs: Mapped[list["Run"]] = relationship(back_populates="schedule")

    __table_args__ = (
        CheckConstraint("mode IN ('dry_run','approval','auto')", name="ck_schedule_mode"),
        CheckConstraint("misfire_grace_seconds BETWEEN 0 AND 1800", name="ck_misfire_grace"),
    )


class Run(TimestampMixin, Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False, index=True
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

    store: Mapped[Store] = relationship(back_populates="runs")
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
            "enqueued_at",
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
        ForeignKey("store_marketplaces.id", ondelete="RESTRICT"), nullable=False
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
    marketplace: Mapped[StoreMarketplace] = relationship(back_populates="site_runs")
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
        ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False
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


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
