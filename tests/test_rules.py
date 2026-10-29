"""Pure rules: title parsing, matching, the owner's price rule, the schedule, roles."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from app.core.permissions import ROLE_PERMISSIONS, TOTP_REQUIRED_ROLES, has_permission
from app.services import matcher, pricing, schedule
from app.services.catalog_sync import product_size, selling_price
from app.services.normalize import barcode_of, parse, parse_size


# ---------------------------------------------------------------- normalize
def test_sizes_become_base_units():
    assert parse_size("Anchor Milk Powder 1kg") == (D("1000.000"), "g")
    assert parse_size("Coca Cola 1.5L") == (D("1500.000"), "ml")
    assert parse_size("MALIBAN BISCUIT CHOCOLATE CREAM - 400 g") == (D("400.000"), "g")
    assert parse_size("Maliban Biscuit Cheese Bits Tin 245G") == (D("245.000"), "g")
    assert parse_size("Yoghurt 4 x 80g") == (D("320.000"), "g")
    assert parse_size("Golden Yolk Egg - Large (Brown)") == (None, None)


def test_pack_field_counts_when_title_has_no_size():
    assert parse("Chocolate Cream", "400 g").size_value == D("400.000")


def test_words_drop_noise_and_sizes():
    p = parse("ZESTA Premium Tea, 490g")
    assert p.words == {"zesta", "premium", "tea"}


def test_barcode_needs_a_valid_check_digit():
    assert barcode_of("4006381333931") == "4006381333931"  # valid EAN-13
    assert barcode_of("4006381333932") is None  # wrong check digit
    assert barcode_of("00005704") is None  # SPAR internal code, bad check digit
    assert barcode_of("SB-RICE") is None


# ---------------------------------------------------------------- matcher
def test_same_product_different_wording_scores_high():
    ours = parse("Maliban Chocolate Cream Biscuit 400g")
    theirs = parse("MALIBAN BISCUIT CHOCOLATE CREAM - 400 g")
    assert matcher.score(ours, theirs) > 0.9


def test_different_pack_size_is_never_a_candidate():
    ours = parse("Maliban Chocolate Cream Biscuit 400g")
    theirs = parse("Maliban Chocolate Cream Biscuit 200g")
    assert matcher.score(ours, theirs) == 0.0


def test_unknown_size_is_never_suggested():
    # Owner (2026-10-29): brand, name and g / ml must all agree.
    ours = parse("Maliban Chocolate Cream Biscuit 400g")
    assert matcher.score(ours, parse("Maliban Chocolate Cream Biscuit 400g")) > 0.9
    assert matcher.score(ours, parse("Maliban Chocolate Cream Biscuit")) == 0.0


def test_another_brand_is_never_suggested():
    ours = parse("Anchor Full Cream Milk Powder 400g")
    assert matcher.score(ours, parse("Ratthi Full Cream Milk Powder 400g")) == 0.0
    assert matcher.score(parse("Wijaya Chilli Powder 100g"),
                         parse("SERA Chilli Powder, 100g")) == 0.0


def test_brand_spelling_slips_still_count():
    for ours, theirs in [("Morison Gripe Mixture 100ml", "MORISONS Gripe Mixture, 100ml"),
                         ("Sun Crush Lemonade 500ml", "Suncrush Lemonade 500Ml"),
                         ("Pelwatte Full Cream Milk Powder 400g",
                          "Pelwette Full Cream Milk Powder 400g")]:
        assert matcher.brand_ok(parse(ours), parse(theirs)), (ours, theirs)
    assert not matcher.brand_ok(parse("Ceregrow Mixed Fruits 200g"),
                                parse("Nestle Cerelac Mixed Fruits With Milk 200G"))


def test_best_candidates_are_ranked_and_capped():
    ours = parse("Munchee Super Cream Cracker 490g")
    items = [
        ("a", parse("Munchee Super Cream Cracker 490G")),
        ("b", parse("Munchee Cream Cracker 490g")),
        ("c", parse("Maliban Cream Cracker 490g")),
        ("d", parse("Munchee Super Cream Cracker 125g")),
        ("e", parse("Sunlight Soap 110g")),
    ]
    got = matcher.best_candidates(ours, items)
    assert [c.key for c in got][:2] == ["a", "b"]
    assert "d" not in [c.key for c in got] and "e" not in [c.key for c in got]
    assert len(got) <= matcher.CANDIDATES_PER_COMPETITOR


# ---------------------------------------------------------------- pricing (owner's rule, Q5)
def _offers(*prices):
    return [pricing.Offer(i + 1, D(p)) for i, p in enumerate(prices)]


def test_matches_the_cheapest_competitor():
    v = pricing.decide(current=D("520"), cost=D("400"), offers=_offers("495", "480.50", "510"),
                       min_margin_pct=D("5"))
    assert v.outcome == "suggest" and v.reason == "match_cheapest"
    assert v.suggested_price == D("480") and v.cheapest.competitor_id == 2
    assert v.floor_price == D("420")


def test_never_below_cost_plus_margin():
    v = pricing.decide(current=D("520"), cost=D("470"), offers=_offers("450"),
                       min_margin_pct=D("5"))
    assert v.outcome == "suggest" and v.reason == "floor"
    assert v.suggested_price == D("494")  # 470 * 1.05 = 493.5 -> up to 494


def test_no_suggestion_without_a_cost():
    v = pricing.decide(current=D("520"), cost=None, offers=_offers("450"), min_margin_pct=D("5"))
    assert v.outcome == "cost_missing" and v.suggested_price is None


def test_never_raises_a_price():
    v = pricing.decide(current=D("400"), cost=D("300"), offers=_offers("450"),
                       min_margin_pct=D("5"))
    assert v.outcome == "cheaper"


def test_floor_above_current_means_hold():
    v = pricing.decide(current=D("500"), cost=D("490"), offers=_offers("450"),
                       min_margin_pct=D("5"))
    assert v.outcome == "at_floor"


def test_no_offers_no_opinion():
    assert pricing.decide(current=D("1"), cost=D("1"), offers=[],
                          min_margin_pct=D("5")).outcome == "no_data"


def test_plain_row_gets_a_branch_price():
    body = pricing.online_update_for(target=D("480"), branch_price=None, global_price=D("520"),
                                     discount_pct=None, is_on_sale=False)
    assert body == {"branch_price": "480.00"}


def test_quick_sale_row_keeps_mrp_and_changes_the_discount():
    body = pricing.online_update_for(target=D("440"), branch_price=D("520"),
                                     global_price=D("520"), discount_pct=D("5"), is_on_sale=True)
    pct = D(str(body["discount_percentage"]))
    assert "branch_price" not in body
    assert (D("520") * (1 - pct / 100)).quantize(D("0.01")) == D("440.00")


# ---------------------------------------------------------------- what the customer pays
def test_selling_price_applies_the_discount():
    assert selling_price(520, 15.384615) == D("440.00")
    assert selling_price(250, None) == D("250.00")
    assert selling_price(None, 10) is None


def test_product_size_falls_back_to_weight():
    assert product_size("Rice 1kg", None, None) == (D("1000.000"), "g")
    assert product_size("Basmati Rice", "0.5", "kg") == (D("500.000"), "g")
    assert product_size("Coconut", None, None) == (None, None)


# ---------------------------------------------------------------- schedule (Q3)
def test_due_follows_review_level():
    now = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)
    yesterday = now - timedelta(hours=23)
    assert schedule.is_due("daily", yesterday, now)
    assert not schedule.is_due("weekly", yesterday, now)
    assert schedule.is_due("weekly", now - timedelta(days=7), now)
    assert not schedule.is_due("long", now - timedelta(days=40), now)
    assert schedule.is_due("long", now - timedelta(days=60), now)
    assert schedule.is_due("monthly", None, now)


def test_stale_prices_do_not_count():
    now = datetime(2026, 10, 27, tzinfo=UTC)
    stale = {"daily": 2, "weekly": 9, "monthly": 35, "long": 70}
    assert schedule.is_fresh("daily", now - timedelta(days=1), now, stale)
    assert not schedule.is_fresh("daily", now - timedelta(days=3), now, stale)


# ---------------------------------------------------------------- roles (Q9)
def test_only_deciders_change_prices():
    assert has_permission("branch_manager", "suggestions.decide")
    assert has_permission("super_admin", "suggestions.decide")
    assert not has_permission("marketing_manager", "suggestions.decide")
    assert not has_permission("price_checker", "suggestions.decide")


def test_checker_confirms_matches_but_sets_no_cost():
    assert has_permission("price_checker", "matches.decide")
    assert has_permission("price_checker", "observations.manual")
    assert not has_permission("price_checker", "costs.write")
    assert not has_permission("branch_manager", "matches.decide")


def test_price_changers_need_two_factor():
    changers = {r for r, perms in ROLE_PERMISSIONS.items()
                if {"suggestions.decide", "users.write"} & perms}
    assert changers <= TOTP_REQUIRED_ROLES


def test_daily_run_is_at_five_in_colombo():
    from app.cli import seconds_until_next_run

    before = datetime(2026, 10, 27, 23, 0, tzinfo=UTC)  # 04:30 in Colombo
    assert seconds_until_next_run(before) == 30 * 60
    after = datetime(2026, 10, 27, 0, 0, tzinfo=UTC)  # 05:30 in Colombo
    assert seconds_until_next_run(after) == 23.5 * 3600


def test_sure_match_is_automatic_only_with_equal_size_and_a_clear_lead():
    ours = parse("Highland Butter Salted 200g")
    same = parse("HIGHLAND BUTTER SALTED - 200 g")
    rival = parse("Highland Butter Unsalted 200g")
    best = matcher.Candidate("a", matcher.score(ours, same))
    assert best.score >= matcher.AUTO_SCORE
    assert matcher.auto_pick(ours, [(best, same)]) is best
    close = matcher.Candidate("b", best.score - 0.01)
    assert matcher.auto_pick(ours, [(best, same), (close, rival)]) is None  # no clear lead
    unsized = parse("Highland Butter Salted")
    assert matcher.auto_pick(ours, [(matcher.Candidate("c", 0.99), unsized)]) is None
    weak = matcher.Candidate("d", 0.9)
    assert matcher.auto_pick(ours, [(weak, same)]) is None


def test_production_needs_an_explicit_choice_to_drop_two_factor():
    import pytest
    from pydantic import ValidationError

    from app.core.config import Settings

    base = {"database_url": "postgresql+asyncpg://x/y", "jwt_secret": "x" * 40,
            "totp_encryption_key": "k", "environment": "production", "two_factor": False}
    with pytest.raises(ValidationError):
        Settings(**base)
    assert Settings(**base, allow_production_without_2fa=True).two_factor is False


def test_a_shop_listing_the_same_product_twice_still_auto_matches():
    ours = parse("Pepsi 1l")
    twin_a, twin_b = parse("Pepsi 1L"), parse("PEPSI - 1 L")
    a = matcher.Candidate("a", matcher.score(ours, twin_a))
    b = matcher.Candidate("b", matcher.score(ours, twin_b))
    assert matcher.auto_pick(ours, [(a, twin_a), (b, twin_b)]) is a
    other = parse("Pepsi Max 1L")
    close = matcher.Candidate("c", a.score - 0.01)
    assert matcher.auto_pick(ours, [(a, twin_a), (close, other)]) is None


def test_implausibly_cheap_prices_are_not_chased():
    # Our placeholder Rs 1,000 rice against a real Rs 200: look, do not cut.
    v = pricing.decide(current=D("1000"), cost=D("150"), offers=_offers("200"),
                       min_margin_pct=D("5"))
    assert v.outcome == "check" and v.suggested_price is None
    # A plausible price next to an outlier still drives the suggestion.
    v = pricing.decide(current=D("1000"), cost=D("150"), offers=_offers("200", "900"),
                       min_margin_pct=D("5"))
    assert v.outcome == "suggest" and v.suggested_price == D("900")


def test_different_pack_kinds_are_never_automatic():
    ours = parse("Lakspray Full Cream Milk Powder Box 400g")
    theirs = parse("Lakspray Full Cream Milk Powder Pouch 400G")
    best = matcher.Candidate("a", matcher.score(ours, theirs))
    assert best.score >= matcher.AUTO_SCORE
    assert matcher.auto_pick(ours, [(best, theirs)]) is None
    plain = parse("Lakspray Full Cream Milk Powder 400g")  # no pack named: fine
    assert matcher.auto_pick(plain, [(best, theirs)]) is best


def _priced(name, price, score=0.8):
    return (matcher.Candidate(name, score), parse(name), D(price))


def test_price_agreement_confirms_a_fuller_title():
    ours = parse("Iodex Balm 18g")
    pick = matcher.price_pick(ours, "Iodex Balm 18g", D("375"),
                              [_priced("IODEX QUICK PAIN RELIEF BALM 18G", "375")])
    assert pick is not None and pick.key == "IODEX QUICK PAIN RELIEF BALM 18G"


def test_price_agreement_never_crosses_a_product_kind_or_a_placeholder():
    ours = parse("Maldive Fish 100g")
    sambol = [_priced("Golden Food Product Maldive Fish Sambol 100G", "385")]
    assert matcher.price_pick(ours, "Maldive Fish 100g", D("400"), sambol) is None
    gram = [_priced("Star Gold Green Gram 1Kg", "980")]
    assert matcher.price_pick(parse("Green Gram 1kg"), "Green Gram 1kg", D("1000"), gram) is None
    far = [_priced("IODEX QUICK PAIN RELIEF BALM 18G", "450")]
    assert matcher.price_pick(parse("Iodex Balm 18g"), "Iodex Balm 18g", D("375"), far) is None
    sinhala = "Siddalepa Kekulu " + chr(0x0DAD) + " 70g"
    soap = [_priced("Kekulu Soap Siddalepa 70g", "120")]
    assert matcher.price_pick(parse(sinhala), sinhala, D("120"), soap) is None


def test_price_agreement_prefers_the_closest_title():
    ours = parse("Munchee Tifin 125g")
    pick = matcher.price_pick(ours, "Munchee Tifin 125g", D("200"), [
        _priced("Munchee Tifin Onion Biscuits 125g", "200"),
        _priced("MUNCHEE Tifin, 125g", "200"),
    ])
    assert pick.key == "MUNCHEE Tifin, 125g"
