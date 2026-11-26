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
  --dex-program YOUR_DEX_PROGRAM_ID
```

Repeat `--dex-program` for every supported venue and `--lock-program` for locker programs whose parsed lock instructions expose LP mint, amount and unlock timestamp. The adapter emits JSON lines to stdout. Pipe them to a file if you want to retain live observations.

The adapter intentionally refuses to infer LP lock verification from an address or an untrusted claim. A lock counts only when a configured locker program returns a parsed lock instruction with the LP mint, amount and a future unlock timestamp. Burn verification requires an observed LP mint-to amount and an RPC-confirmed current supply. Unknown or unsupported venue layouts remain unverified.

### Coverage and honest limitations

Solana's standard JSON-RPC does not expose a universal decoded DEX-pool schema. Configure the exact venue program IDs and use parsed transaction instructions; some venues require a venue-specific decoder. The generic adapter cannot safely decode every binary DEX instruction. It also cannot infer Jito bundle membership from ordinary RPC logs alone; it records same-slot buyer/funder concentration, and leaves the explicit Jito bundle count at zero unless a future provider-specific adapter supplies verified bundle metadata. Thin-liquidity/FDV and mutable metadata require a venue/metadata decoder and are not fabricated when unavailable.

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
