"""Amazon 1-3 star feedback removal workflow plugin."""

from .config import (
    CATEGORY_LABELS,
    REASON_CATALOG,
    FeedbackDomContract,
    is_known_reason,
    reason_label,
)
from .classifier import FeedbackReasonClassifier, ReasonDecision
from .definition import AmazonFeedbackConfig, build_amazon_feedback_definition
from .page import AmazonFeedbackPage, FeedbackRow
from .review_store import FeedbackReviewStore
from .workflow import AmazonFeedbackWorkflow

__all__ = [
    "AmazonFeedbackConfig",
    "AmazonFeedbackPage",
    "AmazonFeedbackWorkflow",
    "CATEGORY_LABELS",
    "FeedbackDomContract",
    "FeedbackReasonClassifier",
    "FeedbackReviewStore",
    "FeedbackRow",
    "REASON_CATALOG",
    "ReasonDecision",
    "build_amazon_feedback_definition",
    "is_known_reason",
    "reason_label",
]
