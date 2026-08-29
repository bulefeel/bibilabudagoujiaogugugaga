"""Record whether Amazon removed the feedback itself.

Revision ID: 0009
Revises: 0008

Amazon appends its own note to feedback on orders it fulfilled:

    来自亚马逊的消息： “该商品由亚马逊配送，亚马逊对配送体验负责。”

The seller confirmed that this note marks feedback Amazon has ALREADY struck
out on its own — it handles FBA delivery complaints without being asked.  The
workflow splits the note out of the comment so the buyer's own words stand
alone, which leaves this column as the only place the fact survives.

Rows recorded before this revision carry the note inside ``comment`` and were
filed as ALREADY_REQUESTED, because at the time the two cases were not told
apart.  Both are terminal, so re-labelling them changes no behaviour — it only
stops the console claiming someone requested a review that Amazon handled.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {str(item["name"]) for item in inspector.get_columns(table_name)}


def upgrade() -> None:
    columns = _columns("feedback_reviews")
    if not columns or "amazon_removed" in columns:
        return

    op.add_column(
        "feedback_reviews",
        sa.Column(
            "amazon_removed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Earlier rows kept Amazon's note inline; recover the flag from it instead
    # of silently recording every historical entry as seller-fulfilled.
    op.get_bind().execute(
        sa.text(
            "UPDATE feedback_reviews SET amazon_removed = 1 "
            "WHERE comment LIKE '%来自亚马逊的消息%' "
            "   OR comment LIKE '%该商品由亚马逊配送%' "
            "   OR comment LIKE '%fulfilled by Amazon%'"
        )
    )
    op.get_bind().execute(
        sa.text(
            "UPDATE feedback_reviews SET state = 'AMAZON_REMOVED' "
            "WHERE amazon_removed = 1 AND state = 'ALREADY_REQUESTED'"
        )
    )


def downgrade() -> None:
    if "amazon_removed" in _columns("feedback_reviews"):
        op.drop_column("feedback_reviews", "amazon_removed")
