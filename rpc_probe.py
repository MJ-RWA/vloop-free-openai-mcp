#!/usr/bin/env python3
"""Rate-limit-aware probe for Solana Mainnet historical block/transaction RPC."""
import argparse
import json
import time
import urllib.error
import urllib.request


def call(url, method, params, timeout=30, retries=5):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    last = None
    for attempt in range(retries + 1):
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read())
            elapsed = round((time.monotonic() - started) * 1000)
            if "error" not in payload:
                return True, payload.get("result"), elapsed
            last = payload["error"]
            code = last.get("code") if isinstance(last, dict) else None
            if code != 429 and "rate" not in str(last).lower():
                return False, last, elapsed
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            elapsed = round((time.monotonic() - started) * 1000)
            last = {"http_status": exc.code, "detail": detail}
            if exc.code != 429:
                return False, last, elapsed
        except Exception as exc:
            return False, {"exception": str(exc)}, round((time.monotonic() - started) * 1000)
        if attempt < retries:
            delay = min(30, 2 ** attempt)
            print(f"  rate limited on {method}; waiting {delay}s (retry {attempt + 1}/{retries})", flush=True)
            time.sleep(delay)
    return False, last, 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default="https://api.mainnet-beta.solana.com")
    parser.add_argument("--days", nargs="+", type=int, default=[1, 7, 30, 90])
    parser.add_argument("--window", type=int, default=256, help="slot radius around each target date")
    args = parser.parse_args()

    print("RPC:", args.rpc)
    for method, params in (
        ("getHealth", []),
        ("getSlot", [{"commitment": "finalized"}]),
        ("getFirstAvailableBlock", []),
    ):
        ok, result, ms = call(args.rpc, method, params)
        print(f"{method}: {'OK' if ok else 'FAIL'} ({ms} ms): {str(result)[:400]}")
        if method == "getSlot" and ok:
            current_slot = result
        if method == "getFirstAvailableBlock" and ok:
            first_slot = result
    if "current_slot" not in locals():
        raise SystemExit("Could not obtain finalized slot; verify the endpoint and network.")

    for days in args.days:
        # Solana averages about 2.5 slots/second; the range lookup handles skipped slots.
        target = max(0, current_slot - int(days * 86400 / 0.4))
        lo, hi = max(0, target - args.window), target + args.window
        ok, slots, ms = call(args.rpc, "getBlocks", [lo, hi])
        if not ok:
            print(f"history_{days}d: INCONCLUSIVE getBlocks failed: {str(slots)[:300]}")
            continue
        if not slots:
            print(f"history_{days}d: NO PRODUCED BLOCK in slot range {lo}-{hi}; try a wider --window")
            continue
        slot = min(slots, key=lambda value: abs(value - target))
        ok, block, block_ms = call(args.rpc, "getBlock", [slot, {
            "encoding": "json", "transactionDetails": "full", "rewards": False,
            "maxSupportedTransactionVersion": 0
        }])
        if not ok or not block:
            print(f"history_{days}d: INCONCLUSIVE getBlock slot={slot} failed: {str(block)[:300]}")
            continue
        txs = block.get("transactions", [])
        signature = None
        if txs:
            signatures = txs[0].get("transaction", {}).get("signatures", [])
            if signatures:
                signature = signatures[0]
        if not signature:
            print(f"history_{days}d: BLOCK_OK slot={slot} tx_count={len(txs)} but no sample signature")
            continue
        tx_ok, tx_result, tx_ms = call(args.rpc, "getTransaction", [signature, {
            "encoding": "json", "commitment": "finalized", "maxSupportedTransactionVersion": 0
        }])
        print(f"history_{days}d: BLOCK_OK slot={slot} blockTime={block.get('blockTime')} "
              f"tx_count={len(txs)} getBlocks={ms}ms getBlock={block_ms}ms "
              f"getTransaction={'OK' if tx_ok else 'FAIL'} ({tx_ms}ms)")

    if "first_slot" in locals():
        print(f"retention_note: getFirstAvailableBlock={first_slot}; this alone does not prove full archival coverage.")
    print("Probe scope: one sampled block and one transaction near each requested age. "
          "This does not establish complete history, high-volume capacity, or unlimited retention.")


if __name__ == "__main__":
    main()
