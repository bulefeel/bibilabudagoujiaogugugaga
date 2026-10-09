"""Explicit, validated onboarding policy, loaded before creating a probe."""
from __future__ import annotations

from pathlib import Path
import tomllib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class OnboardingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    total_deadline_seconds: int = Field(gt=0, le=1800)
    candidate_ready_timeout_ms: int = Field(gt=0, le=60000)
    stable_sample_count: int = Field(ge=2, le=10)
    sample_interval_ms: int = Field(gt=0, le=10000)
    diagnostic_text_limit: int = Field(ge=64, le=1024)
    diagnostic_candidate_limit: int = Field(ge=1, le=1000)
    otp_autofill_timeout_ms: int = Field(ge=3000, le=60000)
    cleanup_grace_seconds: int = Field(gt=0, le=60)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def usable_sampling_budget(self) -> "OnboardingPolicy":
        if self.sample_interval_ms * (self.stable_sample_count - 1) >= self.candidate_ready_timeout_ms:
            raise ValueError("sampling budget cannot fit stable samples")
        if self.candidate_ready_timeout_ms >= self.total_deadline_seconds * 1000:
            raise ValueError("sampling budget must fit total deadline")
        return self

    @classmethod
    def load(cls, path: Path) -> "OnboardingPolicy":
        try:
            with path.open("rb") as handle:
                return cls.model_validate(tomllib.load(handle))
        except (OSError, ValueError, ValidationError):
            # Do not echo arbitrary TOML values, local paths or parser excerpts.
            raise ValueError("建档参数配置缺失或不合法，请检查 onboarding-policy.toml；本次建档未启动") from None
