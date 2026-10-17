"""
Revenue calculation engine for /whois's Revenue button.

Every formula here is sourced from the official Politics & War wiki,
cross-checked against the live game where possible (confirmed) or
carried over from the original implementation (unverified, flagged
below). This is NOT guesswork dressed up as fact, where a number is
uncertain it says so.
"""

import math
from datetime import datetime, timezone

# CONFIRMED (wiki, color-bonus percentages NOT independently verified
# tonight, carried over from prior code as unverified placeholders)
COLOR_TRADE_BONUSES = {
    "aqua": 0.1501, "black": 0.1460, "blue": 0.1428, "brown": 0.1380,
    "green": 0.1420, "lavender": 0.1520, "lime": 0.1511, "maroon": 0.1500,
    "orange": 0.1450, "pink": 0.1421, "purple": 0.1507, "red": 0.1400,
    "sky": 0.1470, "turquoise": 0.1527, "white": 0.1200, "yellow": 0.1388,
    "beige": 0.0, "gray": 0.0,
}

# CONFIRMED against Politics & War Military page, matched exactly
# against a real Evergreen screenshot tonight.
UNIT_MONEY_UPKEEP = {
    "soldiers": 1.88, "tanks": 75.0, "aircraft": 1000.0,
    "ships": 5000.0, "missiles": 31500.0, "nukes": 52500.0, "spies": 2400.0,
}

# CONFIRMED (wiki Pollution page, Improvement Pollution Factors table)
POLLUTION_POINTS = {
    "coalpower": 8, "oilpower": 6, "nuclearpower": 0,
    "bauxitemine": 12, "coalmine": 12, "ironmine": 12, "uramine": 20,
    "oilwell": 12, "farm": 2, "gasrefinery": 32, "steelmill": 40,
    "hospital": 4, "recyclingcenter": -70, "subway": -45,
    "mall": 2, "stadium": 5,
}

RESOURCE_KEYS = ["food", "coal", "oil", "uranium", "iron", "bauxite", "steel", "gasoline"]
FALLBACK_PRICES = {
    "food": 125.0, "coal": 3100.0, "oil": 3800.0, "uranium": 24000.0,
    "iron": 3100.0, "bauxite": 3500.0, "steel": 4800.0, "gasoline": 4200.0,
}


async def get_live_market_prices(session) -> dict:
    """Live trade prices, falls back to rough defaults on any failure
    so a market API hiccup never blocks the whole calculation."""
    prices = dict(FALLBACK_PRICES)
    query = "{ tradeprices(first: 10) { data { resource avgprice } } }"
    try:
        async with session.post(
            "https://api.politicsandwar.com/graphql",
            json={"query": query},
            timeout=5,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                for item in data.get("data", {}).get("tradeprices", {}).get("data", []):
                    res = str(item.get("resource", "")).lower()
                    if res in prices and item.get("avgprice"):
                        prices[res] = float(item["avgprice"])
    except Exception:
        pass
    return prices


def _city_age_days(date_str: str | None) -> float:
    if not date_str:
        return 0.0
    try:
        created = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return max((datetime.now(timezone.utc) - created).total_seconds() / 86400.0, 0.0)
    except Exception:
        return 0.0


def _city_commerce(c: dict, max_comm: float) -> float:
    """CONFIRMED: Bank +5%, Stadium +12%, Subway +8% (wiki Improvements/
    Commerce page). Supermarket and Mall percentages are carried over
    from prior code, UNVERIFIED tonight."""
    comm = (
        (c.get("supermarket", 0) or 0) * 3
        + (c.get("bank", 0) or 0) * 5
        + (c.get("mall", 0) or 0) * 9
        + (c.get("stadium", 0) or 0) * 12
        + (c.get("subway", 0) or 0) * 8
    )
    return min(comm, max_comm)


def _city_population(c: dict, commerce: float) -> tuple[float, float]:
    """CONFIRMED structure from the wiki's City_Formulas and Disease
    pages. Returns (final_population, base_population). Police Station
    crime reduction is UNCONFIRMED tonight, contributes 0 here, meaning
    crime (and therefore population loss) is likely slightly
    OVERESTIMATED for any city with police stations built."""
    infra = c.get("infrastructure", 0) or 0
    land = c.get("land", 0) or 1  # avoid /0; land should always be set in practice
    hospitals = c.get("hospital", 0) or 0

    base_pop = infra * 100

    pollution = sum(
        (c.get(improvement, 0) or 0) * points
        for improvement, points in POLLUTION_POINTS.items()
    )

    pop_density = base_pop / land
    disease_rate = (
        (((pop_density ** 2) * 0.01) - 25) / 100
        + (infra / 1000)
        - (hospitals * 2.5)
        + (pollution * 0.05)
    )
    disease_deaths = max(disease_rate, 0) * infra * 100

    crime_rate = (((103 - commerce) ** 2 + (infra * 100)) / 111111)  # police modifier omitted, unconfirmed
    crime_deaths = max((crime_rate / 10) * (infra * 100) - 25, 0)

    age_days = _city_age_days(c.get("date"))
    age_bonus = 1 + max(math.log(age_days) / 15, 0) if age_days > 0 else 1

    population = max((base_pop - disease_deaths - crime_deaths) * age_bonus, 0)
    return population, base_pop


def _food_consumption_for_city(base_pop: float, age_days: float) -> float:
    """CONFIRMED (wiki Food Consumption page). Uses BASE population
    (infra x 100), not the disease/crime-adjusted figure, per the
    wiki's own stated formula."""
    age_modifier = 1 + max(math.log(age_days) / 15, 0) if age_days > 0 else 1
    return (base_pop ** 2 / 125_000_000) + (((base_pop * age_modifier) - base_pop) / 850)


async def compute_revenue(nation: dict, session) -> dict:
    cities = nation.get("cities") or []

    has_mass_irrigation = bool(nation.get("mass_irrigation"))
    has_itc = bool(nation.get("international_trade_center"))
    has_telecom = bool(nation.get("telecommunications_satellite"))

    max_comm = 100
    if has_itc:
        max_comm += 15
    if has_telecom:
        max_comm += 10

    prod = {k: 0.0 for k in RESOURCE_KEYS}
    cons = {k: 0.0 for k in RESOURCE_KEYS}
    gross_money = 0.0
    total_base_pop_food_cons = 0.0

    for c in cities:
        land = c.get("land", 0) or 0
        farm_divisor = 400.0 if has_mass_irrigation else 500.0  # CONFIRMED (wiki City_Formulas)
        prod["food"] += (c.get("farm", 0) or 0) * (land / farm_divisor) if farm_divisor else 0

        # CONFIRMED (wiki Improvements/Resources): 3 tons/day per mine,
        # flat. Uranium Mine rate specifically NOT independently
        # confirmed tonight, 3/day assumed by pattern, flagged.
        prod["coal"] += (c.get("coalmine", 0) or 0) * 3
        prod["oil"] += (c.get("oilwell", 0) or 0) * 3
        prod["iron"] += (c.get("ironmine", 0) or 0) * 3
        prod["bauxite"] += (c.get("bauxitemine", 0) or 0) * 3
        prod["uranium"] += (c.get("uramine", 0) or 0) * 3  # UNCONFIRMED rate

        # CONFIRMED (wiki Improvements/Manufacturing): steel mill
        # converts 3 coal + 3 iron into 9 steel/day.
        mills = c.get("steelmill", 0) or 0
        prod["steel"] += mills * 9
        cons["coal"] += mills * 3
        cons["iron"] += mills * 3

        # CONFIRMED: oil refinery converts 3 oil into 6 gasoline/day.
        refineries = c.get("gasrefinery", 0) or 0
        cons["oil"] += refineries * 3
        prod["gasoline"] += refineries * 6

        # Power plant consumption: UNVERIFIED tonight, carried over
        # from the prior implementation as-is.
        cons["coal"] += (c.get("coalpower", 0) or 0) * 1.2
        cons["oil"] += (c.get("oilpower", 0) or 0) * 1.2
        cons["uranium"] += (c.get("nuclearpower", 0) or 0) * 2.4

        commerce = _city_commerce(c, max_comm)
        population, base_pop = _city_population(c, commerce)

        # CONFIRMED (wiki Nation_Formulas / Average Income): tax rate
        # cancels out of this formula entirely.
        gross_money += (((commerce / 50) * 0.725) + 0.725) * population

        age_days = _city_age_days(c.get("date"))
        total_base_pop_food_cons += _food_consumption_for_city(base_pop, age_days)

    # Soldier food upkeep — CONFIRMED (wiki, 4 independent sources,
    # cross-checked against a real Evergreen upkeep screenshot tonight).
    at_war = any(
        (w.get("turns_left") or 0) > 0
        for w in (nation.get("offensive_wars") or []) + (nation.get("defensive_wars") or [])
    )
    soldiers = nation.get("soldiers", 0) or 0
    soldier_food_cons = soldiers / (500 if at_war else 750)

    cons["food"] = total_base_pop_food_cons + soldier_food_cons

    net = {k: prod[k] - cons.get(k, 0) for k in RESOURCE_KEYS}

    # Military money upkeep — CONFIRMED against real Evergreen screenshot.
    military_money = -sum(
        (nation.get(unit, 0) or 0) * rate for unit, rate in UNIT_MONEY_UPKEEP.items()
    )

    # UNVERIFIED tonight, carried over: Open Markets +1%, food-shortage
    # penalty of x0.67.
    if str(nation.get("domestic_policy", "")).lower().replace("_", " ") == "open markets":
        gross_money *= 1.01
    if net["food"] < 0:
        gross_money *= 0.67

    color = str(nation.get("color", "white")).lower()
    color_bonus_pct = COLOR_TRADE_BONUSES.get(color, 0.1443)  # UNVERIFIED table
    trade_bonus = gross_money * color_bonus_pct

    net_money = gross_money + military_money + trade_bonus

    prices = await get_live_market_prices(session)
    converted_total = net_money + sum(net[k] * prices.get(k, 0) for k in RESOURCE_KEYS)

    return {
        "production": prod,
        "consumption": cons,
        "net": net,
        "gross_money": gross_money,
        "military_money": military_money,
        "trade_bonus": trade_bonus,
        "net_money": net_money,
        "converted_total": converted_total,
        "at_war": at_war,
    }
