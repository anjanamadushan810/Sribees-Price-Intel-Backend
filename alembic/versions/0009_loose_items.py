"""Bulk (loose) items (owner, 2026-11-04): a shop item sold loose is priced for an amount
(Nexmart "Green Gram - Bulk" Rs 97.50 per 100 g), not for a pack, so its price is scaled to
our pack size before any comparison.

* loose        -- the item is sold loose; size_value / size_unit is the amount its price is
                  for (none = 1 kg / 1 l of our unit);
* price_for_by -- a person set that amount by hand (Nexmart labels many 1 kg prices
                  "per 100 g"); the nightly run then never overwrites the size;
* price_for_at -- when.

Existing items are marked from their titles ("Bulk", "per 100 g", "Per kg"); "Per kg"
titles with no size get 1 kg. The next run marks the rest from the shop's pack text.

Revision ID: 0009
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
ALTER TABLE competitor_items
    ADD COLUMN loose        BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN price_for_by UUID REFERENCES users(user_id),
    ADD COLUMN price_for_at TIMESTAMPTZ;

UPDATE competitor_items SET loose = true
 WHERE name ~* '\mbulk\M'
    OR name ~* '\mper(\s*[0-9]+(\.[0-9]+)?\s*|\s+)(kgs?|grams|gms?|g|ltr|lt|l|ml)\M';

UPDATE competitor_items SET size_value = 1000, size_unit = 'g'
 WHERE loose AND size_value IS NULL AND name ~* '\mper\s+kgs?\M';
UPDATE competitor_items SET size_value = 1000, size_unit = 'ml'
 WHERE loose AND size_value IS NULL AND name ~* '\mper\s+(ltr|lt|l)\M';
""")


def downgrade() -> None:
    op.execute("""
ALTER TABLE competitor_items
    DROP COLUMN price_for_at,
    DROP COLUMN price_for_by,
    DROP COLUMN loose;
""")
