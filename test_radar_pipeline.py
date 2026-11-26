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
          {"parsed":{"type":"initializeMint","info":{"mint":"Mint111111111111111111111111111111111"}}}]}}}
        self.assertEqual(solana_ingest.launch_mints(tx),["Mint111111111111111111111111111111111"])
    def test_pool_parser_uses_configured_program(self):
        tx={"transaction":{"message":{"instructions":[
          {"programId":"DEX","parsed":{"type":"initializePool","info":{
            "tokenAMint":"Mint111111111111111111111111111111111","tokenBMint":"Quote11111111111111111111111111111111","lpMint":"LP111111111111111111111111111111111",
            "tokenAVault":"Vault11111111111111111111111111111111"}}}]}}}
        p=solana_ingest.pool_info(tx,{"DEX"})
        self.assertEqual(p["lp_mint"],"LP111111111111111111111111111111111")
        self.assertIn("Mint111111111111111111111111111111111",p["mints"])
        self.assertEqual(p["vaults"],["Vault11111111111111111111111111111111"])
    def test_raydium_v4_initialize2_decoder(self):
        a=[f"Account{i}" for i in range(21)]
        tx={"transaction":{"message":{"instructions":[
          {"programId":solana_ingest.RAYDIUM_V4,"data":"2","accounts":a}]}}}
        p=solana_ingest.pool_info(tx,{solana_ingest.RAYDIUM_V4})
        self.assertEqual(p["lp_mint"],a[7])
        self.assertEqual(p["mints"],{a[8],a[9]})
        self.assertEqual(p["vaults"],[a[10],a[11]])
    def test_pool_parser_ignores_unconfigured_program(self):
        tx={"transaction":{"message":{"instructions":[
          {"programId":"OTHER","parsed":{"type":"initializePool","info":{"tokenMint":"Mint111111111111111111111111111111111"}}}]}}}
        self.assertIsNone(solana_ingest.pool_info(tx,{"DEX"}))
    def test_lp_burn_and_lock_require_onchain_proof(self):
        pool={"meta":{"err":None},"transaction":{"message":{"instructions":[
          {"parsed":{"type":"mintTo","info":{"mint":"LP","amount":"1000"}}},
          {"parsed":{"type":"burnChecked","info":{"mint":"LP","amount":"500"}}}]}}}
        lock={"transaction":{"message":{"instructions":[
          {"programId":"LOCK","parsed":{"type":"lock","info":{"lpMint":"LP","amount":"250","tokenAccount":"LOCKVAULT","unlockTime":1000+90*86400}}}]}}}
        def fake_rpc(url,method,params):
            if method=="getBlockTime":return 1000
            if method=="getAccountInfo":return {"value":{"data":{"parsed":{"info":{"mint":"LP","tokenAmount":{"amount":"250"}}}}}}
            raise AssertionError(method)
        with patch.object(solana_ingest,"rpc",side_effect=fake_rpc):
            burn,bv,locked,days,lv=solana_ingest.lp_proof("rpc",pool,"LP",{"LOCK"},5,lock)
        self.assertEqual(burn,50)
        self.assertTrue(bv)
        self.assertEqual((locked,days,lv),(25,90,True))

    def test_lp_lock_requires_matching_vault_balance(self):
        pool={"transaction":{"message":{"instructions":[
          {"parsed":{"type":"mintTo","info":{"mint":"LP","amount":"1000"}}}]}}}
        lock={"transaction":{"message":{"instructions":[
          {"programId":"LOCK","parsed":{"type":"lock","info":{"lpMint":"LP","amount":"250","tokenAccount":"VAULT","unlockTime":1000+90*86400}}}]}}}
        def fake_rpc(url,method,params):
            if method=="getTokenSupply":return {"value":{"amount":"1000"}}
            if method=="getBlockTime":return 1000
            if method=="getAccountInfo":return {"value":{"data":{"parsed":{"info":{"mint":"OTHER","tokenAmount":{"amount":"250"}}}}}}
            raise AssertionError(method)
        with patch.object(solana_ingest,"rpc",side_effect=fake_rpc):
            burn,bv,locked,days,lv=solana_ingest.lp_proof("rpc",pool,"LP",{"LOCK"},5,lock)
        self.assertFalse(lv)
        self.assertEqual((locked,days),(0.0,0))

    def test_lp_supply_delta_without_explicit_burn_is_unverified(self):
        pool={"transaction":{"message":{"instructions":[
          {"parsed":{"type":"mintTo","info":{"mint":"LP","amount":"1000"}}}]}}}
        burn,bv,locked,days,lv=solana_ingest.lp_proof("rpc",pool,"LP",set(),5)
        self.assertEqual(burn,0.0)
        self.assertFalse(bv)
        self.assertEqual((locked,days,lv),(0.0,0,False))

    def test_lp_burn_requires_explicit_matching_burn_instruction(self):
        pool={"transaction":{"message":{"instructions":[
          {"parsed":{"type":"mintTo","info":{"mint":"LP","amount":"1000"}}}]}}}
        burn_tx={"meta":{"err":None},"transaction":{"message":{"instructions":[
          {"parsed":{"type":"burnChecked","info":{"mint":"LP","amount":"300"}}}]}}}
        burn,bv,locked,days,lv=solana_ingest.lp_proof("rpc",pool,"LP",set(),5,burn_tx)
        self.assertEqual(burn,30)
        self.assertTrue(bv)
        self.assertEqual((locked,days,lv),(0.0,0,False))

    def test_failed_lp_burn_transaction_is_not_counted(self):
        pool={"transaction":{"message":{"instructions":[
          {"parsed":{"type":"mintTo","info":{"mint":"LP","amount":"1000"}}}]}}}
        burn_tx={"meta":{"err":{"InstructionError":[0,"Custom"]}},"transaction":{"message":{"instructions":[
          {"parsed":{"type":"burn","info":{"mint":"LP","amount":"300"}}}]}}}
        burn,bv,_,_,_=solana_ingest.lp_proof("rpc",pool,"LP",set(),5,burn_tx)
        self.assertEqual(burn,0.0)
        self.assertFalse(bv)
    def test_websocket_receive_reassembles_fragmented_json(self):
        class FakeSocket:
            def __init__(self,data):
                self.data=data
                self.sent=[]
            def recv(self,n):
                part=self.data[:n]
                self.data=self.data[n:]
                return part
            def sendall(self,data):
                self.sent.append(data)
        payload=b'{"jsonrpc":"2.0","result":7}'
        split=11
        frames=bytes([0x01,split])+payload[:split]+bytes([0x80,len(payload)-split])+payload[split:]
        sock=FakeSocket(frames)
        self.assertEqual(solana_ingest.ws_recv(sock),{"jsonrpc":"2.0","result":7})

    def test_websocket_receive_handles_ping_between_fragments(self):
        class FakeSocket:
            def __init__(self,data):
                self.data=data
                self.sent=[]
            def recv(self,n):
                part=self.data[:n]
                self.data=self.data[n:]
                return part
            def sendall(self,data):
                self.sent.append(data)
        payload=b'{"ok":true}'
        split=5
        frames=bytes([0x01,split])+payload[:split]+bytes([0x89,1])+b"x"+bytes([0x80,len(payload)-split])+payload[split:]
        sock=FakeSocket(frames)
        self.assertEqual(solana_ingest.ws_recv(sock),{"ok":True})
        self.assertEqual(len(sock.sent),1)
    def test_launch_correlator_matches_pool_before_mint(self):
        c=solana_ingest.LaunchCorrelator()
        pool={"lp_mint":"LP","vaults":[],"mints":{"M"}}
        tx={"transaction":{"signatures":["pool-sig"]}}
        self.assertIsNone(c.add_pool("M",pool,tx,105,105))
        match=c.add_mint("M",{"slot":100,"mint_tx":{}},100)
        self.assertEqual(match,({"slot":100,"mint_tx":{}},pool,tx,105))
        self.assertNotIn("M",c.pools)

    def test_launch_correlator_matches_mint_before_pool_and_expires(self):
        c=solana_ingest.LaunchCorrelator(window=15)
        launch={"slot":100,"mint_tx":{}}
        pool={"lp_mint":"LP","vaults":[],"mints":{"M"}}
        self.assertIsNone(c.add_mint("M",launch,100))
        self.assertEqual(c.add_pool("M",pool,{},115,115),(launch,pool,{},115))
        self.assertIsNone(c.add_mint("STALE",{"slot":100,"mint_tx":{}},100))
        c.add_pool("OTHER",pool,{},116,116)
        self.assertNotIn("STALE",c.mints)

    def test_slot_metrics_counts_net_buys_and_launch_funders(self):
        def make(payer,delta):
            return {"transaction":{"message":{"accountKeys":[{"pubkey":payer,"signer":True}]}},
              "meta":{"preTokenBalances":[{"mint":"M","accountIndex":1,"owner":payer,"uiTokenAmount":{"amount":"0"}}],
              "postTokenBalances":[{"mint":"M","accountIndex":1,"owner":payer,"uiTokenAmount":{"amount":str(delta)}}]}}
        blocks={10:{"transactions":[make("A",100),make("B",200)]},
                11:{"transactions":[make("C",300)]}}
        with patch.object(solana_ingest,"rpc",side_effect=lambda u,m,p:blocks[p[0]]):
            r=solana_ingest.slot_metrics("rpc","M",10,11)
        self.assertEqual(r["slot0_buys"],2)
        self.assertEqual(r["total_early_buys"],3)
        self.assertEqual(r["unique_funders_in_slot0"],2)
        self.assertEqual(r["jito_bundle_count"],0)

if __name__=="__main__":unittest.main()
