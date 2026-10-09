"""Pure identity decisions. Values stay local; diagnostics carry counts only."""
from __future__ import annotations

from dataclasses import dataclass

from ..amazon_disbursement.page import _identity_normal
from ..amazon_switcher_contract import CURRENT_SUFFIX, LOADING_TEXT
from ..errors import PreflightRejected
from ..setup_diagnostics import SetupDiagnostic

ADAPTER_VERSION = "store-setup-readonly-v3"


class SetupRejected(PreflightRejected):
    def __init__(self, reason: str, *, count: int = 0,
                 category: str = "UNKNOWN_PAGE", stage: str = "identity_read") -> None:
        self.diagnostic = SetupDiagnostic(reason_code=reason, candidate_count=count,
                                          page_category=category, stage=stage)
        self.stop_store = reason in {"IDENTITY_AMBIGUOUS", "IDENTITY_MISMATCH",
                                     "DOMAIN_MISMATCH", "SESSION_CLOSED"}
        super().__init__(self.diagnostic.message)

    def checks_payload(self) -> dict[str, str]:
        reason = self.diagnostic.reason_code
        if reason == "SITE_ACCESS_UNPROVEN":
            identity, access, capability = "PASSED", "FAILED", "UNKNOWN"
        elif reason.startswith("IDENTITY_"):
            identity, access, capability = "FAILED", "NOT_CHECKED", "NOT_CHECKED"
        else:
            identity, access, capability = "NOT_CHECKED", "FAILED", "NOT_CHECKED"
        return {"identity_check": identity, "access_check": access,
                "payment_capability": capability, "adapter_version": ADAPTER_VERSION}


@dataclass(frozen=True)
class IdentityObservation:
    machine: tuple[tuple[str, str], ...] = ()
    visible: tuple[tuple[str, str], ...] = ()
    container_present: bool = False
    loading: bool = False

    def groups(self) -> tuple[dict[str, tuple[str, str]], ...]:
        return tuple({_identity_normal(value): (value, source) for value, source in group
                      if _identity_normal(value)} for group in (self.machine, self.visible))

    @property
    def fingerprint(self) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(sorted(group)) for group in self.groups())

    def require_consistent_with(self, other: "IdentityObservation") -> None:
        # Compare like with like. A machine-only page and a display-name-only
        # page do not by themselves prove either equality or a conflict.
        for before, after in zip(self.groups(), other.groups()):
            if before and after and set(before) != set(after):
                raise SetupRejected("IDENTITY_MISMATCH")

    def choose(self, expected: str | None = None) -> tuple[str, str]:
        groups = self.groups()
        # A machine id and an account display name are different representations,
        # not two sellers. Ambiguity WITHIN either representation remains fatal.
        for group in groups:
            if len(group) > 1:
                raise SetupRejected("IDENTITY_AMBIGUOUS", count=len(group))
        if expected is not None:
            key = _identity_normal(expected)
            for group in groups:
                if key in group:
                    return group[key]
            if any(groups):
                raise SetupRejected("IDENTITY_MISMATCH", count=sum(map(len, groups)))
        for group in groups:
            if group:
                return next(iter(group.values()))
        raise SetupRejected("IDENTITY_CURRENT_NOT_FOUND")


# Current/loading markers are shared with the observed tree contract. Unknown
# role=button and suffix-free active-account variants remain unregistered.


# One renderer snapshot instead of count/nth/attribute round trips against a
# dashboard whose header may be re-rendered between calls. The locator is the
# fixed identity contract, not document-wide HTML or application state.
DASHBOARD_IDENTITY_SNAPSHOT = r"""(elements, limit) => {
    if (elements.length > limit) return {truncated:true, items:[]};
    return {truncated:false, items:elements.map(element => {
        const style = getComputedStyle(element);
        const visible = element.getClientRects().length > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && element.getAttribute('aria-hidden') !== 'true';
        return {
            machine: ['content','data-merchant-id','data-seller-id'].map(name =>
                [String(element.getAttribute(name) || '').trim(), `dom:${name}`]),
            visible: !visible ? [] : [
                [String(element.getAttribute('label') || '').trim(), 'dom:label'],
                [String(element.getAttribute('value') || '').trim(), 'dom:value'],
                [String(element.innerText || '').trim(), 'dom:visible_account']
            ]
        };
    })};
}"""
