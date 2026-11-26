import unittest
from unittest.mock import patch
import backtest, solana_ingest

class BacktestTests(unittest.TestCase):
    def test_pav_is_monotone(self):
        k=backtest.pav([(0.1,1),(0.2,0),(0.3,1),(0.4,0)])
        ys=[v for _,v in k]
        self.assertEqual(ys,sorted(ys))
    def test_apply_steps(self):
        self.assertEqual(backtest.apply(.15,[[.2,.3],[1,.8]]),.3)
        self.assertEqual(backtest.apply(.9,[[.2,.3],[1,.8]]),.8)
    def test_metrics_counts_and_bounds(self):
        m=backtest.metrics([(.9,1),(.8,0),(.1,0),(.2,1)])
        self.assertEqual((m["tp"],m["fp"],m["tn"],m["fn"]),(1,1,1,1))
        self.assertTrue(0<=m["brier"]<=1 and 0<=m["ece"]<=1)

class IngestTests(unittest.TestCase):
    def test_mint_init_detection(self):
        tx={"transaction":{"message":{"instructions":[
          {"parsed":{"type":"initializeMint","info":{"mint":"Mint111"}}}]}}}
        self.assertEqual(solana_ingest.launch_mints(tx),["Mint111"])
    def test_pool_parser_uses_configured_program(self):
        tx={"transaction":{"message":{"instructions":[
          {"programId":"DEX","parsed":{"type":"initializePool","info":{
            "tokenAMint":"Mint111","tokenBMint":"Quote111","lpMint":"LP111",
            "tokenAVault":"Vault111"}}}]}}}
        p=solana_ingest.pool_info(tx,{"DEX"})
        self.assertEqual(p["lp_mint"],"LP111")
        self.assertIn("Mint111",p["mints"])
        self.assertEqual(p["vaults"],["Vault111"])
    def test_pool_parser_ignores_unconfigured_program(self):
        tx={"transaction":{"message":{"instructions":[
          {"programId":"OTHER","parsed":{"type":"initializePool","info":{"tokenMint":"Mint111"}}}]}}}
        self.assertIsNone(solana_ingest.pool_info(tx,{"DEX"}))
    def test_slot_metrics_counts_net_buys_and_launch_funders(self):
        def make(payer,delta):
            return {"transaction":{"message":{"accountKeys":[{"pubkey":payer}]}},
              "meta":{"preTokenBalances":[{"mint":"M","accountIndex":1,"uiTokenAmount":{"amount":"0"}}],
              "postTokenBalances":[{"mint":"M","accountIndex":1,"uiTokenAmount":{"amount":str(delta)}}]}}
        blocks={10:{"transactions":[make("A",100),make("B",200)]},
                11:{"transactions":[make("C",300)]}}
        with patch.object(solana_ingest,"rpc",side_effect=lambda u,m,p:blocks[p[0]]):
            r=solana_ingest.slot_metrics("rpc","M",10,11)
        self.assertEqual(r["slot0_buys"],2)
        self.assertEqual(r["total_early_buys"],3)
        self.assertEqual(r["unique_funders_in_slot0"],2)
        self.assertEqual(r["jito_bundle_count"],0)

if __name__=="__main__":unittest.main()
