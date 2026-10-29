"""Photos (owner, 2026-10-29): our product's photo and the shop's, side by side in the
match queue, so a match is decided at a glance.

Revision ID: 0007
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS image_url VARCHAR(500)")
    op.execute("ALTER TABLE competitor_items ADD COLUMN IF NOT EXISTS image_url VARCHAR(500)")


def downgrade() -> None:
    op.execute("ALTER TABLE competitor_items DROP COLUMN IF EXISTS image_url")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS image_url")
