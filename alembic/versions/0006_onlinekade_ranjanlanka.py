"""Two more shops (owner, 2026-10-28): OnlineKade (WooCommerce Store API; its SKU is
the EAN barcode) and Ranjan Lanka (its own search box). Off until switched on.

Revision ID: 0006
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
INSERT INTO competitors (code, name, kind, adapter, base_url, is_enabled, status, notes) VALUES
 ('onlinekade', 'OnlineKade', 'scraped', 'woocommerce', 'https://onlinekade.lk', false, 'ok',
  'Public WooCommerce Store API; SKU = EAN barcode, so most products match by barcode'),
 ('ranjanlanka', 'Ranjan Lanka', 'scraped', 'ranjan', 'https://ranjanlanka.lk', false, 'ok',
  'Shop search box (phrase match: first two words, then the first); due products only')
ON CONFLICT (code) DO NOTHING
""")


def downgrade() -> None:
    op.execute("DELETE FROM competitors WHERE code IN ('onlinekade', 'ranjanlanka') "
               "AND NOT EXISTS (SELECT 1 FROM competitor_items ci "
               "WHERE ci.competitor_id = competitors.competitor_id)")
