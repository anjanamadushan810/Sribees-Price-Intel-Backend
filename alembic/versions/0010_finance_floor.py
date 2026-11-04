"""Sribees Finance's minimum price as the floor (owner, 2026-11-03/04: costs live in
Finance; Price Intel only reads the minimum price).

* branch_products.finance_min_price -- the lowest price that keeps the owner's margin
  (Finance: latest purchase cost, transport, bags and labels, margin, cashback and
  referral), read from Online's /admin/inventory/price-floors on every catalogue sync;
* finance_floor_at -- when it was read.

When it is there, the price rule never suggests below it; the hand-typed cost (and its
min_margin_pct) is the fallback for items Finance has no floor for yet.

Revision ID: 0010
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
ALTER TABLE branch_products
    ADD COLUMN finance_min_price NUMERIC(10,2) CHECK (finance_min_price >= 0),
    ADD COLUMN finance_floor_at  TIMESTAMPTZ;
""")


def downgrade() -> None:
    op.execute("ALTER TABLE branch_products DROP COLUMN finance_floor_at, "
               "DROP COLUMN finance_min_price;")
