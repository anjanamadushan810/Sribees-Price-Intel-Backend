"""SPAR becomes readable: requests are now signed with Web Bot Auth (app/adapters/botauth.py).

It stays switched off until a super admin turns it on in Sources.

Revision ID: 0002
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE competitors SET status = 'ok', notes = 'Requests signed with Web Bot Auth "
        "(spar2u.lk/agents.md); Shopify asks for a 5 s pace' WHERE code = 'spar' AND status = 'pending'"
    )


def downgrade() -> None:
    op.execute("UPDATE competitors SET status = 'pending' WHERE code = 'spar'")
