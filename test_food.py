import datetime
import asyncio
import os
import aiohttp

# Reads API Key from bot's config/env or prompts
API_KEY = os.getenv("PW_API_KEY", "")

async def fetch_food():
    # If no env key, we can pass a browser User-Agent header
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    
    query = """
    query {
        nations(id: [633226]) {
            data {
                soldiers
                wars {
                    id
                    winner_id
                }
                cities {
                    infrastructure
                    date
                }
            }
        }
    }
    """
    
    url = f"https://api.politicsandwar.com/graphql?api_key={API_KEY}" if API_KEY else "https://api.politicsandwar.com/graphql"
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={"query": query}, headers=headers) as resp:
            if resp.status != 200:
                print(f"API Error Response Status: {resp.status}")
                text = await resp.text()
                print(text)
                return
            
            result = await resp.json()

    nation_data = result["data"]["nations"]["data"][0]
    cities = nation_data.get("cities") or []
    soldiers = nation_data.get("soldiers") or 0
    wars = nation_data.get("wars") or []

    active_wars = [w for w in wars if w.get("winner_id") is None or str(w.get("winner_id")) == "0"]
    is_at_war = len(active_wars) > 0

    now = datetime.datetime.now(datetime.timezone.utc)
    total_city_food_daily = 0.0

    for city in cities:
        infra = float(city.get("infrastructure", 0) or 0)
        base_pop = infra * 100.0

        founded_str = city.get("date")
        city_age_days = 0
        if founded_str:
            try:
                founded_date = datetime.datetime.fromisoformat(str(founded_str).replace("Z", "+00:00"))
                city_age_days = max(0, (now - founded_date).days)
            except Exception:
                city_age_days = 0

        if city_age_days < 90:
            age_modifier = 1.0 + (city_age_days / 90.0) * 0.5
        else:
            age_modifier = 1.5

        city_daily_food = ((base_pop ** 2) / 125000000.0) + (((base_pop * age_modifier) - base_pop) / 850.0)
        total_city_food_daily += city_daily_food

    military_food_rate = 0.003 if is_at_war else 0.002
    military_food_daily = soldiers * military_food_rate

    print("\n--- FOOD CONSUMPTION FOR NATION 633226 ---")
    print(f"City Food Daily:     {round(total_city_food_daily, 2):,.2f}")
    print(f"Military Food Daily: {round(military_food_daily, 2):,.2f}")
    print(f"Total Daily Burn:    {round(total_city_food_daily + military_food_daily, 2):,.2f}")
    print(f"Active War Status:   {is_at_war}\n")

asyncio.run(fetch_food())
