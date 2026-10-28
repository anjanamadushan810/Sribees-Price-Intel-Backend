"""Keells is read from its public sitemap and public product pages (rendered like a
search engine does). It stays switched off until a super admin turns it on.

Revision ID: 0003
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE competitors SET status = 'ok', notes = 'Public sitemap + public product pages "
        "rendered in a browser (prices need the page JavaScript); 5 s per page; matched "
        "products only' WHERE code = 'keells' AND status = 'pending'"
    )


def downgrade() -> None:
    op.execute("UPDATE competitors SET status = 'pending' WHERE code = 'keells'")
