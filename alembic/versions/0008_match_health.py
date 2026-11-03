"""Match health (owner, 2026-11-03): notice when a shop changes or drops an item we
matched. Two new match states, both outside the one-live-match rule so their price is
never used and a replacement may be proposed:

* changed -- the shop's size or barcode moved, or the title no longer passes the brand +
  name + size rule; a person keeps it (back to confirmed) or rejects it;
* gone    -- the item stopped being listed; it comes back by itself if the shop lists it
  again.

The title, size and barcode seen when the match was made are kept to compare against.

Revision ID: 0008
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
ALTER TABLE product_matches
    ADD COLUMN seen_name       VARCHAR(300),
    ADD COLUMN seen_size_value NUMERIC(12,3),
    ADD COLUMN seen_size_unit  VARCHAR(4),
    ADD COLUMN seen_barcode    VARCHAR(20),
    ADD COLUMN misses          INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN held_status     VARCHAR(10) CHECK (held_status IN ('auto','confirmed')),
    ADD COLUMN health_note     VARCHAR(300),
    ADD COLUMN health_at       TIMESTAMPTZ;

ALTER TABLE product_matches DROP CONSTRAINT IF EXISTS product_matches_status_check;
ALTER TABLE product_matches ADD CONSTRAINT product_matches_status_check
    CHECK (status IN ('candidate','auto','confirmed','rejected','changed','gone'));
ALTER TABLE product_matches ADD CONSTRAINT product_matches_held_check
    CHECK (status NOT IN ('changed','gone') OR held_status IS NOT NULL);

-- Today's live matches start from what the shop shows now.
UPDATE product_matches m
   SET seen_name = i.name, seen_size_value = i.size_value, seen_size_unit = i.size_unit,
       seen_barcode = i.barcode
  FROM competitor_items i
 WHERE i.item_id = m.item_id AND m.status IN ('auto','confirmed');

CREATE INDEX ix_matches_health ON product_matches (status, health_at DESC)
    WHERE status IN ('changed','gone');
""")


def downgrade() -> None:
    op.execute("""
UPDATE product_matches SET status = held_status WHERE status IN ('changed','gone');
DROP INDEX IF EXISTS ix_matches_health;
ALTER TABLE product_matches DROP CONSTRAINT IF EXISTS product_matches_held_check;
ALTER TABLE product_matches DROP CONSTRAINT IF EXISTS product_matches_status_check;
ALTER TABLE product_matches ADD CONSTRAINT product_matches_status_check
    CHECK (status IN ('candidate','auto','confirmed','rejected'));
ALTER TABLE product_matches
    DROP COLUMN health_at, DROP COLUMN health_note, DROP COLUMN held_status,
    DROP COLUMN misses, DROP COLUMN seen_barcode, DROP COLUMN seen_size_unit,
    DROP COLUMN seen_size_value, DROP COLUMN seen_name;
""")
