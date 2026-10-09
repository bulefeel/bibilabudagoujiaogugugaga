"""Typed onboarding configuration/resource conflict."""
class SetupConflict(RuntimeError):
    def __init__(self, message, probe_id=None):
        super().__init__(message)
        self.probe_id = probe_id
