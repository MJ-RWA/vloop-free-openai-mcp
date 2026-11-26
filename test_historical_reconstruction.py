import unittest

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


if __name__ == "__main__":
    unittest.main()
