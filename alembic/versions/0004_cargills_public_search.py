"""Cargills is read from its public search page, rendered in a browser. Off until a
super admin turns it on.

Revision ID: 0004
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE competitors SET status = 'ok', notes = 'Public search page /product/<words> "
        "rendered in a browser; due products only; 5 s per page' "
        "WHERE code = 'cargills' AND status = 'pending'"
    )


def downgrade() -> None:
    op.execute("UPDATE competitors SET status = 'pending' WHERE code = 'cargills'")
