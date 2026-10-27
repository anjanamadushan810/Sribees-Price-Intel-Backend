"""Who may do what (VISION.md §9, Q9).

Roles are fixed in code so the rules are reviewable in one place."""

READ = {"products.read", "prices.read", "competitors.read", "branches.read"}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "super_admin": frozenset(
        READ
        | {
            "users.read",
            "users.write",
            "audit.read",
            "competitors.write",
            "settings.read",
            "settings.write",
            "scrape.run",
            "catalog.sync",
            "matches.read",
            "matches.decide",
            "observations.manual",
            "costs.write",
            "suggestions.read",
            "suggestions.decide",
            "reports.read",
        }
    ),
    # Decides prices for their own branch: types costs, accepts or rejects suggestions.
    "branch_manager": frozenset(
        READ | {"costs.write", "suggestions.read", "suggestions.decide", "reports.read",
                "settings.read"}
    ),
    # Sees the market and the reports; changes nothing.
    "marketing_manager": frozenset(READ | {"suggestions.read", "reports.read"}),
    # Keeps the data honest: confirms product matches, types shelf prices from shops.
    "price_checker": frozenset(READ | {"matches.read", "matches.decide", "observations.manual"}),
}

ROLES = tuple(ROLE_PERMISSIONS)

# Roles that see every branch regardless of their branch list.
ALL_BRANCH_ROLES = {"super_admin", "marketing_manager"}

# Anyone who can change a live price or a user signs in with a second factor.
TOTP_REQUIRED_ROLES = {"super_admin", "branch_manager"}


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
