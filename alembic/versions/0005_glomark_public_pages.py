"""Glomark now serves its pages (it answered only a bot challenge on 2026-10-27), and
its product pages carry schema.org JSON-LD: read it from the public sitemap and pages.
Off until a super admin turns it on.

Revision ID: 0005
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE competitors SET kind = 'scraped', adapter = 'glomark', status = 'ok', "
        "base_url = 'https://glomark.lk', is_enabled = false, "
        "notes = 'Public sitemap + product page JSON-LD; matched products only; 3 s per page' "
        "WHERE code = 'glomark' AND kind = 'manual'"
    )


def downgrade() -> None:
    op.execute("UPDATE competitors SET kind = 'manual', adapter = NULL, notes = NULL "
               "WHERE code = 'glomark'")
