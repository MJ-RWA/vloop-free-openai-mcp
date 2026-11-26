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

from solana_ingest import (launch_mints, pool_info, RAYDIUM_V4, PUMP_AMM,
                           instructions, parsed_info, slot_metrics)
from spl_rug_radar import score_launch

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


def instruction_programs(tx):
    """Return invoked program IDs and parsed instruction types, including inner ix."""
    message = (tx or {}).get("transaction", {}).get("message", {})
    keys = [x.get("pubkey") if isinstance(x, dict) else x
            for x in message.get("accountKeys", [])]
    groups = [message.get("instructions", [])]
    groups.extend(x.get("instructions", [])
                  for x in (tx or {}).get("meta", {}).get("innerInstructions", []))
    programs, types = set(), set()
    for group in groups:
        for ix in group:
            program = ix.get("programId")
            if not program:
                index = ix.get("programIdIndex")
                if isinstance(index, int) and 0 <= index < len(keys):
                    program = keys[index]
            if isinstance(program, str):
                programs.add(program)
            parsed = ix.get("parsed")
            if isinstance(parsed, dict) and isinstance(parsed.get("type"), str):
                types.add(parsed["type"])
    return programs, types


def find_mint_init(url, mint, max_pages=20):
    # getSignaturesForAddress is newest-first. Page to the oldest retained
    # signatures first, then inspect transactions oldest-first. A mint account
    # normally cannot have a transaction before its initialization, so this
    # avoids thousands of unnecessary getTransaction calls on active mints.
    pages = []
    before = None
    for _ in range(max_pages):
        params = [mint, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        sigs, error = rpc(url, "getSignaturesForAddress", params)
        if error or not sigs:
            if not pages:
                return None, 0, error or "no_signatures"
            break
        pages.append(sigs)
        before = sigs[-1].get("signature")
        if not before or len(sigs) < 1000:
            break

    inspected = 0
    for page in reversed(pages):
        for item in reversed(page):
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
    return None, inspected, "mint_init_not_found_within_page_limit"


def inspect_window(url, mint, launch_slot, dex_programs):
    end = launch_slot + 15
    slots, error = rpc(url, "getBlocks", [launch_slot, end])
    if error or slots is None:
        return {"error": error or "getBlocks_empty", "produced_slots": []}
    summaries = []
    observed_programs = defaultdict(int)
    program_examples = []
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
            related = (mint in account_keys(entry) or
                       any(x.get("mint") == mint for x in (entry.get("meta") or {}).get("postTokenBalances", [])) or
                       any(x.get("mint") == mint for x in (entry.get("meta") or {}).get("preTokenBalances", [])))
            if related:
                matching += 1
                programs, types = instruction_programs(entry)
                for program in programs:
                    observed_programs[program] += 1
                if len(program_examples) < 50:
                    signatures = tx.get("signatures", [])
                    program_examples.append({
                        "slot": slot,
                        "signature": signatures[0] if signatures else None,
                        "program_ids": sorted(programs),
                        "instruction_types": sorted(types)
                    })
            pool = pool_info(entry, dex_programs)
            if pool and mint in pool.get("mints", set()):
                pool_events.append({"lp_mint": pool.get("lp_mint"),
                                    "vaults": pool.get("vaults", [])})
        if matching or pool_events:
            summaries.append({"slot": slot, "mint_related_transactions": matching,
                              "matching_pool_events": pool_events,
                              "transaction_count": len(block.get("transactions", []))})
    return {"produced_slots": slots, "slot_summaries": summaries,
            "observed_program_ids": dict(sorted(observed_programs.items())),
            "mint_related_transaction_examples": program_examples}



def reconstruct_features(url, mint, launch, window, dex_programs):
    """Build a strictly launch-to-15-slot feature row from historical block data.

    Holder balances are reconstructed from token-balance deltas in the scanned
    launch window, not from today's account state. Missing LP proof and price
    inputs remain explicitly unverified/unavailable.
    """
    launch_slot = launch["slot"]
    end_slot = launch_slot + 15
    account_state = {}
    mint_authority = None
    freeze_authority = None
    mutable_metadata = None
    transfer_fee_bps = 0
    lp_mints = set()
    lp_initial = defaultdict(int)
    lp_burned = defaultdict(int)
    lp_token_ops = []
    lp_vaults = []
    pool_event_count = 0
    for slot in window.get("produced_slots", []):
        block, error = rpc(url, "getBlock", [slot, {
            "encoding": "jsonParsed", "transactionDetails": "full",
            "rewards": False, "maxSupportedTransactionVersion": 0
        }])
        if error or not block:
            continue
        for entry in block.get("transactions", []):
            tx = entry.get("transaction", {})
            keys = tx.get("message", {}).get("accountKeys", [])
            key_list = [x.get("pubkey") if isinstance(x, dict) else x for x in keys]
            meta = entry.get("meta") or {}
            pre = meta.get("preTokenBalances", [])
            post = meta.get("postTokenBalances", [])
            for row in pre:
                if row.get("mint") != mint:
                    continue
                idx = row.get("accountIndex")
                if isinstance(idx, int) and idx < len(key_list):
                    address = key_list[idx]
                    account_state.setdefault(address, {
                        "amount": int(row.get("uiTokenAmount", {}).get("amount", 0)),
                        "owner": row.get("owner")
                    })
            for row in post:
                if row.get("mint") != mint:
                    continue
                idx = row.get("accountIndex")
                if isinstance(idx, int) and idx < len(key_list):
                    address = key_list[idx]
                    account_state[address] = {
                        "amount": int(row.get("uiTokenAmount", {}).get("amount", 0)),
                        "owner": row.get("owner")
                    }
            for ix in instructions(tx):
                typ, info = parsed_info(ix)
                typ = typ.lower()
                if typ in ("initializemint", "initializemint2") and info.get("mint") == mint:
                    mint_authority = info.get("mintAuthority") is not None
                    freeze_authority = info.get("freezeAuthority") is not None
                elif typ == "setauthority" and info.get("mint") == mint:
                    authority_type = str(info.get("authorityType", "")).lower()
                    if "mint" in authority_type:
                        mint_authority = info.get("newAuthority") is not None
                    elif "freeze" in authority_type:
                        freeze_authority = info.get("newAuthority") is not None
                if typ in ("mintto", "minttochecked", "burn", "burnchecked") and info.get("mint"):
                    try:
                        lp_token_ops.append((info["mint"], typ, int(info.get("amount", 0))))
                    except (ValueError, TypeError):
                        pass
            pool = pool_info(entry, dex_programs)
            if pool and mint in pool.get("mints", set()):
                pool_event_count += 1
                if pool.get("lp_mint"):
                    lp_mints.add(pool["lp_mint"])
                lp_vaults = pool.get("vaults", []) or lp_vaults
    # Apply observed LP mint/burn operations after discovering the LP mint
    # from a pool initialization anywhere in the same historical window.
    for token, typ, amount in lp_token_ops:
        if token not in lp_mints:
            continue
        if typ in ("mintto", "minttochecked"):
            lp_initial[token] += amount
        elif typ in ("burn", "burnchecked"):
            lp_burned[token] += amount
    holders = {}
    excluded = set(lp_vaults)
    for address, state in account_state.items():
        owner = state.get("owner")
        amount = state.get("amount", 0)
        if owner and address not in excluded and amount > 0:
            holders[owner] = holders.get(owner, 0) + amount
    burn_pct = 0.0
    burn_verified = False
    for lp_mint in lp_mints:
        minted = lp_initial[lp_mint]
        burned = lp_burned[lp_mint]
        if minted > 0 and burned > 0:
            burn_pct = max(burn_pct, min(100.0, 100.0 * burned / minted))
            burn_verified = True
    activity = slot_metrics(url, mint, launch_slot, end_slot)
    event = {
        "mint": mint, "launch_slot": launch_slot, "event_slot": end_slot,
        "mint_authority_active": bool(mint_authority),
        "freeze_authority_active": bool(freeze_authority),
        "metadata_mutable": bool(mutable_metadata) if mutable_metadata is not None else False,
        "transfer_fee_bps": transfer_fee_bps,
        "holders": holders, "lp_vaults": lp_vaults,
        "lp_burn_pct": burn_pct, "lp_burn_verified": burn_verified,
        "lp_lock_pct": 0.0, "lp_lock_days": 0, "lp_lock_verified": False,
        "initial_liq_sol": 0.0, "fdv_sol": 0.0,
        **activity
    }
    scored = score_launch(event)
    return {
        "features": event,
        "score": scored,
        "feature_quality": {
            "holder_balances": "reconstructed from pre/post token balances in produced slots",
            "lp_burn": "explicit LP mint/burn instructions only; otherwise unverified",
            "lp_lock": "not reconstructed; unverified",
            "initial_liquidity_and_fdv": "unavailable; zero defaults are not observations",
            "metadata_mutability": "false only when no positive mutable signal was decoded; interpret as incomplete",
            "pool_events_detected": pool_event_count,
            "lp_mints_detected": sorted(lp_mints),
            "scoring_warning": "Exploratory reconstruction only; incomplete fields can bias heuristic scores."
        }
    }


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
    dex_programs = set(args.dex_program or [RAYDIUM_V4, PUMP_AMM])
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
            try:
                row["reconstruction"] = reconstruct_features(
                    args.rpc, mint, init, row["first_15_slot_window"], dex_programs)
            except Exception as exc:
                row["feature_reconstruction_error"] = type(exc).__name__
        report["records"].append(row)
        print(f"pilot {index}/{len(cohort)} label={item['label']} init={'found' if init else 'not_found'}", flush=True)
    found = [x for x in report["records"] if x["mint_init"]]
    feature_rows = []
    for record in report["records"]:
        reconstruction = record.get("reconstruction")
        if not reconstruction:
            continue
        feature_rows.append({
            **reconstruction["features"],
            "label": record["label"],
            "label_source": record["label_source"],
            "mint": record["mint"],
            "feature_quality": reconstruction["feature_quality"]
        })
    report["feature_rows"] = feature_rows
    report["summary"] = {
        "feature_rows_reconstructed": len(feature_rows),
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
