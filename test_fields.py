import asyncio
import os
import aiohttp
from dotenv import load_dotenv

load_dotenv()

async def test():
    api_key = os.environ["PNW_API_KEY"]
    query = """
    query($id: [Int]) {
      nations(id: $id, first: 1) {
        data {
          id
          nation_name
          num_cities
          projects
          project_bits
        }
      }
    }
    """
    url = f"https://api.politicsandwar.com/graphql?api_key={api_key}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={"query": query, "variables": {"id": [756196]}}) as resp:
            data = await resp.json()
            print(data)

asyncio.run(test())
