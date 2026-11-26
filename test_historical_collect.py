import json
import os
import tempfile
import unittest
from unittest.mock import patch

import historical_collect


class HistoricalCollectorTests(unittest.TestCase):
    def test_produced_slots_uses_bounded_inclusive_chunks(self):
        calls = []

        def fake_rpc(url, method, params):
            calls.append((method, params))
            left, right = params
            return list(range(left, right + 1, 2))

        with patch.object(historical_collect, "rpc", side_effect=fake_rpc):
            slots = historical_collect.produced_slots("https://rpc.invalid", 10, 19, 4)

        self.assertEqual([10, 12, 14, 16, 18], slots)
        self.assertEqual(
            [("getBlocks", [10, 13]), ("getBlocks", [14, 17]), ("getBlocks", [18, 19])],
            calls,
        )

    def test_checkpoint_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "checkpoint.json")
            self.assertEqual(set(), historical_collect.read_checkpoint(path))
            expected = {101, 205, 999}
            historical_collect.write_checkpoint(path, expected)
            self.assertEqual(expected, historical_collect.read_checkpoint(path))

    def test_launch_candidates_have_null_label_until_review(self):
        row = {
            "mint": "ExampleMint",
            "launch_slot": 123,
            "block_time": 1700000000,
            "signature": "ExampleSignature",
            "source": "solana-jsonrpc-getBlock-jsonParsed",
            "label": None,
            "status": "candidate_requires_pool_and_outcome_review",
        }
        self.assertIsNone(row["label"])
        self.assertIn("signature", row)
        self.assertIn("launch_slot", row)


if __name__ == "__main__":
    unittest.main()
