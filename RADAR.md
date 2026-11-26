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

The adapter includes a binary `Initialize2` decoder for Raydium AMM v4 (program ID shown above), in addition to parsed-instruction decoding for other venues. Repeat `--dex-program` for every additional venue. Add `--lock-program` for locker programs whose parsed lock instructions expose LP mint, amount and unlock timestamp; lock events are tracked after pool creation while the launch remains inside the 15-slot window. Pass `--calibration calibration.json` after running the backtest to apply the trained isotonic map to live scores. Without that artifact, output remains explicitly heuristic. The adapter joins mint and pool notifications by mint address in either arrival order, accepts only pool events within the 15-slot launch window, and keeps bounded in-flight correlation state across WebSocket reconnects within the running process (not across process restarts). The adapter emits JSON lines to stdout. Pipe them to a file if you want to retain live observations.

The adapter intentionally refuses to infer LP lock verification from an address or an untrusted claim. A lock counts only when a configured locker program returns a parsed lock instruction with the LP mint, amount and a future unlock timestamp, and the named destination token account is independently confirmed to hold at least that amount of the same LP mint. Locker formats that do not expose a verifiable destination token account remain unverified. Burn verification is deliberately conservative: it requires an observed LP mint-to amount plus an explicit successful parsed `burn`/`burnChecked` instruction for that LP mint. The adapter subscribes to Token and Token-2022 burn logs and joins later burn transactions to the observed pool by LP mint, provided the burn is observed within the 15-slot launch window. A current-supply delta alone is never accepted, because later minting followed by burning can make that net delta ambiguous. Observed successful burn transactions are accumulated per LP mint (bounded to the most recent 64 transactions) so multiple partial burns are counted cumulatively during the running process. Burns outside the live observation window, before the pool correlation is established, or missed during disconnects may be undercounted; burn history and correlation state are in-memory only. Unknown or unsupported venue layouts remain unverified.

### Coverage and honest limitations

Solana's standard JSON-RPC does not expose a universal decoded DEX-pool schema. Configure the exact venue program IDs and use parsed transaction instructions; some venues require a venue-specific decoder. The generic adapter cannot safely decode every binary DEX instruction. It cannot infer Jito bundle membership from ordinary RPC logs alone; it records same-slot buyer/funder concentration and same-wallet round trips, and leaves the explicit Jito bundle count at zero unless a provider-specific adapter supplies verified bundle metadata. Thin-liquidity/FDV and mutable metadata require a venue/metadata decoder and are not fabricated when unavailable.

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

Rows are sorted by launch slot; the first 70% are used to fit a pool-adjacent-violators isotonic map and the final 30% are a holdout. The report includes confusion counts, precision, recall, F1, Brier score and 10-bin ECE for raw and calibrated holdout predictions. The generated `calibration.json` is applied with:

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
