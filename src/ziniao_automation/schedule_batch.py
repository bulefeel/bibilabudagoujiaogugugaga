"""Transactional multi-store schedule creation.

The service is intentionally HTTP-agnostic.  Web routes validate their public
schemas, call :meth:`preview`, and only call :meth:`create` after an operator
has seen the per-store result.  A single failed target prevents every write.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Schedule, ScheduleBatch, Store
from .repositories import ConflictError, ScheduleRepository, canonical_hash




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
        # 一个店铺不合格不再拖垮整批：能建的建，建不了的自动摘掉。操作员勾选十家、
        # 其中两家还没建档时，原来的 all-or-nothing 会写零条，逼他回去一个个取消勾选。
        # 预检会把每一家的去向逐条列出来，确认之前看得见，所以不算「悄悄少建」。
        eligible_count = sum(1 for item in targets if item["eligible"])
        eligible = eligible_count > 0
        return {
            "eligible": eligible,
            "eligible_count": eligible_count,
            "created_count": eligible_count,
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
        # 跳过的目标不构成阻断：这一批仍然可以为其余店铺创建。
        # 一个店铺不合格不再拖垮整批：能建的建，建不了的自动摘掉。操作员勾选十家、
        # 其中两家还没建档时，原来的 all-or-nothing 会写零条，逼他回去一个个取消勾选。
        # 预检会把每一家的去向逐条列出来，确认之前看得见，所以不算「悄悄少建」。
        eligible_count = sum(1 for item in targets if item["eligible"])
        eligible = eligible_count > 0
        preview = {
            "eligible": eligible,
            "eligible_count": eligible_count,
            "created_count": eligible_count,
            "definition_hash": prepared.definition_hash,
            "targets": targets,
            "normalized_template": dict(prepared.template),
        }
        # 只有一条都建不出来时才拒绝——否则空批次会让「已创建」的回执骗人。
        # 两种「零条」原因不同，给的话也不同。
        if not eligible_count:
            if all(item["skipped"] for item in targets):
                raise ConflictError(
                    "所选店铺都已有同一流程的排期（含已暂停），本次没有需要创建的内容。"
                )
            raise BatchScheduleValidationError(preview)

        definition_json = {
            "store_ids": list(prepared.store_ids),
            "template": dict(prepared.template),
        }
        # 只为真正要创建的店铺建行；definition_json 保留原始请求（幂等键算的是
        # 请求本身，不是结果），schedule_count 记实际建了几条。
        create_store_ids = [
            item["store_id"] for item in targets if item["eligible"]
        ]
        batch = ScheduleBatch(
            request_id=prepared.request_id,
            definition_hash=prepared.definition_hash,
            definition_json=definition_json,
            schedule_count=len(create_store_ids),
        )

        # Batch and children stay in the same outer transaction.  Releasing a
        # SAVEPOINT before children are inserted can commit the parent row on
        # SQLite and leave an orphan if a later child fails.
        self.session.add(batch)
        self.session.flush()

        config = dict(prepared.template["workflow_config"])
        marketplace_codes = list(config.get("marketplace_codes") or ())
        schedules: list[Schedule] = []
        for order, store_id in enumerate(create_store_ids, start=1):
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
                first_run_at=datetime.fromisoformat(
                    prepared.template["first_run_at"]
                ),
                interval_minutes=prepared.template["interval_minutes"],
                timezone=prepared.template["timezone"],
                enabled=prepared.template["enabled"],
                misfire_grace_seconds=prepared.template[
                    "misfire_grace_seconds"
                ],
            )
            self.session.add(schedule)
            schedules.append(schedule)
        self.session.flush()
        return self._result(
            batch,
            schedules,
            status="created",
            skipped_count=sum(1 for item in targets if item["skipped"]),
        )

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
        first_run_at = _coerce_first_run_at(template.get("first_run_at"))
        try:
            interval_minutes = int(template.get("interval_minutes", 1440))
        except (TypeError, ValueError) as exc:
            raise ValueError("运行间隔必须是整数分钟") from exc
        if interval_minutes < 1:
            raise ValueError("运行间隔必须至少 1 分钟")
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
            # Serialised, not a datetime: this dict is hashed by canonical_hash
            # into the batch idempotency key, so it has to round-trip through
            # JSON identically on a retry.
            "first_run_at": first_run_at.isoformat(),
            "interval_minutes": interval_minutes,
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
            # 「已经有一条了」不是错误，是无事可做。放进 reasons 会让整批失败，
            # 因为 eligible 是「全体合格才创建」——所以它必须是独立的一维。
            already = (
                store is not None
                and not reasons
                and repository.existing_workflow_schedule_id(
                    store.id, prepared.template["workflow"]
                )
                is not None
            )
            targets.append(
                {
                    "store_id": store_id,
                    "store_name": store_name,
                    "eligible": not reasons and not already,
                    "skipped": bool(already),
                    "order": order,
                    "reasons": reasons,
                    "skip_reason": "该店铺已有同一流程的排期（含已暂停），本次跳过" if already else "",
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
        skipped_count: int = 0,
    ) -> dict[str, Any]:
        return {
            "status": status,
            # 让回执说实话：勾了 5 家但只建了 3 家时，操作员必须看得出另外 2 家
            # 是被跳过而不是失败了。
            "skipped_count": skipped_count,
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



def _coerce_first_run_at(value: object) -> datetime:
    """Accept a datetime or an ISO string, always return aware UTC.

    The batch template travels as JSON on the wire and is hashed into the
    idempotency key, so the same request has to normalise to the same instant
    whether it arrives parsed or as text.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("首次运行时间格式不合法") from exc
    else:
        raise ValueError("请填写首次运行时间")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    """Detach Pydantic/MutableDict values and assert JSON serialisability."""

    return json.loads(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
