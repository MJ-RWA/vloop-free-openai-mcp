import unittest
from unittest.mock import patch

import historical_reconstruction_pilot as pilot


class HistoricalProgramDiscoveryTests(unittest.TestCase):
    def test_collects_top_level_and_inner_program_ids(self):
        tx = {
            "transaction": {"transaction": {"signatures": ["sig"]},
                "message": {
                    "accountKeys": [{"pubkey": "OWNER"}, {"pubkey": "DEX_A"}],
                    "instructions": [
                        {"programId": "DEX_A", "parsed": {"type": "initializePool"}}
                    ]
                }},
            "meta": {"innerInstructions": [{"index": 0, "instructions": [
                {"programId": "DEX_B", "parsed": {"type": "transfer"}}
            ]}]}
        }
        programs, types = pilot.instruction_programs(tx)
        self.assertEqual(programs, {"DEX_A", "DEX_B"})
        self.assertEqual(types, {"initializePool", "transfer"})

    def test_resolves_compiled_program_id_index(self):
        tx = {
            "transaction": {"message": {
                "accountKeys": ["OWNER", "DEX_COMPILED"],
                "instructions": [{"programIdIndex": 1}]
            }},
            "meta": {"innerInstructions": []}
        }
        programs, types = pilot.instruction_programs(tx)
        self.assertEqual(programs, {"DEX_COMPILED"})
        self.assertEqual(types, set())


    @patch("historical_reconstruction_pilot.rpc")
    def test_mint_init_search_checks_oldest_transactions_first(self, mocked_rpc):
        mint = "MINT_ADDRESS"
        newest_page = [
            {"signature": f"newer-{i}", "slot": 3000 - i, "blockTime": 3000 - i}
            for i in range(999)
        ] + [{"signature": "before-cursor", "slot": 2000, "blockTime": 2000}]
        oldest_page = [
            {"signature": "oldest", "slot": 10, "blockTime": 100},
        ]

        def rpc_side_effect(url, method, params):
            if method == "getSignaturesForAddress":
                if len([x for x in mocked_rpc.call_args_list if x.args[1] == "getSignaturesForAddress"]) == 1:
                    return newest_page, None
                return oldest_page, None
            if method == "getTransaction":
                self.assertEqual(params[0], "oldest")
                return {
                    "transaction": {"message": {"instructions": [{
                        "parsed": {"type": "initializeMint2", "info": {"mint": mint}}
                    }]}}
                }, None
            self.fail("unexpected RPC method: " + method)

        mocked_rpc.side_effect = rpc_side_effect
        result, inspected, error = pilot.find_mint_init("https://rpc.invalid", mint)
        self.assertIsNone(error)
        self.assertEqual(inspected, 1)
        self.assertEqual(result, {"slot": 10, "signature": "oldest", "block_time": 100})
        self.assertEqual(mocked_rpc.call_count, 3)


if __name__ == "__main__":
    unittest.main()
