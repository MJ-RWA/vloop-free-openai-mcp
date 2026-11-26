#!/usr/bin/env python3
"""Small, deterministic audit of historical labeled mints against Solana RPC.

This is a reconstruction pilot, not a scorer backtest. It verifies mint-init
transactions and examines the following 15 slots for configured pool events.
"""
import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict

from solana_ingest import launch_mints, pool_info, RAYDIUM_V4

LABELS_URL = "https://raw.githubusercontent.com/Saba-Heydaridoost/rugpull_detector/main/dataset/rule_based_labels.csv"


def rpc(url, method, params, timeout=45):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read())
            if payload.get("error"):
                last = "rpc_error_" + str(payload["error"].get("code", "unknown"))
                if payload["error"].get("code") != 429:
                    return None, last
            else:
                return payload.get("result"), None
        except urllib.error.HTTPError as exc:
            last = "http_" + str(exc.code)
            if exc.code != 429:
                return None, last
        except (OSError, TimeoutError, ValueError):
            last = "transport_or_json_error"
        if attempt < 3:
            time.sleep(2 ** attempt)
    return None, last or "request_failed"


def load_cohort(per_class, seed):
    request = urllib.request.Request(LABELS_URL, headers={"User-Agent": "solana-radar-research-pilot"})
    with urllib.request.urlopen(request, timeout=60) as response:
        rows = list(csv.DictReader(line.decode("utf-8") for line in response))
    # One q1 row per mint; later q1 outcome label, never q1 feature columns.
    unique = {}
    for row in rows:
        if row.get("experiment_id") != "q1" or row.get("label_status") != "VALID":
            continue
        mint = row.get("token_mint", "")
        if mint and row.get("rug_pull_label") in ("0", "1"):
            unique[mint] = {"mint": mint, "created_at": int(row["token_creation_time"]),
                            "label": int(row["rug_pull_label"]),
                            "label_status": row["label_status"],
                            "label_source": "Early Rug Pull Detection on Solana, q1 rule-based label"}
    selected = []
    for label in (0, 1):
        group = sorted((x for x in unique.values() if x["label"] == label),
                       key=lambda x: (x["created_at"], x["mint"]))
        # Deterministic spread over the chronological cohort.
        if len(group) > per_class:
            indexes = [round(i * (len(group) - 1) / (per_class - 1)) for i in range(per_class)] if per_class > 1 else [len(group)//2]
            group = [group[i] for i in indexes]
        selected.extend(group)
    return sorted(selected, key=lambda x: (x["created_at"], x["mint"]))


def tx_signature(tx):
    signatures = (tx or {}).get("transaction", {}).get("signatures", [])
    return signatures[0] if signatures else None


def account_keys(tx):
    message = (tx or {}).get("transaction", {}).get("message", {})
    keys = message.get("accountKeys", [])
    return {x.get("pubkey") if isinstance(x, dict) else x for x in keys}


def find_mint_init(url, mint, max_pages=20):
    before = None
    inspected = 0
    for _ in range(max_pages):
        params = [mint, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        sigs, error = rpc(url, "getSignaturesForAddress", params)
        if error or not sigs:
            return None, inspected, error or "no_signatures"
        for item in sigs:
            inspected += 1
            sig = item.get("signature")
            if not sig:
                continue
            tx, tx_error = rpc(url, "getTransaction", [sig, {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0
            }])
            if tx_error or not tx:
                continue
            if mint in launch_mints(tx):
                return {"slot": int(item["slot"]), "signature": sig,
                        "block_time": item.get("blockTime")}, inspected, None
        before = sigs[-1].get("signature")
        if not before or len(sigs) < 1000:
            break
    return None, inspected, "mint_init_not_found_within_page_limit"


def inspect_window(url, mint, launch_slot, dex_programs):
    end = launch_slot + 15
    slots, error = rpc(url, "getBlocks", [launch_slot, end])
    if error or slots is None:
        return {"error": error or "getBlocks_empty", "produced_slots": []}
    summaries = []
    for slot in slots:
        block, block_error = rpc(url, "getBlock", [slot, {
            "encoding": "jsonParsed", "transactionDetails": "full",
            "rewards": False, "maxSupportedTransactionVersion": 0
        }])
        if block_error or not block:
            summaries.append({"slot": slot, "error": block_error or "block_unavailable"})
            continue
        matching = 0
        pool_events = []
        for entry in block.get("transactions", []):
            tx = entry.get("transaction", {})
            if mint in account_keys(tx) or any(x.get("mint") == mint for x in (entry.get("meta") or {}).get("postTokenBalances", [])):
                matching += 1
            pool = pool_info(entry, dex_programs)
            if pool and mint in pool.get("mints", set()):
                pool_events.append({"lp_mint": pool.get("lp_mint"),
                                    "vaults": pool.get("vaults", [])})
        if matching or pool_events:
            summaries.append({"slot": slot, "mint_related_transactions": matching,
                              "matching_pool_events": pool_events,
                              "transaction_count": len(block.get("transactions", []))})
    return {"produced_slots": slots, "slot_summaries": summaries}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rpc", default=os.environ.get("HELIUS_RPC_URL"))
    ap.add_argument("--per-class", type=int, default=4)
    ap.add_argument("--out", default="historical_reconstruction_pilot.json")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--dex-program", action="append", default=[])
    args = ap.parse_args()
    if not args.rpc:
        ap.error("HELIUS_RPC_URL must be configured as an environment secret")
    if not 1 <= args.per_class <= 10:
        ap.error("--per-class must be between 1 and 10")
    dex_programs = set(args.dex_program or [RAYDIUM_V4])
    cohort = load_cohort(args.per_class, 7)
    report = {
        "study": "bounded historical reconstruction feasibility pilot",
        "dataset": LABELS_URL,
        "label_definition": "q1 VALID binary rule-based outcome label; q1 features are not used",
        "sample_design": {"per_class": args.per_class, "selected": len(cohort),
                          "labels": {str(k): sum(x["label"] == k for x in cohort) for k in (0, 1)}},
        "dex_programs": sorted(dex_programs),
        "records": [],
        "warning": "Small feasibility sample only. No model scores or predictive metrics are computed."
    }
    for index, item in enumerate(cohort, 1):
        mint = item["mint"]
        init, inspected, error = find_mint_init(args.rpc, mint)
        row = {**item, "mint_init_signatures_inspected": inspected,
               "mint_init_error": error, "mint_init": init}
        if init:
            row["first_15_slot_window"] = inspect_window(args.rpc, mint, init["slot"], dex_programs)
        report["records"].append(row)
        print(f"pilot {index}/{len(cohort)} label={item['label']} init={'found' if init else 'not_found'}", flush=True)
    found = [x for x in report["records"] if x["mint_init"]]
    report["summary"] = {
        "sampled_mints": len(cohort),
        "mint_initializations_recovered": len(found),
        "windows_scanned": sum("first_15_slot_window" in x for x in found),
        "windows_with_pool_event": sum(
            any(s.get("matching_pool_events") for s in x.get("first_15_slot_window", {}).get("slot_summaries", []))
            for x in found),
        "label_counts_recovered": {str(k): sum(x["label"] == k for x in found) for k in (0, 1)}
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
