"""Private replay data for evidence failures; exception text stays source-free."""


class SpecificationEvidenceError(ValueError):
    def __init__(self, message, details):
        self.evidence_failure = details
        super().__init__(message)
