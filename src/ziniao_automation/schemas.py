"""Validated HTTP request/response schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


MarketplaceCode = Literal["CA", "UK", "AU"]
RunMode = Literal["dry_run", "approval", "auto"]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


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


class ScheduleCreate(ApiModel):
    store_id: int
    name: str = Field(min_length=1, max_length=160)
    workflow: Literal["amazon_disbursement"] = "amazon_disbursement"
    mode: RunMode = "dry_run"
    local_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    days_of_week: str = "mon,tue,wed,thu,fri"
    timezone: str = Field(default="Asia/Singapore", max_length=64)
    marketplace_codes: list[MarketplaceCode] = Field(default_factory=list)
    enabled: bool = False
    misfire_grace_seconds: int = Field(default=1800, ge=0, le=1800)


class SchedulePatch(ApiModel):
    name: str | None = Field(None, min_length=1, max_length=160)
    mode: RunMode | None = None
    local_time: str | None = Field(None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    days_of_week: str | None = None
    timezone: str | None = Field(None, max_length=64)
    marketplace_codes: list[MarketplaceCode] | None = None
    enabled: bool | None = None


class ScheduleView(ApiModel):
    id: int
    store_id: int
    name: str
    workflow: str
    mode: str
    local_time: str
    days_of_week: str
    timezone: str
    marketplace_codes: list[str]
    enabled: bool
    next_run_at: datetime | None


class RunCreate(ApiModel):
    store_id: int
    workflow: Literal["amazon_disbursement"] = "amazon_disbursement"
    mode: RunMode = "dry_run"
    marketplace_codes: list[MarketplaceCode] | None = None


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
