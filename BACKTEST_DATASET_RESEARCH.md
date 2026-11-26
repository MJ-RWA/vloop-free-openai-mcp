# Historical dataset review: early Solana rug-pull benchmark

## Finding

A reproducible public research corpus is available in the authors' repository for **Early Rug Pull Detection on Solana**:

- Paper: https://arxiv.org/abs/2611.13342
- Code and released CSVs: https://github.com/Saba-Heydaridoost/rugpull_detector
- Dataset source: SolRPDS (CC BY 4.0): https://huggingface.co/datasets/DeFiLab/SolRPDS

The repository releases `dataset/rule_based_labels.csv` and `dataset/graph_nodes.csv`. The paper samples 2,700 SolRPDS mints and evaluates 750 s, 2,127 s, and 5,585 s observation windows. Labels require a post-cutoff liquidity drawdown greater than 80%, a subsequent price collapse greater than 90%, and persistent liquidity depletion for 30 minutes within a 24-hour outcome horizon. Samples whose rug event is already visible by the cutoff are excluded. These are operational market-outcome labels, not proof of criminal intent; slow rugs and freeze-only attacks are outside this label definition.

## Reproducible q1 cohort audit

The released CSVs were inspected directly from the repository on 2026-11-26:

- Labels file: 2,700 q1 rows; 1,962 VALID labels (765 positive, 1,197 negative); remaining rows are UNKNOWN or IN_WINDOW_EVENT.
- Feature file: 1,961 q1 rows with one row per mint; 765 positive and 1,196 negative. One valid label has no matching feature row.
- Feature cohort creation-time range: 2022-09-15 18:05:48 UTC through 2024-10-31 14:21:40 UTC.
- q1 observation cutoff: 750 seconds (12.5 minutes) after creation.

The feature file includes creator, mint/freeze authority, buy/sell-volume changes, unique-wallet counts and liquidity snapshots. It does not publish the first-15-slot holder distribution, top-10 concentration, LP burn/lock proof, slot-0 funder graph, or verified Jito bundle membership needed by this radar's event schema.

## Why this is not yet a valid backtest for this bounty

The bounty's decision deadline is **15 Solana slots**, not 750 seconds. Solana slots are much shorter than 750 seconds, so using q1 features would leak hundreds of seconds of activity beyond the required decision point. The released feature rows therefore must not be fed into the 15-slot scorer or described as its backtest.

The dataset is still useful as a source of token identifiers and independently collected later outcomes. A valid next-stage experiment must:

1. Recover the historical creation slot and first pool-creation slot for each mint.
2. Reconstruct only transactions and account state available through launch slot + 15.
3. Compute the scorer's supported early features from that bounded interval; leave unavailable evidence explicitly unknown rather than substituting q1 features.
4. Attach the later operational outcome label only after the 24-hour horizon and persistence confirmation are complete.
5. Exclude unknown labels and events already visible inside the 15-slot feature window.
6. Split chronologically by creation slot, keeping same-slot launches together, and report the exact usable cohort and holdout metrics.

Historical transaction reconstruction may require an archive RPC provider. The authors' paper reports that their own collection used Birdeye for market histories and Solana RPC for transfer histories. The released repository does not include edge-level transfer data, and its README says the external SolRPDS source file is not bundled. Do not claim the first-15-slot reconstruction is complete until it has actually been collected and checked.

## Provenance and caveats

- Research protocol and label definition: https://hub.valency.io/works/W_qjyrc47u_v1
- Authors' data/code repository: https://github.com/Saba-Heydaridoost/rugpull_detector
- SolRPDS source and CC BY 4.0 license: https://huggingface.co/datasets/DeFiLab/SolRPDS

The authors' repository does not include an explicit top-level LICENSE file. Do not redistribute its CSVs as part of this project unless the authors' data terms and upstream provider restrictions are clarified. Cite the paper and dataset source in any report using them.

**Current project status:** this is a viable labeled research lead and a concrete reconstruction plan, not a completed 15-slot backtest. The scorer remains heuristic until a compatible first-15-slot dataset has been reconstructed and evaluated on a chronological holdout.
