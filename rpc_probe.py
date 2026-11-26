#!/usr/bin/env python3
"""Probe Solana JSON-RPC health, recent history, and transaction retrieval."""
import argparse
import json
import time
import urllib.error
import urllib.request


def call(url, method, params, timeout=25):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read())
            elapsed = round((time.monotonic() - started) * 1000)
            if "error" in payload:
                return False, payload["error"], elapsed
            return True, payload.get("result"), elapsed
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace")
        return False, {"http_status": exc.code, "detail": detail}, round((time.monotonic() - started) * 1000)
    except Exception as exc:
        return False, {"exception": str(exc)}, round((time.monotonic() - started) * 1000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default="https://api.mainnet-beta.solana.com")
    parser.add_argument("--days", nargs="+", type=int, default=[1, 7, 30, 90])
    args = parser.parse_args()

    print("RPC:", args.rpc)
    for method in ("getHealth", "getSlot", "getFirstAvailableBlock"):
        params = [{"commitment": "finalized"}] if method == "getSlot" else []
        ok, result, ms = call(args.rpc, method, params)
        print(f"{method}: {'OK' if ok else 'FAIL'} ({ms} ms): {str(result)[:500]}")
        if method == "getSlot" and ok:
            current_slot = result
        if method == "getFirstAvailableBlock" and ok:
            first_slot = result
    if "current_slot" not in locals():
        raise SystemExit("Could not obtain finalized slot; check endpoint/network and retry.")

    # Approximate target: 2.5 slots/second. Walk backward to skip unproduced slots.
    for days in args.days:
        target = max(0, current_slot - int(days * 86400 / 0.4))
        found = None
        for offset in range(65):
            slot = target - offset
            if slot < 0:
                break
            ok, result, ms = call(args.rpc, "getBlock", [slot, {
                "encoding": "json", "transactionDetails": "full", "rewards": False,
                "maxSupportedTransactionVersion": 0
            }])
            if ok and result:
                found = (slot, result, ms)
                break
        if not found:
            print(f"history_{days}d: FAIL (no block found near target slot {target}; last response {str(result)[:300]})")
            continue
        slot, block, ms = found
        txs = block.get("transactions", [])
        sig = None
        if txs:
            sigs = txs[0].get("transaction", {}).get("signatures", [])
            if sigs:
                sig = sigs[0]
        tx_ok = False
        tx_result = "no transaction signature in sampled block"
        if sig:
            tx_ok, tx_payload, tx_ms = call(args.rpc, "getTransaction", [sig, {
                "encoding": "json", "commitment": "finalized", "maxSupportedTransactionVersion": 0
            }])
            tx_result = f"{'OK' if tx_ok else 'FAIL'} ({tx_ms} ms)"
        print(f"history_{days}d: OK slot={slot} blockTime={block.get('blockTime')} tx_count={len(txs)} getBlock={ms}ms getTransaction={tx_result}")

    if "first_slot" in locals():
        print(f"retention_note: getFirstAvailableBlock={first_slot}; this does not prove every historical slot/transaction is queryable.")
    print("Note: this probes only the tested dates/methods; it does not prove full archival coverage or sustained rate capacity.")


if __name__ == "__main__":
    main()
