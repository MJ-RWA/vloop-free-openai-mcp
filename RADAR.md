# SPL launch radar: operation, data contract and evaluation

## Components

- `spl_rug_radar.py` is the dependency-free heuristic scorer. Keep this file under 8,192 bytes.
- `solana_ingest.py` is a read-only JSON-RPC/WebSocket adapter. It subscribes to SPL Token and Token-2022 mint logs plus explicitly configured DEX program IDs.
- `backtest.py` fits an isotonic calibration map on the earlier chronological partition and reports metrics on a later holdout.
- `calibrated_scorer.py` applies the generated calibration artifact to one JSON event on stdin.

## Live ingestion

Python 3.12; no pip packages or wallet are needed. A public RPC endpoint is the default, but a dedicated RPC provider is recommended because holder enumeration and historical block reads can be rate-limited.

```sh
python solana_ingest.py \
  --rpc https://api.mainnet-beta.solana.com \
  --ws wss://api.mainnet-beta.solana.com \
  --dex-program 675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8 \
  --dex-program YOUR_OTHER_DEX_PROGRAM_ID \
  --calibration calibration.json
```

The adapter includes a binary `Initialize2` decoder for Raydium AMM v4 (program ID shown above), in addition to parsed-instruction decoding for other venues. Parsed pool mint fields are required for generic venues; the parser excludes the LP share mint and does not guess pool assets from every token balance in a router transaction, avoiding false launch correlations. Repeat `--dex-program` for every additional venue. Add `--lock-program` for locker programs whose parsed lock instructions expose LP mint, amount and unlock timestamp; configured locker-program notifications are joined by LP mint after pool creation and re-scored while the launch remains inside the 15-slot window. Pass `--calibration calibration.json` after running the backtest to apply the trained isotonic map to live scores. Without that artifact, output remains explicitly heuristic. The adapter joins mint and pool notifications by mint address in either arrival order, accepts only pool events within the 15-slot launch window, and keeps bounded in-flight correlation state across WebSocket reconnects within the running process (not across process restarts). The adapter emits JSON lines to stdout. Pipe them to a file if you want to retain live observations.

The adapter intentionally refuses to infer LP lock verification from an address or an untrusted claim. A lock counts only when a configured locker program returns a parsed lock instruction with the LP mint, amount and a future unlock timestamp, and the named destination token account is independently confirmed to hold at least that amount of the same LP mint. Locker formats that do not expose a verifiable destination token account remain unverified. Burn verification is deliberately conservative: it requires an observed LP mint-to amount plus an explicit successful parsed `burn`/`burnChecked` instruction for that LP mint. The adapter subscribes to Token and Token-2022 burn logs and joins later burn transactions to the observed pool by LP mint, provided the burn is observed within the 15-slot launch window. A current-supply delta alone is never accepted, because later minting followed by burning can make that net delta ambiguous. Observed successful burn transactions are accumulated per LP mint (bounded to the most recent 64 transactions) so multiple partial burns are counted cumulatively during the running process. Burns outside the live observation window, before the pool correlation is established, or missed during disconnects may be undercounted. The SQLite journal persists observed burns and already-correlated pool context; pending mint-only or pool-only correlation entries are not persisted. Unknown or unsupported venue layouts remain unverified.

### Coverage and honest limitations

Solana's standard JSON-RPC does not expose a universal decoded DEX-pool schema. Configure the exact venue program IDs and use parsed transaction instructions; some venues require a venue-specific decoder. The generic adapter cannot safely decode every binary DEX instruction. It cannot infer Jito bundle membership from ordinary RPC logs alone; it records same-slot buyer/funder concentration and same-wallet round trips, and leaves the explicit Jito bundle count at zero unless a provider-specific adapter supplies verified bundle metadata. Token-2022 transfer-fee configuration and mutable token-metadata flags are extracted when the RPC provider exposes parsed extension state; the larger configured older/newer fee is reported conservatively. Missing extension fields remain unknown; textual boolean values are parsed explicitly (`false` is never treated as true), and out-of-range transfer-fee values are ignored. Thin-liquidity/FDV still requires venue-specific reserve and valuation decoding and is not fabricated when unavailable.

LP burn observations are persisted in a local SQLite journal (`lp_burn_journal.sqlite3` by default; override with `--burn-journal`). Successful parsed burn transactions are deduplicated by signature and retained up to the latest 64 per LP mint. The journal bounds restored launch/pool associations to 10,000 LP mints and removes burn rows for evicted associations. The journal also stores the correlated launch/pool context, allowing the adapter to restore both the LP association and burn observations after restart or WebSocket reconnect; restored burns are aggregated only within the launch's 15-slot window. Keep the journal file with the deployment if continuity across restarts is required; deleting it discards that history. The journal stores parsed transaction responses locally and should be treated as operational data.

The adapter is read-only: it never signs or sends transactions. It requires an RPC endpoint that supports `logsSubscribe`, `getTransaction`, `getBlock`, `getProgramAccounts`, `getTokenSupply`, and `getTokenLargestAccounts`.

## Historical backtest and calibration

Input is newline-delimited JSON. Each row contains the scorer's normalized event fields plus `label` (1 = confirmed rug under the chosen label definition, 0 = non-rug). Include only features available no later than 15 slots after launch. Do not include post-launch outcome features in the event object.

Example shape:

```json
{"mint":"...","launch_slot":100,"event_slot":112,"mint_authority_active":true,"freeze_authority_active":false,"holders":{"walletA":500,"walletB":250},"lp_burn_pct":0,"lp_burn_verified":false,"label":1}
```

Run:

```sh
python backtest.py historical_launches.jsonl --min-samples 50 --calibration calibration.json
```

Rows are sorted by launch slot; the first 70% are used to fit a pool-adjacent-violators isotonic map and the final 30% are a holdout. The report includes confusion counts, precision, recall, F1, Brier score, 10-bin ECE, tie-aware ROC AUC and average precision for raw and calibrated holdout predictions. The chronological split keeps every launch from the same slot on one side of the boundary, preventing same-slot launches from leaking across train and holdout; the resulting split is approximately 70/30. The generated `calibration.json` is applied with:

```sh
echo '{"mint":"...","launch_slot":100,"event_slot":110}' | python calibrated_scorer.py calibration.json
```

Do not publish a performance claim until the input file, label definition, sample size, date range, class balance and holdout metrics are included with the submission. Synthetic fixtures are for unit tests only and are not a historical backtest. SolRPDS labels inactivity/liquidity-pool outcomes, which are a proxy and not equivalent to manually confirmed fraud; document that distinction if you use it.

## Tests and size gate

```sh
python -B -m unittest discover -s . -v
python -B -c "from pathlib import Path; p=Path('spl_rug_radar.py'); assert p.stat().st_size <= 8192, p.stat().st_size; print(p.stat().st_size)"
```

## Bounded historical mint discovery

```sh
python historical_collect.py --days 1 --max-blocks 100 \
  --out historical_mint_candidates.jsonl \
  --checkpoint historical_mint_checkpoint.json
```

The collector timestamp-aligns the requested lookback. In its default bounded
mode (`--max-blocks 100`), it deterministically samples at most that many
candidate slot numbers and calls `getBlock` only for those slots; skipped
slots can reduce the number of actual blocks fetched. This avoids indexing
the entire slot range before applying the limit. It resumes from a checkpoint
and appends deduplicated mint-initialization candidates to JSONL.
Use `--max-blocks 0` only when you explicitly intend exhaustive discovery:
that mode enumerates every produced slot using `getBlocks` chunks and may
require hundreds of RPC requests for a one-day window, so public endpoints
may rate-limit or time out. Bounded sampling is not exhaustive and can miss
launches.

Each output row contains the mint, launch slot, block time, transaction
signature and an intentionally null label. This is discovery output only:
sampling can miss launches, and a mint initialization alone does not establish
a DEX pool, a rug, or a safe launch. Do not feed these rows directly into
`backtest.py`. The next stage must associate candidates with configured
pool-creation transactions, reconstruct features available within 15 slots,
and attach independently reviewed outcome labels. Preserve signatures and
label provenance for auditability.


## Old Faithful historical RPC capability probe

Old Faithful can serve archived Solana blocks through its `faithful-cli rpc` JSON-RPC server. Remote CAR and index URIs are supported when the hosting endpoint accepts HTTP Range requests. However, the official RPC documentation notes that the GSFA index needed for full `getSignaturesForAddress` support cannot currently be used remotely. The default server build also does not enable `jsonParsed`; that requires the documented `make jsonParsed-linux` build target. See https://docs.old-faithful.net/usage/rpc-server and https://github.com/rpcpool/yellowstone-faithful.

Once an Old Faithful-compatible endpoint is running, probe representative historical slots before attempting a dataset reconstruction:

```powershell
$env:OLD_FAITHFUL_RPC_URL = "http://127.0.0.1:8899"
py -3.12 faithful_probe.py --slot 150000000 --slot 160000000 --timeout 90
```

Or pass `--rpc` directly. Add `--address <PUBKEY>` only to diagnose address-history lookup; failure of that method does not prevent the bounded slot-scan path. The probe tests `getVersion`, `getFirstAvailableBlock`, `getSlot`, `getBlocks`, `getBlockTime`, `getBlock`, and a sample `getTransaction`. It first tries `jsonParsed`, then falls back to raw `json` and reports which encoding worked. Raw JSON availability means the historical block is retrievable, but it does not by itself provide parsed SPL instruction fields; raw instruction decoding is still required if `jsonParsed` is unavailable.

Probe at least one known slot from each target epoch, including a slot whose signature and transaction metadata have been independently checked. A null response, unsupported method, missing transaction metadata, or a successful sample from only one epoch is not evidence of complete history. Save the JSON output with the reconstruction audit. Do not start large-scale collection until block coverage and transaction metadata are verified across the cohort's full date range.

## Helius historical RPC probe (user-run)

A user-run probe against a Helius Mainnet RPC endpoint reported successful `getHealth`, `getSlot`, and `getFirstAvailableBlock` calls. It sampled one block and one transaction at approximately 365, 730, 1,095, and 1,460 days before the probe anchor. All four sampled `getBlocks`/`getBlock`/`getTransaction` paths returned successfully:

| Approximate age | Sample slot | Transactions in sampled block | Sample transaction |
|---:|---:|---:|---|
| 365 days | 382,619,204 | 1,128 | OK |
| 730 days | 303,713,493 | 2,051 | OK |
| 1,095 days | 232,538,996 | 3,704 | OK |
| 1,460 days | 163,465,783 | 208 | OK |

The endpoint reported `getFirstAvailableBlock = 0`. This is encouraging evidence of deep historical access, but the probe sampled only four blocks and one transaction per block. It does not establish exhaustive retention, full transaction metadata, address-history support, `jsonParsed` support, throughput, or uninterrupted access across every target cohort date. Before bulk reconstruction, separately test `getSignaturesForAddress` on known addresses, `getBlock` with `jsonParsed` and raw `json`, versioned transactions, and several consecutive blocks around selected launch timestamps. Do not include the RPC URL or API key in committed files, logs, or reports. Rotate any credential exposed in chat before large-scale use.


## Repeatable Helius probe via GitHub Actions

The repository includes a manual workflow, `.github/workflows/historical-rpc-probe.yml`, and a credential-safe runner, `helius_history_probe.py`. To run it from the fork:

1. Rotate any RPC credential previously pasted into chat or otherwise exposed.
2. In the fork, open **Settings → Secrets and variables → Actions → New repository secret**.
3. Set the secret name to `HELIUS_RPC_URL` and its value to the rotated Mainnet RPC URL. Never put the URL in workflow inputs or commit it.
4. Open **Actions → Historical RPC capability probe → Run workflow** on this branch. The default slots repeat the four historical samples above; up to 20 comma-separated slots may be supplied.
5. Download the `historical-rpc-probe` artifact and inspect per-slot block retrieval, encoding, transaction metadata coverage, and token-balance rows.

The runner intentionally omits the endpoint URL, credentials, and provider response bodies from its report and logs. This is still a capability probe, not a dataset collector or a backtest. A successful run is only a gate to the next controlled reconstruction pilot; it does not establish exhaustive historical coverage or predictive performance.
