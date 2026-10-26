"""Typed onboarding configuration/resource conflict."""
class SetupConflict(RuntimeError):
    def __init__(self, message, probe_id=None, *, store_id=None, code="SETUP_CONFLICT"):
        super().__init__(message)
        self.probe_id = probe_id
        self.store_id = store_id
        self.code = code
