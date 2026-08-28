"""Durable per-feedback ledger.

The unique constraint on ``(store_id, marketplace_code, order_id)`` is what
makes "never retry" physical: a feedback that has been considered once can
never be inserted a second time, so no later run can submit for it again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ...models import FeedbackReview
from ...workflows.types import utc_now


# State machine for one feedback entry.
PENDING = "PENDING"          # reason decided, not yet submitted
NEEDS_HUMAN = "NEEDS_HUMAN"  # classifier declined; an operator must choose
# Amazon no longer offers 「请求审核」 on the row.  Per the seller: that means a
# review has ALREADY been requested for this feedback, or it was already
# reviewed and removed — not that Amazon provides no entry point.  Either way
# the one request this feedback gets has been used.
ALREADY_REQUESTED = "ALREADY_REQUESTED"
SUBMITTED = "SUBMITTED"      # request accepted by the page
UNCERTAIN = "UNCERTAIN"      # clicked, but the outcome could not be confirmed
FAILED = "FAILED"            # could not get as far as submitting

# States from which nothing may re-arm a submission.  ALREADY_REQUESTED belongs
# here for the same reason SUBMITTED does: Amazon has the request either way,
# so letting an operator assign a reason would only queue a doomed retry.
TERMINAL_STATES = frozenset({SUBMITTED, UNCERTAIN, ALREADY_REQUESTED})


@dataclass(frozen=True, slots=True)
class ReviewItem:
    id: str
    order_id: str
    marketplace_code: str
    rating: int
    comment: str
    category: str | None
    reason_code: str | None
    state: str


class FeedbackReviewStore:
    """SQLite-backed ledger, mirroring the repository style used elsewhere."""

    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory

    async def known_order_ids(self, store_id: int, marketplace_code: str) -> set[str]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(FeedbackReview.order_id).where(
                    FeedbackReview.store_id == store_id,
                    FeedbackReview.marketplace_code == marketplace_code,
                )
            )
            return {str(value) for value in rows}

    async def record_candidate(
        self,
        *,
        run_id: str,
        store_id: int,
        marketplace_code: str,
        order_id: str,
        rating: int,
        order_date: str | None,
        comment: str,
        state: str,
        category: str | None = None,
        reason_code: str | None = None,
        decision_source: str | None = None,
        decision_note: str | None = None,
    ) -> str | None:
        """Insert one entry.  Returns ``None`` when it already existed.

        A pre-existing row means this feedback was handled by an earlier run;
        the caller must treat that as "skip", never as "try again".
        """

        review = FeedbackReview(
            run_id=run_id,
            store_id=store_id,
            marketplace_code=marketplace_code,
            order_id=order_id,
            rating=rating,
            order_date=order_date,
            comment=comment,
            category=category,
            reason_code=reason_code,
            decision_source=decision_source,
            decision_note=decision_note,
            state=state,
        )
        with self.session_factory() as session:
            session.add(review)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                # Only a duplicate order means "an earlier run owns this".
                # A foreign-key or NOT NULL violation is a real bug, and
                # reporting it as "already handled" would make the workflow
                # silently skip entries it never actually recorded.
                if not _is_duplicate_order(exc):
                    raise
                return None
            return str(review.id)

    async def pending_for_store(
        self, store_id: int, marketplace_code: str
    ) -> list[ReviewItem]:
        """Every entry waiting to be submitted for this store and site.

        Deliberately NOT scoped to the current run.  PENDING means "a reason is
        decided, the request has not been sent" — it does not matter whether
        that reason came from an earlier dry run or from an operator on the
        Feedback page.  Scoping this to ``run_id`` stranded both: a dry run's
        entries and every manually decided one could never be picked up again,
        because the unique constraint also stops them being re-recorded.
        """

        with self.session_factory() as session:
            rows = session.scalars(
                select(FeedbackReview)
                .where(
                    FeedbackReview.store_id == store_id,
                    FeedbackReview.marketplace_code == marketplace_code,
                    FeedbackReview.state == PENDING,
                )
                .order_by(FeedbackReview.created_at)
            ).all()
            return [_to_item(row) for row in rows]

    async def counts_for_run(self, run_id: str) -> dict[str, int]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(FeedbackReview.state).where(FeedbackReview.run_id == run_id)
            ).all()
        counts: dict[str, int] = {}
        for state in rows:
            counts[str(state)] = counts.get(str(state), 0) + 1
        return counts

    async def mark(
        self,
        review_id: str,
        state: str,
        *,
        run_id: str | None = None,
        submitted_at: datetime | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.session_factory() as session:
            row = session.get(FeedbackReview, review_id)
            if row is None:
                return
            # A submitted entry is final.  Nothing may walk it back to a state
            # from which another run could pick it up again.
            if row.state in TERMINAL_STATES:
                return
            row.state = state
            # Re-attribute the entry to the run that actually acted on it, so a
            # carried-over item appears in the report of the run that sent it
            # rather than the one that merely wrote it down.
            if run_id is not None:
                row.run_id = run_id
            if submitted_at is not None:
                row.submitted_at = submitted_at
            if details:
                merged = dict(row.details or {})
                merged.update(details)
                row.details = merged
            session.commit()

    async def set_decision(
        self,
        review_id: str,
        *,
        category: str,
        reason_code: str,
        decision_source: str,
        decision_note: str | None = None,
    ) -> bool:
        with self.session_factory() as session:
            row = session.get(FeedbackReview, review_id)
            if row is None or row.state in TERMINAL_STATES:
                return False
            row.category = category
            row.reason_code = reason_code
            row.decision_source = decision_source
            row.decision_note = decision_note
            row.state = PENDING
            session.commit()
            return True

    async def list_reviews(
        self,
        *,
        store_id: int | None = None,
        state: str | None = None,
        limit: int = 500,
    ) -> Sequence[FeedbackReview]:
        with self.session_factory() as session:
            stmt = select(FeedbackReview).order_by(FeedbackReview.created_at.desc())
            if store_id is not None:
                stmt = stmt.where(FeedbackReview.store_id == store_id)
            if state:
                stmt = stmt.where(FeedbackReview.state == state)
            rows = session.scalars(stmt.limit(limit)).all()
            for row in rows:
                session.expunge(row)
            return rows


def _is_duplicate_order(exc: IntegrityError) -> bool:
    message = str(getattr(exc, "orig", exc))
    return "UNIQUE constraint failed" in message and "order_id" in message


def _to_item(row: FeedbackReview) -> ReviewItem:
    return ReviewItem(
        id=str(row.id),
        order_id=str(row.order_id),
        marketplace_code=str(row.marketplace_code),
        rating=int(row.rating),
        comment=str(row.comment or ""),
        category=row.category,
        reason_code=row.reason_code,
        state=str(row.state),
    )


__all__ = [
    "ALREADY_REQUESTED",
    "FAILED",
    "NEEDS_HUMAN",
    "PENDING",
    "SUBMITTED",
    "TERMINAL_STATES",
    "UNCERTAIN",
    "FeedbackReviewStore",
    "ReviewItem",
    "utc_now",
]
