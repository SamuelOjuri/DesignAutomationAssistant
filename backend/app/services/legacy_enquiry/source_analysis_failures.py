SOURCE_ANALYSIS_REVIEW_KEY = "sourceAnalysisReview"


class SourceAnalysisReviewRequired(ValueError):
    """An analysis must wait for source changes or operator review."""

    def __init__(self, message: str, *, code: str, **details):
        # Callers supply application-owned codes, source IDs and counts only.
        self.details = {"code": code, **details}
        super().__init__(message)
