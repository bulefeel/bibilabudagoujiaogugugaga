"""Durable onboarding facts, independent of financial runs and queues.

Short SQLite transactions only. Public projections are closed allowlists; local
selector/identity snapshots are never copied into diagnostic responses.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from hashlib import sha256
import json
import logging
from uuid import uuid4

from sqlalchemy import select, text

from .db import utc_now
from .models import Store, Run, StoreMarketplace, StoreSetupProbe as Probe, StoreSetupSiteResult as Site
from .workflows.store_setup.config import SETUP_MARKETPLACES, DEFAULT_SETUP_CODES
from .workflows.setup_diagnostics import SetupDiagnostic, REASON_MESSAGES
from .workflows.store_setup.identity import ADAPTER_VERSION

ACTIVE = ("QUEUED", "CHECKING", "WAITING_AUTH")
TERMINAL = ("SUCCEEDED", "PARTIAL", "FAILED", "NEEDS_REVIEW", "CANCELLED", "TIMED_OUT", "INTERRUPTED")
REASONS = {**REASON_MESSAGES,
    "SETUP_FAILED": "建档核验未完成，请查看已保存的分项结果",
    "SETUP_CANCELLED": "本次核验已取消",
    "SETUP_TIMED_OUT": "本次核验达到全程截止时间",
    "SETUP_INTERRUPTED": "上次验证现场已中断，请重新核验",
    "CONFIG_CHANGED": "建档配置已变化，旧结果仅作历史记录",
    "NOT_RUN": "前序步骤未完成，该站尚未核验",
    "RESOURCE_UNCONFIRMED": "流程已停止，窗口释放待确认",
}
BLOCKED = {"REGISTRATION_REQUIRED", "MARKETPLACE_UNAVAILABLE", "MARKETPLACE_NOT_OFFERED", "INACTIVE_ACCOUNT", "PERMISSION_REQUIRED"}

from .setup_errors import SetupConflict


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def snapshot(session, store):
    markets = session.scalars(select(StoreMarketplace).where(StoreMarketplace.store_id == store.id).order_by(StoreMarketplace.code)).all()
    return {"selector_type": store.selector_type, "selector_value": store.selector_value,
            "expected_seller_id": store.expected_seller_id, "identity_confirmed": store.identity_confirmed,
            "markets": [[m.code, m.domain, m.currency, m.enabled] for m in markets]}


def additive_configuration(probe, current, revision):
    """Accept only uninterrupted additions, never revive a reset or edited identity.

    SQLite increments the generation once per inserted/enabled marketplace. The
    exact delta matters: equal snapshots alone cannot detect change-then-restore.
    Historical probes and their timestamps stay immutable.
    """
    previous = probe.config_snapshot
    if any(previous.get(key) != current.get(key) for key in
           ("selector_type", "selector_value", "expected_seller_id", "identity_confirmed")):
        return False
    before = {m[0]: m for m in previous.get("markets", [])}
    after = {m[0]: m for m in current["markets"]}
    if not before.keys() <= after.keys():
        return False
    changes = len(after.keys() - before.keys())
    for code, old in before.items():
        new = after[code]
        if old == new:
            continue
        if old[:3] != new[:3] or old[3] or not new[3]:
            return False
        changes += 1
    return revision - probe.setup_revision == changes


def identity_payload(value):
    allowed = {"status", "seller_id", "source", "marketplace_code", "persistence_state", "identity_confirmed", "store_enabled", "adapter_version", "identity_source", "identity_contract_version", "candidate_count", "selection_basis", "reason_code"}
    output = {k: v for k, v in value.items() if k in allowed and (v is None or type(v) in (str, int, bool))}
    for k in list(output):
        if isinstance(output[k], str):
            output[k] = output[k][:120]
    if output.get("reason_code") not in REASONS:
        output.pop("reason_code", None)
    return output


class SetupRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    def _store(self, session, store_id):
        store = session.get(Store, int(store_id))
        if store is None:
            raise LookupError("店铺已不存在，请重新获取列表")
        return store

    def _probe(self, session, store_id, probe_id):
        row = session.get(Probe, str(probe_id))
        if row is None or row.store_id != int(store_id):
            raise LookupError("建档记录不存在或不属于该店铺")
        return row

    def existing(self, store_id, codes, kind="unified", request_id=None, scope="full", expected_revision=None):
        fingerprint = digest({"codes": list(codes), "kind": kind, "scope": scope, "expected_revision": expected_revision})
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = None
            if request_id:
                row = next((p for p in session.scalars(select(Probe).where(Probe.store_id == store_id))
                            if p.request_id == str(request_id) or str(request_id) in p.request_aliases), None)
                if row and row.request_fingerprint != fingerprint:
                    raise SetupConflict("同一请求编号对应的范围不同，请重新预览", row.id)
            if row is None:
                row = session.scalar(select(Probe).where(Probe.store_id == store_id, Probe.lifecycle.in_(ACTIVE)))
                if row and row.request_fingerprint != fingerprint:
                    raise SetupConflict("该店铺已有不同范围的核验，请完成或取消后重试", row.id)
            if row and request_id and str(request_id) != row.request_id and str(request_id) not in row.request_aliases:
                row.request_aliases = [*row.request_aliases, str(request_id)]
                session.commit()
            return {**self._view(session, row), "reused": True} if row else None

    def begin(self, store_id, codes, *, kind, seconds, policy_version, request_id=None, scope="full", expected_revision=None):
        if scope not in {"full", "retry_unverified"}:
            raise ValueError("未知核验范围")
        if kind != "identity" and (not codes or len(set(codes)) != len(codes) or set(codes) - set(SETUP_MARKETPLACES)):
            raise ValueError("请选择不重复的已支持建档站点")
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            store = self._store(session, store_id)
            if not store.platform.is_amazon:
                raise ValueError("统一建档仅处理已确认归属的亚马逊店铺，请先核实平台")
            if request_id:
                previous = next((p for p in session.scalars(select(Probe).where(Probe.store_id == store_id))
                                 if p.request_id == str(request_id) or str(request_id) in p.request_aliases), None)
                if previous:
                    if previous.request_fingerprint != digest({"codes": list(codes), "kind": kind, "scope": scope, "expected_revision": expected_revision}):
                        raise SetupConflict("同一请求编号对应不同内容", previous.id)
                    return {**self._view(session, previous), "reused": True}
            existing = session.scalar(select(Probe).where(Probe.store_id == store_id, Probe.lifecycle.in_(ACTIVE)))
            if existing:
                raise SetupConflict("该店铺已有活动核验", existing.id)
            uncertain = session.scalar(select(Probe).where(Probe.cleanup_state == "UNCONFIRMED").limit(1))
            if uncertain:
                raise SetupConflict("存在窗口释放待确认的核验，请先检查资源再继续", uncertain.id,
                                    store_id=uncertain.store_id, code="SETUP_RESOURCE_UNCONFIRMED")
            if expected_revision is not None and store.setup_revision != expected_revision:
                raise SetupConflict("配置已变化，请刷新预览")
            requested = list(codes)
            fingerprint = digest({"codes": requested, "kind": kind, "scope": scope, "expected_revision": expected_revision})
            enabled = {m[0] for m in snapshot(session, store)["markets"] if m[3]}
            if scope == "retry_unverified":
                if not set(codes) <= enabled:
                    raise SetupConflict("补测范围已不在当前所选站点中，请刷新预览")
                summary = self._summary(session, store)
                codes = [code for code in codes if summary["marketplaces"].get(code, {}).get("outcome") != "PASSED"]
                if not codes:
                    return {"status": "SUCCEEDED", "outcome": "SUCCEEDED", "no_work": True,
                            "cleanup_state": "CONFIRMED", "store_id": store_id, "marketplaces": [],
                            "message": "当前范围已通过，无需重复核验"}
            elif kind == "unified":
                for code in codes:
                    domain, currency = SETUP_MARKETPLACES[code]
                    market = session.scalar(select(StoreMarketplace).where(StoreMarketplace.store_id == store_id, StoreMarketplace.code == code))
                    if market is None:
                        session.add(StoreMarketplace(store_id=store_id, code=code, domain=domain, currency=currency, enabled=True))
                    else:
                        if (market.domain.lower(), market.currency.upper()) != (domain, currency):
                            raise SetupConflict("站点配置不符合固定域名及币种白名单")
                        market.enabled = True
                session.flush()
                session.refresh(store)
            now = utc_now()
            row = Probe(id=str(uuid4()), store_id=store_id, kind=kind, request_id=str(request_id) if request_id else None,
                request_fingerprint=fingerprint, request_aliases=[], resource_snapshot={}, setup_revision=store.setup_revision, config_snapshot=snapshot(session, store),
                requested_marketplace_codes=list(codes), scope=scope, adapter_version=ADAPTER_VERSION, policy_version=policy_version,
                lifecycle="QUEUED", phase="ACQUIRE_SESSION", identity_result={}, diagnostics={},
                message="等待获取对应紫鸟环境", cleanup_state="PENDING", created_at=now, updated_at=now,
                deadline_at=now + timedelta(seconds=seconds))
            session.add(row); session.flush()
            for code in codes:
                session.add(Site(probe_id=row.id, marketplace_code=code, domain=SETUP_MARKETPLACES[code][0],
                    adapter_version=ADAPTER_VERSION, message="等待核验"))
            session.commit()
            return self._view(session, row)

    def check(self, session, row):
        store = self._store(session, row.store_id)
        session.refresh(store)
        if row.lifecycle not in ACTIVE or store.setup_revision != row.setup_revision or snapshot(session, store) != row.config_snapshot:
            raise SetupConflict("配置或探针状态已变化，旧核验已停止", row.id)
        return store

    def identity_bootstrap_codes(self, probe_id):
        """Reuse a currently proven site for identity only, never as a new pass.

        Failed/unregistered sites may expose no current seller. Both a retry and
        an incremental full probe can use a compatible previously proven site,
        but the adapter must read and compare that identity again on the live page.
        """
        with self.sessions() as session:
            row = session.get(Probe, probe_id)
            if row is None:
                raise SetupConflict("核验记录已删除")
            store = self.check(session, row)
            requested = list(row.requested_marketplace_codes)
            summary = self._summary(session, store)
            proven = [code for code in summary["selected_marketplace_codes"]
                      if code in SETUP_MARKETPLACES and code not in requested
                      and summary["marketplaces"].get(code, {}).get("outcome") == "PASSED"]
            return proven + requested

    def assert_current(self, probe_id):
        with self.sessions() as session:
            row = session.get(Probe, probe_id)
            if row is None:
                raise SetupConflict("核验记录已删除")
            self.check(session, row)

    def bind_identity(self, probe_id, seller, selector, code):
        seller = " ".join(str(seller).split())
        if not seller or len(seller) > 120 or code not in SETUP_MARKETPLACES:
            raise SetupConflict("身份结果缺少可信证据")
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(Probe, probe_id)
            store = self.check(session, row)
            if (store.selector_type, store.selector_value) != (selector.type, selector.value):
                raise SetupConflict("紫鸟环境已变化")
            old = " ".join(str(store.expected_seller_id or "").split())
            if old and old.casefold() != seller.casefold():
                return "NEEDS_REVIEW", old, bool(store.identity_confirmed), bool(store.enabled)
            state = "MATCHED" if old and store.identity_confirmed else "PROMOTED" if old else "CREATED"
            if state != "MATCHED":
                store.expected_seller_id = old or seller
                store.identity_confirmed = True
                store.enabled = True
                session.flush(); session.refresh(store)
                row.setup_revision = store.setup_revision
                row.config_snapshot = snapshot(session, store)
            row.phase = "VERIFY_IDENTITY"; row.updated_at = utc_now()
            session.commit()
            return state, old or seller, bool(store.identity_confirmed), bool(store.enabled)

    def progress(self, probe_id, *, identity=None, sites=(), lifecycle=None, phase=None, diagnostic=None):
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(Probe, probe_id)
            if row is None or row.lifecycle == "FINISHED":
                return
            self.check(session, row)
            now = utc_now(); row.updated_at = now
            row.started_at = row.started_at or now
            if lifecycle:
                row.lifecycle = lifecycle
                if lifecycle == "WAITING_AUTH":
                    row.resume_phase = phase or row.phase
            if phase: row.phase = phase
            if identity is not None: row.identity_result = identity_payload(identity)
            if diagnostic:
                d = SetupDiagnostic(**{k: v for k, v in diagnostic.items() if k in SetupDiagnostic.__dataclass_fields__})
                row.diagnostics = d.to_payload(); row.reason_code = d.reason_code; row.message = d.message
            for value in sites:
                item = session.scalar(select(Site).where(Site.probe_id == probe_id, Site.marketplace_code == value.get("code")))
                if item is None or item.outcome is not None: continue
                status = value.get("status")
                if status in {"CHECKING", "WAITING_AUTH"}:
                    item.attempted = True; item.started_at = item.started_at or now
                    row.current_marketplace = item.marketplace_code
                reason = value.get("reason_code")
                item.reason_code = reason if reason in REASONS else None
                if status in {"SUCCEEDED", "UNAVAILABLE"}:
                    if value.get("identity_check") != "PASSED" or value.get("access_check") != "PASSED":
                        raise ValueError("A passed setup site requires verified identity and access")
                    item.attempted = True; item.outcome = "PASSED"
                elif status in {"FAILED", "NEEDS_REVIEW"}:
                    item.outcome = ("BLOCKED" if reason in BLOCKED else "FAILED") if item.attempted else "NOT_RUN"
                elif status == "CANCELLED":
                    item.outcome = "CANCELLED" if item.attempted else "NOT_RUN"
                for key in ("identity_check", "access_check", "payment_capability", "inventory_capability"):
                    val = value.get(key)
                    if val in {"PASSED", "FAILED", "NOT_CHECKED", "UNKNOWN", "SUPPORTED", "NO_PAYMENT_DATA"}:
                        setattr(item, key, val)
                if item.outcome:
                    item.reason_code = None if item.outcome == "PASSED" else reason if reason in REASONS else "NOT_RUN" if not item.attempted else "SETUP_FAILED"
                    item.message = "建档核验通过" if item.outcome == "PASSED" else REASONS[item.reason_code]
                    item.finished_at = now
                diag = value.get("diagnostics") or {}
                if diag:
                    d = SetupDiagnostic(**{k: v for k, v in diag.items() if k in SetupDiagnostic.__dataclass_fields__})
                    item.page_category = d.page_category
            session.commit()

    def finalize_phase(self, probe_id):
        with self.sessions() as session:
            row = session.get(Probe, probe_id)
            if row and row.lifecycle != "FINISHED":
                row.phase = "FINALIZE"; row.updated_at = utc_now(); session.commit()

    def finish(self, probe_id, *, outcome, cleanup, identity=None, reason=None):
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(Probe, probe_id)
            if row is None or row.lifecycle == "FINISHED": return
            now = utc_now()
            if identity is not None: row.identity_result = identity_payload(identity)
            try: self.check(session, row)
            except (SetupConflict, LookupError): outcome, reason = "INTERRUPTED", "CONFIG_CHANGED"
            sites = session.scalars(select(Site).where(Site.probe_id == probe_id)).all()
            for item in sites:
                if item.outcome is None:
                    item.outcome = "NOT_RUN" if not item.attempted else "CANCELLED" if outcome == "CANCELLED" else "FAILED"
                    item.reason_code = reason if reason in REASONS else "NOT_RUN" if not item.attempted else "SETUP_FAILED"
                    item.message = REASONS[item.reason_code]; item.finished_at = now
            if outcome not in {"CANCELLED", "TIMED_OUT", "INTERRUPTED", "NEEDS_REVIEW"} and row.kind != "identity":
                passed = sum(s.outcome == "PASSED" for s in sites)
                outcome = "SUCCEEDED" if sites and passed == len(sites) else "PARTIAL" if passed else "FAILED"
                if row.identity_result.get("status") == "NEEDS_REVIEW": outcome = "NEEDS_REVIEW"
            if reason == "IDENTITY_MISMATCH": outcome = "NEEDS_REVIEW"
            if reason in {"DOMAIN_MISMATCH", "SESSION_CLOSED", "IDENTITY_AMBIGUOUS"} and outcome not in {"CANCELLED", "TIMED_OUT", "INTERRUPTED"}:
                outcome = "FAILED"
            if row.identity_result.get("status") in {None, "PENDING", "CHECKING", "WAITING_AUTH"}:
                row.identity_result = {**row.identity_result, "status": "FAILED", "reason_code": reason if reason in REASONS else "SETUP_FAILED"}
            row.lifecycle = "FINISHED"; row.phase = "DONE"; row.outcome = outcome
            row.cleanup_state = cleanup; row.updated_at = now; row.finished_at = now
            if reason == "SETUP_FAILED":
                row.diagnostics = {}  # a stale site refusal must not mask a runtime error
            elif reason in REASON_MESSAGES and row.diagnostics.get("reason_code") != reason:
                row.diagnostics = SetupDiagnostic(reason_code=reason).to_payload()
            row.reason_code = None if outcome == "SUCCEEDED" else reason if reason in REASONS else row.reason_code or "SETUP_FAILED"
            row.message = "建档核验通过" if outcome == "SUCCEEDED" else "部分站点通过，请查看分项结果" if outcome == "PARTIAL" else REASONS[row.reason_code]
            if (row.identity_result.get("status") == "SUCCEEDED" and outcome != "SUCCEEDED"
                    and reason != "CONFIG_CHANGED"):
                row.message = "卖家身份已保存；站点核验未全部通过：" + row.message
            if cleanup == "UNCONFIRMED": row.message += "；窗口释放待确认"
            session.commit()

    def save_resource_proof(self, probe_id, proof):
        if not proof: return
        with self.sessions() as session:
            row = session.get(Probe, probe_id)
            if row and row.lifecycle in ACTIVE:
                row.resource_snapshot = proof
                session.commit()

    async def check_release(self, store_id, probe_id):
        from .setup_resources import process_released
        with self.sessions() as session:
            row = self._probe(session, store_id, probe_id)
            if row.lifecycle != "FINISHED":
                raise SetupConflict("核验仍在执行，请先取消并等待清理", row.id)
            if row.cleanup_state == "CONFIRMED":
                return self._view(session, row)
            proof = dict(row.resource_snapshot)
        released = await process_released(proof)
        with self.sessions() as session:
            row = self._probe(session, store_id, probe_id)
            if released and row.lifecycle == "FINISHED" and row.resource_snapshot == proof:
                row.cleanup_state = "CONFIRMED"
                row.message = row.message.removesuffix("；窗口释放待确认")
                row.updated_at = utc_now()
                session.commit()
            logging.getLogger(__name__).info("setup_release_check probe_id=%s process_released=%s", probe_id, released)
            return self._view(session, row)

    def _release_target(self, store_id, probe_id):
        from .ziniao.models import ProfileSelector
        with self.sessions() as session:
            row = self._probe(session, store_id, probe_id)
            store = self._store(session, store_id)
            if row.lifecycle != "FINISHED":
                raise SetupConflict("核验仍在执行，请先取消并等待清理", row.id)
            if (row.config_snapshot.get("selector_type"), row.config_snapshot.get("selector_value")) != (store.selector_type, store.selector_value):
                raise SetupConflict("该店紫鸟环境已变更，不能释放另一个环境", row.id)
            if session.scalar(select(Probe.id).where(Probe.store_id == store_id, Probe.lifecycle.in_(ACTIVE)).limit(1)):
                raise SetupConflict("该店仍有活动核验，请等待结束", row.id)
            if session.scalar(select(Run.id).where(Run.store_id == store_id,
                    Run.status.in_(("QUEUED", "RUNNING", "WAITING_AUTH", "RECONCILING"))).limit(1)):
                raise SetupConflict("该店仍有排队或执行中的业务任务，请先等待任务结束", row.id)
            return ProfileSelector(store.selector_type, store.selector_value)

    async def release_environment(self, store_id, probe_id, controller):
        """Check an exited process first, then stop only the owned idle environment."""
        current = self.get(store_id, probe_id)
        if current["cleanup_state"] == "CONFIRMED":
            return current
        selector = self._release_target(store_id, probe_id)
        current = await self.check_release(store_id, probe_id)
        if current["cleanup_state"] == "CONFIRMED":
            return current
        def validate():
            if self._release_target(store_id, probe_id) != selector:
                raise SetupConflict("环境已变化，请刷新后重试", probe_id)
        acknowledged = await controller.release_setup_store(selector, str(store_id), validate)
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = self._probe(session, store_id, probe_id)
            if acknowledged is True and row.lifecycle == "FINISHED":
                # One acknowledged stop also releases older terminal probes of
                # this exact environment. Keep their results and original times.
                for pending in session.scalars(select(Probe).where(
                        Probe.store_id == store_id, Probe.lifecycle == "FINISHED",
                        Probe.cleanup_state == "UNCONFIRMED")):
                    if (pending.config_snapshot.get("selector_type"), pending.config_snapshot.get("selector_value")) == (selector.type, selector.value):
                        pending.cleanup_state = "CONFIRMED"
                        pending.message = pending.message.removesuffix("；窗口释放待确认")
                        pending.updated_at = utc_now()
                session.commit()
            logging.getLogger(__name__).info("setup_release probe_id=%s acknowledged=%s", probe_id, acknowledged is True)
            return self._view(session, row)

    def pending_releases(self, store_ids=None):
        with self.sessions() as session:
            query = select(Probe).where(Probe.cleanup_state == "UNCONFIRMED").order_by(Probe.created_at, Probe.id)
            if store_ids is not None:
                query = query.where(Probe.store_id.in_(store_ids))
            return [(row.store_id, row.id) for row in session.scalars(query)]

    def recover(self):
        with self.sessions() as session:
            rows = [(row.id, row.lifecycle == "QUEUED" and row.phase == "ACQUIRE_SESSION"
                     and not row.resource_snapshot)
                    for row in session.scalars(select(Probe).where(Probe.lifecycle.in_(ACTIVE)))]
        for pid, not_started in rows:
            self.finish(pid, outcome="INTERRUPTED", cleanup="CONFIRMED" if not_started else "UNCONFIRMED", reason="SETUP_INTERRUPTED")
        return len(rows)

    def get(self, store_id, probe_id, kind=None):
        with self.sessions() as session:
            row = self._probe(session, store_id, probe_id)
            if kind and row.kind != kind: raise LookupError("核验类型不匹配")
            return self._view(session, row)

    def active(self, store_id):
        with self.sessions() as session:
            row = session.scalar(select(Probe).where(Probe.store_id == store_id, Probe.lifecycle.in_(ACTIVE)))
            return self._view(session, row) if row else None

    def _site_view(self, row):
        status = "SUCCEEDED" if row.outcome == "PASSED" else "CANCELLED" if row.outcome == "CANCELLED" else "FAILED" if row.outcome else "CHECKING" if row.attempted else "PENDING"
        payload = {"code": row.marketplace_code, "domain": row.domain, "currency": SETUP_MARKETPLACES[row.marketplace_code][1],
                "status": status, "outcome": row.outcome, "attempted": row.attempted,
                "identity_check": row.identity_check, "access_check": row.access_check,
                "payment_capability": row.payment_capability, "inventory_capability": row.inventory_capability,
                "reason_code": row.reason_code,
                "message": row.message, "page_category": row.page_category, "adapter_version": row.adapter_version,
                "evidence_state": row.evidence_state, "started_at": iso(row.started_at), "finished_at": iso(row.finished_at)}

        if payload["reason_code"] is None:
            payload.pop("reason_code")
        return payload

    def _view(self, session, row):
        sites = {s.marketplace_code: s for s in session.scalars(select(Site).where(Site.probe_id == row.id))}
        status = row.lifecycle if row.lifecycle != "FINISHED" else row.outcome if row.outcome not in {"TIMED_OUT", "INTERRUPTED"} else "FAILED"
        if status == "QUEUED": status = "CHECKING"
        remaining = max(0, int((row.deadline_at.replace(tzinfo=timezone.utc) - utc_now()).total_seconds()))
        identity = dict(row.identity_result)
        return {"probe_id": row.id, "store_id": row.store_id, "kind": row.kind, "status": status,
                "lifecycle": row.lifecycle, "phase": row.phase, "outcome": row.outcome, "scope": row.scope,
                "setup_revision": row.setup_revision, "identity": identity,
                "seller_id": identity.get("seller_id", ""), "source": identity.get("source"),
                "marketplace_code": identity.get("marketplace_code"), "reason_code": row.reason_code,
                "marketplaces": [self._site_view(sites[c]) for c in row.requested_marketplace_codes if c in sites],
                "cleanup_state": row.cleanup_state, "message": row.message, "last_diagnostic": dict(row.diagnostics),
                "resume_phase": row.resume_phase, "current_marketplace": row.current_marketplace,
                "created_at": iso(row.created_at), "deadline_at": iso(row.deadline_at), "updated_at": iso(row.updated_at),
                "finished_at": iso(row.finished_at), "expires_in_seconds": remaining if row.lifecycle != "FINISHED" else 0}

    def _summary(self, session, store):
        probes = session.scalars(select(Probe).where(Probe.store_id == store.id).order_by(Probe.created_at.desc(), Probe.id.desc())).all()
        current = snapshot(session, store)
        current_markets = {m[0]: m for m in current["markets"]}
        latest = {}; old_pass = {}
        for probe in probes:
            compatible = probe.adapter_version == ADAPTER_VERSION and additive_configuration(probe, current, store.setup_revision)
            previous_markets = {m[0]: m for m in probe.config_snapshot.get("markets", [])}
            for item in session.scalars(select(Site).where(Site.probe_id == probe.id)):
                if item.outcome == "PASSED" and item.marketplace_code not in old_pass: old_pass[item.marketplace_code] = iso(item.finished_at)
                if (compatible and item.marketplace_code not in latest
                        and previous_markets.get(item.marketplace_code) == current_markets.get(item.marketplace_code)):
                    latest[item.marketplace_code] = self._site_view(item)
        enabled = [m[0] for m in current["markets"] if m[3]]
        eligible = [c for c in enabled if c in SETUP_MARKETPLACES]
        passed = sum(latest.get(c, {}).get("outcome") == "PASSED" for c in eligible)
        active = next((p for p in probes if p.lifecycle in ACTIVE), None)
        state = "UNVERIFIED" if not latest else "SUCCEEDED" if eligible and passed == len(eligible) else "PARTIAL" if passed else "FAILED"
        stale = bool(probes and (not additive_configuration(probes[0], current, store.setup_revision)
                                or probes[0].adapter_version != ADAPTER_VERSION))
        if stale: state = "NEEDS_REVIEW"
        if probes and not stale and probes[0].outcome in {"CANCELLED", "TIMED_OUT", "INTERRUPTED", "NEEDS_REVIEW"}:
            state = probes[0].outcome
        if active: state = active.lifecycle
        return {"store_id": store.id, "verification_outcome": state, "setup_revision": store.setup_revision,
                "expected_seller_id": store.expected_seller_id,
                "identity_confirmed": bool(store.identity_confirmed),
                "store_enabled": bool(store.enabled),
                "configured": bool(store.expected_seller_id and store.identity_confirmed and enabled),
                "selected_marketplace_codes": enabled,
                "marketplace_config": [{"code":m.code, "domain":m.domain, "currency":m.currency, "enabled":bool(m.enabled)}
                    for m in session.scalars(select(StoreMarketplace).where(StoreMarketplace.store_id == store.id).order_by(StoreMarketplace.id))],
                "marketplaces": latest, "last_passed_at": old_pass,
                "latest_probe": self._view(session, probes[0]) if probes else None,
                "release_probe": next((self._view(session, p) for p in probes if p.cleanup_state == "UNCONFIRMED"), None),
                "active_probe": next((self._view(session,p) for p in probes if p.lifecycle in ACTIVE), None),
                "stale": stale,
                "last_checked_at": iso(probes[0].updated_at) if probes else None}

    def summary(self, store_id):
        with self.sessions() as session:
            return self._summary(session, self._store(session, store_id))

    def preview(self, store_ids, mode, selections):
        targets=[]
        for sid in store_ids:
            try:
                with self.sessions() as session:
                    store=self._store(session,sid); summary=self._summary(session,store)
                    enabled=summary["selected_marketplace_codes"]
                    codes=list(selections.get(sid, [c for c in enabled if c in SETUP_MARKETPLACES]))
                    explicit = sid in selections
                    defaulted=mode=="fill_missing" and not enabled and not explicit
                    if defaulted: codes=list(DEFAULT_SETUP_CODES)
                    reason=None
                    if not store.platform.is_amazon: reason="仅核验已确认的亚马逊店铺"
                    elif summary["active_probe"]: reason="已有活动核验"
                    elif not codes: reason="请选择要核验的站点"
                    elif len(set(codes)) != len(codes) or set(codes)-set(SETUP_MARKETPLACES): reason="包含重复或不支持的建档站点"
                    elif mode=="retry_unverified" and not set(codes)<=set(enabled): reason="请求范围不在当前所选站点中"
                    already_passed = [c for c in codes if c in enabled
                        and (mode=="retry_unverified" or (summary["identity_confirmed"] and summary["expected_seller_id"]))
                        and summary["marketplaces"].get(c,{}).get("outcome")=="PASSED"]
                    if mode in {"fill_missing", "retry_unverified"}:
                        codes=[c for c in codes if c not in already_passed]
                    if not codes and not reason: reason="所选站点均已通过；需要新站点时请在上方勾选，无需删除建档"
                    targets.append({"store_id":sid,"name":store.name,"setup_revision":store.setup_revision,
                        "marketplace_codes":codes,"scope":"retry_unverified" if mode=="retry_unverified" else "full",
                        "added_marketplace_codes":[c for c in codes if c not in enabled],
                        "preserved_marketplace_codes":[c for c in enabled if c not in codes],
                        "already_passed_marketplace_codes":already_passed if mode!="recheck" else [],
                        "eligible":reason is None,"reason":reason,"defaulted":defaulted})
            except LookupError:
                targets.append({"store_id":sid,"eligible":False,"reason":"店铺已删除","marketplace_codes":[]})
        return {"mode":mode,"targets":targets,"eligible_count":sum(t["eligible"] for t in targets)}
