"""Validated HTTP request/response schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


MarketplaceCode = Literal["CA", "UK", "AU"]
RunMode = Literal["dry_run", "approval", "auto"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StrictApiModel(ApiModel):
    """Reject silently ignored input on state-changing generic endpoints."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class MarketplaceInput(ApiModel):
    code: MarketplaceCode
    domain: str | None = None
    enabled: bool = False


class MarketplaceView(ApiModel):
    id: int
    code: str
    domain: str
    currency: str
    enabled: bool


class MarketplaceSetupInput(ApiModel):
    marketplace_codes: list[MarketplaceCode] = Field(min_length=1)

    @field_validator("marketplace_codes")
    @classmethod
    def unique_codes(cls, value: list[MarketplaceCode]) -> list[MarketplaceCode]:
        if len(value) != len(set(value)):
            raise ValueError("自动建档站点不可重复")
        return value


class FeishuSettingsInput(ApiModel):
    """What the console collects for Feishu; the secret is never echoed back.

    ``max_length`` is not cosmetic: Windows Credential Manager rejects a blob
    over roughly 2.5 KB, and the whole JSON payload shares that budget.
    """

    app_id: str = Field(min_length=1, max_length=200)
    app_secret: str = Field(min_length=1, max_length=500)
    chat_id: str = Field(min_length=1, max_length=200)


class ZiniaoSettingsInput(ApiModel):
    company: str = Field(min_length=1, max_length=200)
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=500)


class AiSettingsInput(ApiModel):
    """Cloud classifier key for the feedback removal workflow."""

    api_key: str = Field(min_length=1, max_length=500)


class FeedbackDecisionInput(ApiModel):
    """An operator choosing the removal reason the classifier declined to pick.

    Both values are re-checked against the code whitelist before use; the
    console can only ever offer what Amazon actually lists.
    """

    category: str = Field(min_length=1, max_length=40)
    reason_code: str = Field(min_length=1, max_length=8)


class StoreCreate(ApiModel):
    name: str = Field(min_length=1, max_length=160)
    selector_type: Literal["oauth", "id"]
    selector_value: str = Field(min_length=1, max_length=255)
    browser_oauth: str | None = Field(None, max_length=255)
    browser_id: str | None = Field(None, max_length=80)
    expected_seller_id: str | None = Field(None, max_length=120)


class StorePatch(ApiModel):
    name: str | None = Field(None, min_length=1, max_length=160)
    expected_seller_id: str | None = Field(None, max_length=120)
    identity_confirmed: bool | None = None
    enabled: bool | None = None
    selector_type: Literal["oauth", "id"] | None = None
    selector_value: str | None = Field(None, min_length=1, max_length=255)
    marketplaces: list[MarketplaceInput] | None = None


class StoreView(ApiModel):
    id: int
    name: str
    selector_type: str
    selector_value: str
    browser_oauth: str | None
    browser_id: str | None
    expected_seller_id: str | None
    identity_confirmed: bool
    enabled: bool
    last_seen_at: datetime | None
    marketplaces: list[MarketplaceView] = Field(default_factory=list)


class StoreSetupResetView(ApiModel):
    status: Literal["reset"] = "reset"
    store: StoreView
    disabled_schedules: int = 0


class ScheduleCreate(StrictApiModel):
    store_id: int
    name: str = Field(min_length=1, max_length=160)
    workflow: str = Field(default="amazon_disbursement", min_length=1, max_length=80)
    mode: str = Field(default="dry_run", min_length=1, max_length=24)
    # Amazon measures its once-per-24-hours payout cap from the previous
    # request, so a wall-clock schedule always falls a few seconds short. An
    # absolute anchor plus a period lets the operator pick one that clears it.
    first_run_at: datetime
    interval_minutes: int = Field(default=1440, ge=1)
    timezone: str = Field(default="Asia/Singapore", max_length=64)
    marketplace_codes: list[MarketplaceCode] = Field(default_factory=list)
    workflow_config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = False
    misfire_grace_seconds: int = Field(default=1800, ge=0, le=1800)



class SchedulePatch(StrictApiModel):
    name: str | None = Field(None, min_length=1, max_length=160)
    mode: str | None = Field(None, min_length=1, max_length=24)
    first_run_at: datetime | None = None
    interval_minutes: int | None = Field(None, ge=1)
    timezone: str | None = Field(None, max_length=64)
    marketplace_codes: list[MarketplaceCode] | None = None
    workflow_config: dict[str, Any] | None = None
    enabled: bool | None = None



class ScheduleView(ApiModel):
    id: int
    store_id: int
    name: str
    workflow: str
    mode: str
    first_run_at: datetime
    interval_minutes: int
    timezone: str
    marketplace_codes: list[str]
    workflow_config: dict[str, Any] = Field(default_factory=dict)
    workflow_config_version: int = 1
    batch_id: int | None = None
    batch_order: int | None = None
    enabled: bool
    next_run_at: datetime | None


class ScheduleMutationView(ScheduleView):
    """A durable schedule write plus its process-local projection status."""

    scheduler_refreshed: bool
    scheduler_failed_schedule_ids: list[int] | None = None
    warning: str | None = None


class ScheduleDeleteView(ApiModel):
    status: Literal["deleted"] = "deleted"
    schedule_id: int
    scheduler_refreshed: bool
    scheduler_failed_schedule_ids: list[int] | None = None
    warning: str | None = None


class RunCreate(StrictApiModel):
    store_id: int
    workflow: str = Field(default="amazon_disbursement", min_length=1, max_length=80)
    mode: str = Field(default="dry_run", min_length=1, max_length=24)
    marketplace_codes: list[MarketplaceCode] | None = None
    workflow_config: dict[str, Any] = Field(default_factory=dict)


class BatchScheduleTemplate(StrictApiModel):
    name: str = Field(min_length=1, max_length=160)
    workflow: str = Field(default="amazon_disbursement", min_length=1, max_length=80)
    mode: str = Field(default="dry_run", min_length=1, max_length=24)
    workflow_config: dict[str, Any] = Field(default_factory=dict)
    # Amazon measures its once-per-24-hours payout cap from the previous
    # request, so a wall-clock schedule always falls a few seconds short. An
    # absolute anchor plus a period lets the operator pick one that clears it.
    first_run_at: datetime
    interval_minutes: int = Field(default=1440, ge=1)
    timezone: str = Field(default="Asia/Singapore", min_length=1, max_length=64)
    enabled: bool = False
    misfire_grace_seconds: int = Field(default=1800, ge=0, le=1800)



class BatchSchedulePreviewInput(StrictApiModel):
    request_id: UUID | None = None
    store_ids: list[int] = Field(min_length=1, max_length=500)
    template: BatchScheduleTemplate

    @field_validator("store_ids")
    @classmethod
    def unique_store_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("店铺ID必须为正整数")
        if len(value) != len(set(value)):
            raise ValueError("批量选择的店铺不可重复")
        return value


class BatchScheduleCreateInput(BatchSchedulePreviewInput):
    request_id: UUID


class SiteRunView(ApiModel):
    id: str
    marketplace_code: str
    status: str
    currency: str | None
    payable_amount: Decimal | None
    delayed_amount: Decimal | None
    settlement_key: str | None
    error: str | None


class RunView(ApiModel):
    id: str
    store_id: int
    schedule_id: int | None
    workflow: str
    mode: str
    workflow_config: dict[str, Any] = Field(default_factory=dict)
    workflow_config_version: int = 1
    trigger: str
    status: str
    created_at: datetime
    scheduled_for_at: datetime | None = None
    queue_state: str | None = None
    queue_position: int | None = None
    queue_action: str | None = None
    queued_at: datetime | None = None
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    site_runs: list[SiteRunView] = Field(default_factory=list)


class ApprovalAction(ApiModel):
    approval_id: str | None = None


class LoginInput(ApiModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class BootstrapInput(ApiModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)
    confirm_password: str

    @field_validator("password")
    @classmethod
    def strong_enough(cls, value: str) -> str:
        categories = sum(
            (
                any(char.islower() for char in value),
                any(char.isupper() for char in value),
                any(char.isdigit() for char in value),
                any(not char.isalnum() for char in value),
            )
        )
        if categories < 3:
            raise ValueError("密码需包含大写、小写、数字、符号中的至少三类")
        return value

    @field_validator("confirm_password")
    @classmethod
    def matching_password(cls, value: str, info: Any) -> str:
        if info.data.get("password") != value:
            raise ValueError("两次密码不一致")
        return value
