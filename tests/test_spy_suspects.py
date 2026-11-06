import unittest
from datetime import datetime, timedelta, timezone

from utils.spy_suspects import find_possible_spy_suspects


class MockPWClient:
    def __init__(self, treaties, nations):
        self.treaties = treaties
        self.nations = nations

    async def get_alliance_treaties(self, alliance_id):
        return self.treaties

    async def get_nations_in_score_range(self, minimum, maximum):
        self.score_range = (minimum, maximum)
        return self.nations


class SpySuspectTests(unittest.IsolatedAsyncioTestCase):

    async def test_filters_and_sorts_suspects(self):
        now = datetime.now(timezone.utc)

        def active(minutes):
            return (now - timedelta(minutes=minutes)).isoformat()

        victim = {
            "id": 100,
            "score": 1000,
            "alliance_id": 10,
        }

        treaties = [
            {"other_alliance_id": 20, "turns_left": 100},
            {"other_alliance_id": 30, "turns_left": 0},
        ]

        nations = [
            {
                "id": 101,
                "alliance_id": 10,
                "last_active": active(1),
            },
            {
                "id": 102,
                "alliance_id": 20,
                "last_active": active(1),
            },
            {
                "id": 103,
                "alliance_id": 30,
                "last_active": active(2),
            },
            {
                "id": 104,
                "alliance_id": 40,
                "last_active": active(3),
            },
            {
                "id": 105,
                "alliance_id": 50,
                "last_active": active(20),
            },
            {
                "id": 100,
                "alliance_id": 40,
                "last_active": active(1),
            },
            {
                "id": 106,
                "alliance_id": 60,
                "last_active": active(1),
            },
        ]

        client = MockPWClient(treaties, nations)

        result = await find_possible_spy_suspects(client, victim)

        result_ids = [nation["id"] for nation in result]

        self.assertEqual(result_ids, [106, 103, 104])
        self.assertEqual(client.score_range, (400.0, 2500.0))

    async def test_rejects_missing_victim_score(self):
        client = MockPWClient([], [])

        with self.assertRaises(ValueError):
            await find_possible_spy_suspects(
                client,
                {"id": 1, "alliance_id": 10, "score": 0},
            )

    async def test_rejects_missing_victim_alliance(self):
        client = MockPWClient([], [])

        with self.assertRaises(ValueError):
            await find_possible_spy_suspects(
                client,
                {"id": 1, "score": 1000},
            )


if __name__ == "__main__":
    unittest.main()
