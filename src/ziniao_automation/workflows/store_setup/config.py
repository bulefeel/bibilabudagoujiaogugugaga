"""Onboarding scope is deliberately broader than the disbursement allowlist."""
from ..amazon_disbursement.config import ALLOWED_MARKETPLACES
from ..amazon_inventory_gpsr.config import INVENTORY_MARKETPLACES

DEFAULT_SETUP_CODES = tuple(ALLOWED_MARKETPLACES)
SETUP_MARKETPLACES = {
    **ALLOWED_MARKETPLACES,
    **{code: (domain, "PLN" if code == "PL" else "EUR")
       for code, domain in INVENTORY_MARKETPLACES.items()},
}
