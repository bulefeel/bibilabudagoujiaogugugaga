"""Transactional multi-store schedule creation.

The service is intentionally HTTP-agnostic.  Web routes validate their public
schemas, call :meth:`preview`, and only call :meth:`create` after an operator
has seen the per-store result.  A single failed target prevents every write.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
from types import SimpleNamespace
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Schedule, ScheduleBatch, Store
from .repositories import ConflictError, ScheduleRepository, canonical_hash


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class BatchScheduleValidationError(ValueError):
    """Creation was rejected without writing a batch or child schedule."""

    def __init__(self, preview: Mapping[str, Any]) -> None:
        self.preview = dict(preview)
        super().__init__("批量排期预检未通过，请取消不符合条件的店铺后重试")


@dataclass(frozen=True, slots=True)
class _PreparedBatch:
    request_id: str | None
    store_ids: tuple[int, ...]
    definition: Any
    template: dict[str, Any]
    definition_hash: str


class BatchScheduleService:
    """Preview and atomically persist N independent store schedules.

    ``registry`` is the process code-only workflow registry.  It is optional
    only for backwards-compatible isolated tests; that fallback exposes the
    sole V1 workflow and rejects every other key.
    """

    def __init__(self, session: Session, registry: Any | None = None) -> None:
        self.session = session
        self.registry = registry

    def preview(
        self,
        *,
        request_id: str | None = None,
        store_ids: Sequence[int],
        template: Mapping[str, Any],
    ) -> dict[str, Any]:
        prepared = self._prepare(
            request_id=request_id,
            store_ids=store_ids,
            template=template,
            require_request_id=False,
        )
        targets = self._preview_targets(prepared)
        eligible = all(item["eligible"] for item in targets)
        eligible_count = sum(1 for item in targets if item["eligible"])
        return {
            "eligible": eligible,
            "eligible_count": eligible_count,
            # Creation is all-or-nothing: one invalid target means this exact
            # request will write zero schedules.
            "created_count": len(targets) if eligible else 0,
            "definition_hash": prepared.definition_hash,
            "targets": targets,
            "normalized_template": dict(prepared.template),
        }

    def create(
        self,
        *,
        request_id: str,
        store_ids: Sequence[int],
        template: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create a batch without committing the caller's transaction.

        The caller commits once, then refreshes the scheduler once.  Returning
        IDs instead of invoking the scheduler here keeps database atomicity and
        async process projection as two explicit steps.
        """

        prepared = self._prepare(
            request_id=request_id,
            store_ids=store_ids,
            template=template,
            require_request_id=True,
        )
        assert prepared.request_id is not None
        self._begin_immediate_if_available()
        existing = self._existing_result(prepared)
        if existing is not None:
            return existing

        targets = self._preview_targets(prepared)
        eligible = all(item["eligible"] for item in targets)
        eligible_count = sum(1 for item in targets if item["eligible"])
        preview = {
            "eligible": eligible,
            "eligible_count": eligible_count,
            "created_count": len(targets) if eligible else 0,
            "definition_hash": prepared.definition_hash,
            "targets": targets,
            "normalized_template": dict(prepared.template),
        }
        if not preview["eligible"]:
            raise BatchScheduleValidationError(preview)

        definition_json = {
            "store_ids": list(prepared.store_ids),
            "template": dict(prepared.template),
        }
        batch = ScheduleBatch(
            request_id=prepared.request_id,
            definition_hash=prepared.definition_hash,
            definition_json=definition_json,
            schedule_count=len(prepared.store_ids),
        )

        # Batch and children stay in the same outer transaction.  Releasing a
        # SAVEPOINT before children are inserted can commit the parent row on
        # SQLite and leave an orphan if a later child fails.
        self.session.add(batch)
        self.session.flush()

        config = dict(prepared.template["workflow_config"])
        marketplace_codes = list(config.get("marketplace_codes") or ())
        schedules: list[Schedule] = []
        for order, store_id in enumerate(prepared.store_ids, start=1):
            schedule = Schedule(
                store_id=store_id,
                batch_id=batch.id,
                batch_order=order,
                name=prepared.template["name"],
                workflow=prepared.template["workflow"],
                mode=prepared.template["mode"],
                workflow_config=_json_copy(config),
                workflow_config_version=prepared.template[
                    "workflow_config_version"
                ],
                # Keep the V1 column populated while readers migrate to the
                # versioned workflow_config snapshot.
                marketplace_codes=list(marketplace_codes),
                local_time=prepared.template["local_time"],
                days_of_week=prepared.template["days_of_week"],
                timezone=prepared.template["timezone"],
                enabled=prepared.template["enabled"],
                misfire_grace_seconds=prepared.template[
                    "misfire_grace_seconds"
                ],
            )
            self.session.add(schedule)
            schedules.append(schedule)
        self.session.flush()
        return self._result(batch, schedules, status="created")

    def _begin_immediate_if_available(self) -> None:
        """Serialize SQLite idempotency checks before the first database read.

        The HTTP route ends its authentication read transaction before calling
        ``create``.  Direct repository tests may already own a transaction; in
        that case their caller already owns the unit-of-work boundary.
        """

        bind = self.session.bind
        if (
            bind is not None
            and bind.dialect.name == "sqlite"
            and not self.session.in_transaction()
        ):
            self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")

    def _prepare(
        self,
        *,
        request_id: str | None,
        store_ids: Sequence[int],
        template: Mapping[str, Any],
        require_request_id: bool,
    ) -> _PreparedBatch:
        if require_request_id:
            normalized_request_id: str | None = str(UUID(str(request_id)))
        elif request_id:
            normalized_request_id = str(UUID(str(request_id)))
        else:
            normalized_request_id = None
        normalized_store_ids = tuple(int(value) for value in store_ids)
        if not normalized_store_ids:
            raise ValueError("请至少选择一家店铺")
        if len(normalized_store_ids) != len(set(normalized_store_ids)):
            raise ValueError("同一家店铺不可重复选择")
        if any(value <= 0 for value in normalized_store_ids):
            raise ValueError("店铺 ID 必须是正整数")

        workflow = str(template.get("workflow") or "").strip()
        definition = self._definition(workflow)
        mode = str(template.get("mode") or "").strip()
        supported_modes = tuple(
            str(item) for item in getattr(definition, "supported_modes", ())
        )
        if mode not in supported_modes:
            raise ValueError(f"工作流 {workflow} 不支持运行模式 {mode}")

        raw_config = template.get("workflow_config") or {}
        if not isinstance(raw_config, Mapping):
            raise ValueError("工作流参数必须是 JSON 对象")
        config = self._validate_config(workflow, dict(raw_config))

        name = str(template.get("name") or "").strip()
        if not name or len(name) > 160:
            raise ValueError("排期名称长度必须是 1 到 160 个字符")
        local_time = str(template.get("local_time") or "09:00")
        if not TIME_PATTERN.fullmatch(local_time):
            raise ValueError("运行时间必须使用 HH:MM 格式")
        days_of_week = _normalize_days(template.get("days_of_week"))
        timezone_name = str(template.get("timezone") or "Asia/Singapore")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"时区不存在：{timezone_name}") from exc
        misfire = int(template.get("misfire_grace_seconds", 1800))
        if not 0 <= misfire <= 1800:
            raise ValueError("补跑宽限时间必须在 0 到 1800 秒之间")

        normalized_template = {
            "name": name,
            "workflow": workflow,
            "mode": mode,
            "workflow_config": config,
            "workflow_config_version": int(
                getattr(definition, "config_version", 1)
            ),
            "local_time": local_time,
            "days_of_week": days_of_week,
            "timezone": timezone_name,
            "enabled": bool(template.get("enabled", False)),
            "misfire_grace_seconds": misfire,
        }
        fingerprint = canonical_hash(
            {
                "store_ids": list(normalized_store_ids),
                "template": normalized_template,
            }
        )
        return _PreparedBatch(
            request_id=normalized_request_id,
            store_ids=normalized_store_ids,
            definition=definition,
            template=normalized_template,
            definition_hash=fingerprint,
        )

    def _definition(self, workflow: str) -> Any:
        if self.registry is not None:
            return self.registry.definition(workflow)
        if workflow != "amazon_disbursement":
            raise ValueError("工作流不在代码白名单")
        return SimpleNamespace(
            supported_modes=("dry_run", "approval", "auto"),
            requires_marketplace_targets=True,
            requires_confirmed_identity=True,
            config_version=1,
        )

    def _validate_config(self, workflow: str, config: dict[str, Any]) -> dict[str, Any]:
        if self.registry is None:
            unknown = set(config) - {"marketplace_codes"}
            if unknown:
                raise ValueError(
                    "包含未注册的工作流参数：" + "、".join(sorted(unknown))
                )
            codes = config.get("marketplace_codes", [])
            if not isinstance(codes, list):
                raise ValueError("marketplace_codes 必须是数组")
            normalized = list(
                dict.fromkeys(str(code).upper() for code in codes if str(code).strip())
            )
            return {"marketplace_codes": normalized}

        validated = self.registry.validate_config(workflow, config)
        if hasattr(validated, "model_dump"):
            value = validated.model_dump(mode="json")
        elif isinstance(validated, Mapping):
            value = dict(validated)
        elif validated is None:
            value = config
        else:
            raise TypeError("workflow registry returned an unsupported config value")
        return _json_copy(value)

    def _preview_targets(self, prepared: _PreparedBatch) -> list[dict[str, Any]]:
        requires_identity = bool(
            getattr(prepared.definition, "requires_confirmed_identity", False)
        )
        requires_marketplaces = bool(
            getattr(prepared.definition, "requires_marketplace_targets", False)
        )
        codes = list(
            prepared.template["workflow_config"].get("marketplace_codes") or ()
        )
        targets: list[dict[str, Any]] = []
        repository = ScheduleRepository(self.session)
        for order, store_id in enumerate(prepared.store_ids, start=1):
            store = self.session.get(Store, store_id)
            reasons: list[str] = []
            if store is None:
                store_name = f"店铺 #{store_id}"
                reasons.append("店铺不存在")
            else:
                store_name = store.name
                if not store.enabled:
                    reasons.append("店铺尚未启用")
                if requires_identity and (
                    not store.identity_confirmed
                    or not str(store.expected_seller_id or "").strip()
                ):
                    reasons.append("卖家身份尚未绑定并确认")
                if requires_marketplaces:
                    try:
                        repository._validate_marketplace_codes(
                            store.id,
                            codes,
                            require_payment_account=False,
                        )
                    except (ValueError, LookupError) as exc:
                        reasons.append(str(exc))
            targets.append(
                {
                    "store_id": store_id,
                    "store_name": store_name,
                    "eligible": not reasons,
                    "order": order,
                    "reasons": reasons,
                }
            )
        return targets

    def _existing_result(self, prepared: _PreparedBatch) -> dict[str, Any] | None:
        batch = self.session.scalar(
            select(ScheduleBatch).where(
                ScheduleBatch.request_id == prepared.request_id
            )
        )
        if batch is None:
            return None
        if batch.definition_hash != prepared.definition_hash:
            raise ConflictError("request_id 已用于另一份批量排期定义")
        schedules = list(
            self.session.scalars(
                select(Schedule)
                .where(Schedule.batch_id == batch.id)
                .order_by(Schedule.batch_order, Schedule.id)
            )
        )
        return self._result(batch, schedules, status="existing")

    @staticmethod
    def _result(
        batch: ScheduleBatch,
        schedules: Sequence[Schedule],
        *,
        status: str,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "batch_id": batch.id,
            "request_id": batch.request_id,
            "definition_hash": batch.definition_hash,
            "created_count": len(schedules),
            "schedules": [
                {
                    "id": schedule.id,
                    "store_id": schedule.store_id,
                    "batch_order": schedule.batch_order,
                }
                for schedule in schedules
            ],
        }


def _normalize_days(value: object) -> str:
    if value is None:
        items = list(WEEKDAYS[:5])
    elif isinstance(value, str):
        items = [item.strip().lower() for item in value.split(",") if item.strip()]
    elif isinstance(value, Sequence):
        items = [str(item).strip().lower() for item in value if str(item).strip()]
    else:
        raise ValueError("运行日必须是星期数组或逗号分隔文本")
    if not items:
        raise ValueError("请至少选择一个运行日")
    if items == ["*"]:
        return "*"
    if "*" in items:
        raise ValueError("全天运行标记 * 不可与具体星期混用")
    unknown = set(items) - set(WEEKDAYS)
    if unknown:
        raise ValueError("运行日无效：" + "、".join(sorted(unknown)))
    if len(items) != len(set(items)):
        raise ValueError("运行日不可重复")
    selected = set(items)
    if selected == set(WEEKDAYS):
        return "*"
    return ",".join(day for day in WEEKDAYS if day in selected)


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    """Detach Pydantic/MutableDict values and assert JSON serialisability."""

    return json.loads(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
