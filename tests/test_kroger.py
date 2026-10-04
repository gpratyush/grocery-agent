"""Kroger store prices, against a fake API: matched items use shelf prices, the rest keep estimates."""

from __future__ import annotations

import httpx
import pytest
from conftest import ITALIAN_1, THAI_1, THAI_2

from grocery_agent.kroger import Kroger, KrogerPrices, matches, pick, size_grams
from grocery_agent.scoring import Evaluator
from grocery_agent.store import IngredientFacts

PRODUCTS = {
    "chicken breast": [
        {"description": "Kroger® Chicken Breast Tenderloins", "items": [
            {"size": "1 lb", "price": {"regular": 5.99, "promo": 0}}]},
    ],
    "jasmine rice": [
        {"description": "Mahatma Jasmine Rice", "items": [{"size": "2 lb", "price": {"regular": 4.29, "promo": 3.49}}]},
    ],
    "fish sauce": [
        {"description": "Soy Vay Teriyaki", "items": [{"size": "10 fl oz", "price": {"regular": 4.0}}]},
    ],
    "basil": [
        {"description": "Fresh Basil", "items": [{"size": "0.75 oz", "price": {"regular": 2.5, "promo": 0}}]},
    ],
}


def fake_api(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/oauth2/token"):
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        assert request.headers["authorization"] == "Bearer tok"
        if request.url.path.endswith("/locations"):
            assert request.url.params["filter.zipCode.near"] == "45202"
            return httpx.Response(200, json={"data": [
                {"locationId": "01400943", "chain": "KROGER", "name": "Kroger Downtown",
                 "address": {"addressLine1": "100 E Court St", "city": "Cincinnati", "state": "OH"}}]})
        if request.url.path.endswith("/products"):
            assert request.url.params["filter.locationId"] == "01400943"
            return httpx.Response(200, json={"data": PRODUCTS.get(request.url.params["filter.term"], [])})
        return httpx.Response(404)

    return Kroger("id", "secret", client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("size,facts,grams", [
    ("16 oz", None, 453.6),
    ("2 lbs", None, 907.2),
    ("1/2 gal", IngredientFacts("milk", density=1.03), 0.5 * 3785 * 1.03),
    ("32 fl oz", None, 32 * 29.57),
    ("12 ct", IngredientFacts("egg", each_g=50), 600),
    ("6 ct / 12 fl oz", None, 6 * 12 * 29.57),
    ("1 bunch", None, None),
    ("", None, None),
])
def test_size_grams(size, facts, grams):
    got = size_grams(size, facts)
    assert got == pytest.approx(grams) if grams else got is None


def test_match_needs_every_ingredient_word():
    assert matches("chicken thigh", "Kroger® Boneless Skinless Chicken Thighs")
    assert not matches("fish sauce", "Soy Vay Teriyaki Sauce")


def test_pick_uses_promo_and_per_pound_items():
    hit = pick("ground beef", [{"description": "Ground Beef 80/20", "items": [
        {"size": "", "soldBy": "WEIGHT", "price": {"regular": 4.99, "promo": 4.49}}]}], None)
    assert (hit.package_g, hit.price) == (453.6, 4.49)


def test_locations(store):
    api = fake_api([])
    [loc] = api.locations("45202")
    assert loc == {"id": "01400943", "name": "Kroger Downtown", "address": "100 E Court St, Cincinnati, OH"}


def test_plan_uses_kroger_prices_and_estimates_the_rest(store, prefs, pool):
    calls = []
    prices = KrogerPrices(fake_api(calls), store, "01400943", "Kroger Downtown")
    estimate = Evaluator(store, prefs).evaluate([THAI_1.id, THAI_2.id, ITALIAN_1.id])
    ev = Evaluator(store, prefs, prices).evaluate([THAI_1.id, THAI_2.id, ITALIAN_1.id])
    lines = {g.canonical: g for g in ev.grocery}
    breast = lines["chicken breast"]
    assert (breast.price_source, breast.package_g, breast.cost) == ("kroger", 453.6, 5.99)
    assert lines["jasmine rice"].cost == 3.49 and "Mahatma" in lines["jasmine rice"].product
    # no product named "fish sauce": keeps the estimate
    est = {g.canonical: g for g in estimate.grocery}
    assert lines["fish sauce"].price_source == "estimate" and lines["fish sauce"].cost == est["fish sauce"].cost
    assert ev.summary()["store_priced_items"] == 3
    assert ev.total_cost != estimate.total_cost

    # Second evaluation is served from the cache: one token call, one search per ingredient, ever.
    searches = calls.count("/v1/products")
    Evaluator(store, prefs, prices).evaluate([THAI_1.id, THAI_2.id, ITALIAN_1.id])
    assert calls.count("/v1/products") == searches and calls.count("/v1/connect/oauth2/token") == 1


def test_api_failure_falls_back_to_estimates(store, prefs, pool):
    def broken(request):
        return httpx.Response(401, json={"error": "invalid_client"})

    api = Kroger("id", "bad", client=httpx.Client(transport=httpx.MockTransport(broken)))
    prices = KrogerPrices(api, store, "01400943")
    ev = Evaluator(store, prefs, prices).evaluate([THAI_1.id, THAI_2.id, ITALIAN_1.id])
    estimate = Evaluator(store, prefs).evaluate([THAI_1.id, THAI_2.id, ITALIAN_1.id])
    assert ev.total_cost == estimate.total_cost
    assert prices.failed and "sign in" in prices.failed


def test_stale_cache_is_refreshed(store, prefs, pool):
    calls = []
    prices = KrogerPrices(fake_api(calls), store, "01400943", cache_days=0)
    prices.price("basil")
    prices.price("basil")
    assert calls.count("/v1/products") == 2


def test_plan_command_without_keys_uses_estimates(store, prefs, monkeypatch, capsys):
    from grocery_agent import cli

    monkeypatch.delenv("KROGER_CLIENT_ID", raising=False)
    prefs.price_source, prefs.kroger_location_id = "kroger", "01400943"
    assert cli.make_prices(store, prefs) is None
    assert "connect-kroger" in capsys.readouterr().err
    monkeypatch.setenv("KROGER_CLIENT_ID", "id")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "s")
    assert cli.make_prices(store, prefs).location_id == "01400943"


def test_connect_kroger_saves_keys_and_store(tmp_path, monkeypatch):
    from grocery_agent import cli
    from grocery_agent.config import Preferences

    for k in ("KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("grocery_agent.kroger.Kroger", lambda cid, secret: fake_api([]))
    secrets = iter(["my-id", "my-secret"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(secrets))
    answers = iter(["45202", "1"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    prefs = Preferences()
    assert cli.connect_kroger(tmp_path / ".env", prefs) == 0
    env = (tmp_path / ".env").read_text()
    assert "KROGER_CLIENT_ID=my-id" in env and "KROGER_CLIENT_SECRET=my-secret" in env
    assert (prefs.price_source, prefs.zip_code, prefs.kroger_location_id) == ("kroger", "45202", "01400943")
    assert prefs.kroger_store == "Kroger Downtown, 100 E Court St, Cincinnati, OH"


def test_plan_and_telegram_label_store_prices(store, prefs, pool):
    from grocery_agent.delivery import telegram_messages
    from grocery_agent.render import render_plan

    prices = KrogerPrices(fake_api([]), store, "01400943", "Kroger Downtown, 100 E Court St")
    ev = Evaluator(store, prefs, prices).evaluate([THAI_1.id, THAI_2.id, ITALIAN_1.id])
    n = ev.estimated_count
    md = render_plan("p1", ev, prefs)
    assert f"groceries **{ev.total_cost:.2f} USD** (Kroger Downtown, 100 E Court St; {n} items estimated)" in md
    assert "basil: need" in md and next(l for l in md.splitlines() if "basil: need" in l).count("~") == 0
    assert next(l for l in md.splitlines() if "fish sauce: need" in l).split("USD")[1].startswith(" ~")
    assert "~ marks an estimate" in md
    overview, _meals, *shopping = telegram_messages(ev, prefs)
    assert f"<i>Kroger Downtown, 100 E Court St; {n} items estimated</i>" in overview
    assert "fish sauce: " in "".join(shopping) and " ~" in "".join(shopping)


def test_estimate_only_plan_reads_as_before(store, prefs, pool):
    from grocery_agent.render import render_plan

    md = render_plan("p1", Evaluator(store, prefs).evaluate([THAI_1.id, THAI_2.id, ITALIAN_1.id]), prefs)
    assert "estimated groceries **" in md and "~" not in md and "Prices are estimates." in md
