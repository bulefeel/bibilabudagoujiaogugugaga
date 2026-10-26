"""Onboarding scope is deliberately broader than the disbursement allowlist."""
from ..amazon_disbursement.config import ALLOWED_MARKETPLACES
from ..amazon_inventory_gpsr.config import INVENTORY_MARKETPLACES

# Compatibility default for an unconfigured store; new sites require selection.
DEFAULT_SETUP_CODES = ("CA", "UK", "AU")
SETUP_MARKETPLACES = {
    **ALLOWED_MARKETPLACES,
    **{code: (domain, "PLN" if code == "PL" else "EUR")
       for code, domain in INVENTORY_MARKETPLACES.items()},
}
