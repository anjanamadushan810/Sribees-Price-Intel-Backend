"""Foundation: people, our catalogue copy, competitors, observations, matches, suggestions.

Rules kept in the database so they hold even when application code is wrong:

* the audit log and the price history are append-only;
* a product has at most one live match (auto or confirmed) per competitor;
* a branch has at most one pending suggestion per product;
* a suggestion never goes below its floor (cost + margin) and never raises a price.

Revision ID: 0001
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


SCHEMA = r"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- people
CREATE TABLE branches (
    branch_id   UUID PRIMARY KEY,
    name        VARCHAR(120) NOT NULL,
    is_active   BOOLEAN      NOT NULL DEFAULT true,
    synced_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE users (
    user_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email            VARCHAR(255) NOT NULL,
    full_name        VARCHAR(150) NOT NULL,
    password_hash    VARCHAR(255) NOT NULL,
    role             VARCHAR(20)  NOT NULL CHECK (role IN
                        ('super_admin','branch_manager','marketing_manager','price_checker')),
    all_branches     BOOLEAN      NOT NULL DEFAULT false,
    is_active        BOOLEAN      NOT NULL DEFAULT true,
    totp_secret_enc  TEXT,
    totp_enabled     BOOLEAN      NOT NULL DEFAULT false,
    failed_logins    INTEGER      NOT NULL DEFAULT 0,
    locked_until     TIMESTAMPTZ,
    last_login_at    TIMESTAMPTZ,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_users_email ON users (lower(email));

CREATE TABLE user_branches (
    user_id    UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    branch_id  UUID NOT NULL REFERENCES branches(branch_id),
    PRIMARY KEY (user_id, branch_id)
);

CREATE TABLE user_sessions (
    session_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    refresh_hash   VARCHAR(64) NOT NULL UNIQUE,
    user_agent     VARCHAR(300),
    ip_address     VARCHAR(64),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    revoked_at     TIMESTAMPTZ
);
CREATE INDEX ix_user_sessions_user ON user_sessions (user_id);

CREATE TABLE audit_log (
    audit_id     BIGSERIAL PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id      UUID REFERENCES users(user_id),
    action       VARCHAR(60) NOT NULL,
    entity       VARCHAR(40) NOT NULL,
    entity_id    VARCHAR(100),
    before       JSONB,
    after        JSONB,
    ip_address   VARCHAR(64)
);
CREATE INDEX ix_audit_entity ON audit_log (entity, entity_id);
CREATE INDEX ix_audit_user ON audit_log (user_id, occurred_at);

CREATE TABLE settings (
    key         VARCHAR(60) PRIMARY KEY,
    value       JSONB       NOT NULL,
    updated_by  UUID REFERENCES users(user_id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- our catalogue (copy of Online)
CREATE TABLE products (
    product_id    UUID PRIMARY KEY,
    name          VARCHAR(255) NOT NULL,
    name_si       VARCHAR(255),
    sku           VARCHAR(100),
    barcode       VARCHAR(20),
    category      VARCHAR(120),
    size_value    NUMERIC(12,3) CHECK (size_value > 0),
    size_unit     VARCHAR(4) CHECK (size_unit IN ('g','ml','pcs')),
    review_level  VARCHAR(10)  NOT NULL DEFAULT 'long'
                    CHECK (review_level IN ('daily','weekly','monthly','long')),
    mrp           NUMERIC(10,2) CHECK (mrp >= 0),
    is_active     BOOLEAN      NOT NULL DEFAULT true,
    synced_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX ix_products_barcode ON products (barcode) WHERE barcode IS NOT NULL;

CREATE TABLE branch_products (
    branch_id        UUID NOT NULL REFERENCES branches(branch_id),
    product_id       UUID NOT NULL REFERENCES products(product_id),
    inventory_id     UUID,
    branch_price     NUMERIC(10,2) CHECK (branch_price >= 0),
    global_price     NUMERIC(10,2) CHECK (global_price >= 0),
    discount_pct     NUMERIC(12,6) CHECK (discount_pct >= 0 AND discount_pct < 100),
    is_on_sale       BOOLEAN      NOT NULL DEFAULT false,
    selling_price    NUMERIC(10,2) CHECK (selling_price >= 0),
    is_active        BOOLEAN      NOT NULL DEFAULT true,
    cost_price       NUMERIC(10,2) CHECK (cost_price > 0),
    cost_updated_by  UUID REFERENCES users(user_id),
    cost_updated_at  TIMESTAMPTZ,
    synced_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_id, product_id)
);

-- ---------------------------------------------------------------- the market
CREATE TABLE competitors (
    competitor_id  SERIAL PRIMARY KEY,
    code           VARCHAR(30)  NOT NULL UNIQUE,
    name           VARCHAR(80)  NOT NULL,
    kind           VARCHAR(10)  NOT NULL CHECK (kind IN ('scraped','manual')),
    adapter        VARCHAR(20),
    base_url       VARCHAR(200),
    is_enabled     BOOLEAN      NOT NULL DEFAULT true,
    status         VARCHAR(12)  NOT NULL DEFAULT 'ok'
                     CHECK (status IN ('ok','failing','blocked','pending')),
    notes          TEXT,
    last_run_at    TIMESTAMPTZ,
    last_ok_at     TIMESTAMPTZ,
    CHECK (kind = 'manual' OR adapter IS NOT NULL)
);

CREATE TABLE competitor_items (
    item_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    competitor_id  INTEGER      NOT NULL REFERENCES competitors(competitor_id),
    external_id    VARCHAR(120) NOT NULL,
    name           VARCHAR(300) NOT NULL,
    brand          VARCHAR(120),
    barcode        VARCHAR(20),
    size_value     NUMERIC(12,3) CHECK (size_value > 0),
    size_unit      VARCHAR(4) CHECK (size_unit IN ('g','ml','pcs')),
    url            VARCHAR(500),
    last_price     NUMERIC(10,2) CHECK (last_price > 0),
    last_mrp       NUMERIC(10,2) CHECK (last_mrp > 0),
    in_stock       BOOLEAN      NOT NULL DEFAULT true,
    first_seen_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (competitor_id, external_id)
);
CREATE INDEX ix_items_barcode ON competitor_items (barcode) WHERE barcode IS NOT NULL;

CREATE TABLE price_observations (
    observation_id  BIGSERIAL PRIMARY KEY,
    item_id         UUID NOT NULL REFERENCES competitor_items(item_id),
    price           NUMERIC(10,2) NOT NULL CHECK (price > 0),
    mrp             NUMERIC(10,2) CHECK (mrp > 0),
    in_stock        BOOLEAN     NOT NULL DEFAULT true,
    source          VARCHAR(10) NOT NULL CHECK (source IN ('scrape','manual')),
    entered_by      UUID REFERENCES users(user_id),
    note            VARCHAR(300),
    seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (source = 'scrape' OR entered_by IS NOT NULL)
);
CREATE INDEX ix_observations_item ON price_observations (item_id, seen_at DESC);

CREATE TABLE product_matches (
    match_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id     UUID NOT NULL REFERENCES products(product_id),
    item_id        UUID NOT NULL REFERENCES competitor_items(item_id),
    competitor_id  INTEGER NOT NULL REFERENCES competitors(competitor_id),
    status         VARCHAR(10) NOT NULL
                     CHECK (status IN ('candidate','auto','confirmed','rejected')),
    method         VARCHAR(10) NOT NULL CHECK (method IN ('barcode','name','manual')),
    score          NUMERIC(5,4) NOT NULL CHECK (score BETWEEN 0 AND 1),
    decided_by     UUID REFERENCES users(user_id),
    decided_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (product_id, item_id),
    CHECK (status NOT IN ('confirmed','rejected') OR decided_by IS NOT NULL)
);
CREATE UNIQUE INDEX uq_matches_one_live ON product_matches (product_id, competitor_id)
    WHERE status IN ('auto','confirmed');
CREATE INDEX ix_matches_queue ON product_matches (status, product_id) WHERE status = 'candidate';

CREATE TABLE scrape_runs (
    run_id         BIGSERIAL PRIMARY KEY,
    competitor_id  INTEGER     NOT NULL REFERENCES competitors(competitor_id),
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    status         VARCHAR(10) NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','ok','failed','blocked')),
    requests       INTEGER     NOT NULL DEFAULT 0,
    items_seen     INTEGER     NOT NULL DEFAULT 0,
    error          TEXT
);
CREATE INDEX ix_runs_competitor ON scrape_runs (competitor_id, started_at DESC);

-- When a search source (Arpico) last looked a product up, matched or not, so the
-- review-level schedule also covers products that have no match yet.
CREATE TABLE search_checks (
    product_id     UUID    NOT NULL REFERENCES products(product_id),
    competitor_id  INTEGER NOT NULL REFERENCES competitors(competitor_id),
    checked_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (product_id, competitor_id)
);

CREATE TABLE suggestions (
    suggestion_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id               UUID NOT NULL REFERENCES branches(branch_id),
    product_id              UUID NOT NULL REFERENCES products(product_id),
    current_price           NUMERIC(10,2) NOT NULL CHECK (current_price > 0),
    suggested_price         NUMERIC(10,2) NOT NULL CHECK (suggested_price > 0),
    cheapest_price          NUMERIC(10,2) NOT NULL CHECK (cheapest_price > 0),
    cheapest_competitor_id  INTEGER NOT NULL REFERENCES competitors(competitor_id),
    cost_price              NUMERIC(10,2) NOT NULL CHECK (cost_price > 0),
    floor_price             NUMERIC(10,2) NOT NULL CHECK (floor_price > 0),
    reason                  VARCHAR(20) NOT NULL CHECK (reason IN ('match_cheapest','floor')),
    status                  VARCHAR(12) NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','accepted','rejected','superseded',
                                                'failed','applied')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by              UUID REFERENCES users(user_id),
    decided_at              TIMESTAMPTZ,
    applied_at              TIMESTAMPTZ,
    apply_error             TEXT,
    CHECK (suggested_price < current_price),
    CHECK (suggested_price >= floor_price),
    CHECK (status IN ('pending','superseded') OR decided_at IS NOT NULL)
);
CREATE UNIQUE INDEX uq_suggestions_one_pending ON suggestions (branch_id, product_id)
    WHERE status = 'pending';
CREATE INDEX ix_suggestions_branch ON suggestions (branch_id, status, created_at DESC);

-- ---------------------------------------------------------------- append-only tables
CREATE FUNCTION pi_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END $$;

CREATE TRIGGER trg_audit_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION pi_append_only();
CREATE TRIGGER trg_audit_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION pi_append_only();
CREATE TRIGGER trg_observations_append_only
    BEFORE UPDATE OR DELETE ON price_observations
    FOR EACH ROW EXECUTE FUNCTION pi_append_only();
"""

# Sources from VISION.md §9 Q6 and docs/PHASE0_FINDINGS.md. Keells and Cargills start
# 'pending' (adapter not written yet) and disabled; Glomark and Laugfs are manual.
COMPETITORS = r"""
INSERT INTO competitors (code, name, kind, adapter, base_url, is_enabled, status, notes) VALUES
 ('nexmart', 'Nexmart',           'scraped', 'nexmart',  'https://www.nexmart.lk', true,  'ok', NULL),
 ('spar',    'SPAR',              'scraped', 'shopify',  'https://spar2u.lk',      false, 'pending',
             'spar2u.lk/agents.md: server-to-server agents must sign requests with Web Bot Auth; enable after that is built'),
 ('arpico',  'Arpico Supercentre','scraped', 'opencart', 'https://myarpico.com',   true,  'ok',
             'robots Content-Signal use=reference: price reference only, never AI training'),
 ('keells',  'Keells',            'scraped', 'keells',   'https://www.keellssuper.com', false, 'pending',
             'Phase 2: guest-session API at zebraliveback.keellssuper.com'),
 ('cargills','Cargills',          'scraped', 'cargills', 'https://cargillsonline.com',  false, 'pending',
             'Phase 2: AngularJS JSON endpoints with encrypted ids'),
 ('glomark', 'Glomark',           'manual',  NULL, 'https://glomark.lk', true, 'ok',
             'Zenedge bot challenge on every URL: never automated'),
 ('laugfs',  'Laugfs Super',      'manual',  NULL, NULL, true, 'ok', 'No online store found'),
 ('local',   'Local shops (manual)', 'manual', NULL, NULL, true, 'ok',
             'Shelf prices from shops near a branch, typed by the Price Checker');

INSERT INTO settings (key, value) VALUES
 ('pricing', '{"min_margin_pct": 5, "stale_days": {"daily": 2, "weekly": 9, "monthly": 35, "long": 70}}');
"""


def upgrade() -> None:
    op.execute(SCHEMA)
    op.execute(COMPETITORS)


def downgrade() -> None:
    raise RuntimeError("the foundation migration is not reversible; restore from backup")
