# Customer tagging — measured results, 10 September 2026

Small customer changes **were maintained incrementally**, and their work stayed
almost constant when the customer base grew tenfold. At one million customers,
incremental maintenance used 36% less CPU and wrote 97.6% fewer bytes than a full
rebuild. However, the five-refresh chain took longer than the single full-rebuild
job. This experiment supports delta-bounded maintenance, **not a claim of a large
reduction in the all-in cloud bill**.

The dedicated `BSS_BENCH` cluster is confirmed **SUSPENDED**. Both synthetic test
schemas remain available; no automatic BSS refresh schedule was enabled. Existing
xDR objects and schedules were not changed.

## Test and evidence

- Separate TMF-like customer, party, subscriber, account, subscription, offering,
  bill and ticket model, described in [README.md](README.md).
- Two populations: 100,000 customers / 150,000 subscriptions, and 1,000,000
  customers / 1,500,000 subscriptions.
- Dedicated GENERAL cluster, one CRU, one replica, 60-second auto-suspend.
- Consent and recurring-charge changes affecting 100 distinct customers per
  cycle: 0.1% and 0.01% of the respective populations. One warm-up, then five
  measured delta cycles per scale. Five repeated no-change cycles at 100k and
  one at 1M. Additional business-event tests ran at 100k.
- Refresh all five dynamic tables in dependency order, then rebuild the control
  directly from base tables using identical rules. The control does not reuse
  feature dynamic tables. Bidirectional result comparison plus cardinality and
  uniqueness checks passed in all 26 correctness snapshots, including bootstrap.
- Capture exact job IDs, job profiles, server execution times, refresh history,
  source row counts, flag transitions, EXPLAIN output, account rates and cluster
  state. Local business-logic/orchestration suite: 10 tests passed.

[Per-case CSV](measured_results.csv), [repeated-run summary](measured_summary.json)
are published aggregate outputs. Account-specific costs and raw evidence are
retained privately under `evidence/bss_tagging/` and are not published.

CPU seconds below are the job profile's `cpu_wall_time` nanosecond meter divided
by 1e9, summed once per job. They are not elapsed seconds or billed CRU-hours.
Input/output counters come from the top-level profile, without double-counting
nested stages. Logical input rows include intermediate/state processing and are
not unique customers. Server time is the sum of five refresh execution durations
versus the single full-control duration; client gaps and validation are excluded.

## Small-delta comparison

Medians of five measured cycles, excluding warm-up:

| Metric | 100k incremental | 100k full | 1M incremental | 1M full |
|---|---:|---:|---:|---:|
| Changed customers | 100 | 100 | 100 | 100 |
| CPU seconds | 2.964 | 2.907 | 3.070 | 4.798 |
| Server execution seconds | 5.520 | 1.423 | 5.571 | 1.476 |
| Logical input rows | 1,860 | 455,002 | 1,899 | 4,550,002 |
| Profile input bytes | 137,334,377 | 216,214,593 | 154,531,462 | 164,905,196 |
| Profile output bytes | 49,009 | 387,713 | 50,104 | 2,120,355 |

At 1M, logical input rows fell 99.96%, but input bytes fell only 6.3%. Cached
state access and fixed job overhead matter: the logical-row improvement must
not be presented as an equivalent physical-I/O or dollar saving. At 100k,
incremental CPU was approximately equal to full-rebuild CPU.

Increasing the base from 100k to 1M raised incremental CPU only 3.6% and logical
inputs 2.1% for the fixed 100-customer delta. Subscription features, customer
features and final tags each refreshed `INCREMENTAL`, retracting and inserting
100 customer-grain rows; unaffected billing and care features reported `NO_DATA`.
An updated tag row does not necessarily mean every flag changed.

At 1M, incremental server execution ranged from 4.592–6.771 seconds versus
1.091–1.751 seconds for full rebuild. Median time from the last source-write
commit to final tags was 15.097 seconds, including the harness's intervening
client overhead. Five observations do not establish a production freshness SLA;
the summary's nearest-rank p95 is simply the maximum of those five observations.
These sequential, warm-cluster tests are not randomized cold-cache or sustained
concurrency benchmarks.

## Where the work goes

For 1M customers and the same 100-customer changes:

| Component | Median CPU seconds | Interpretation |
|---|---:|---|
| Synthetic source updates, two jobs | 6.436 | Separate ingestion proxy, excluded from refresh comparison |
| Subscription features | 1.264 | Changed subscription/customer groups |
| Billing features | 0.001 | No relevant changes |
| Care features | 0.001 | No relevant changes |
| Joined customer features | 1.605 | Incremental customer-level join maintenance |
| Final tag evaluation | 0.179 | Six rule flags on changed feature rows |

Component medians need not sum to the median of the complete chain. The final
rules themselves are inexpensive; maintaining their inputs and issuing multiple
jobs account for most of the work. Source-update jobs took 3.102 server seconds
at the median and processed 1,031,814 logical input rows. This SQL update fixture
is not an optimized external CDC connector: its cost cannot be hidden when
discussing end-to-end economics.

One-time 1M fixture initialization consumed 32.372 profile CPU seconds across
11 captured seed/helper/control jobs. The first build of the five dynamic tables
consumed 12.320 CPU seconds and 6.465 summed server seconds. DDL, client overhead,
validation and idle capacity are additional; these figures are not an invoice
for bootstrap and are not part of steady-state delta medians.

## Correctness and limits

Payments, complaint closure, date-boundary ageing, subscription termination,
consent changes and customer deletion all matched fresh full recomputation.
The 100-customer deletion left exactly 99,900 customers and tag rows; dependent
source records were deliberately retained to exercise parent deletion behavior.

One offering-family change affected **80,000 customers**. Subscription features
remained incremental, but customer features and tags switched to **FULL**,
rebuilding 100,000 rows. Correct results, but not a small-delta cost case. Scope
the claim by affected customers, not by the number of changed source records.

All five tables reported `NO_DATA` during unchanged cycles. At 100k, median
combined CPU was 0.004 seconds with zero input/output bytes, versus 3.785 CPU
seconds for full rebuilding. Median server time was 0.560 seconds, with one
15.618-second outlier. Rounded-zero job CRU counters do not mean free billing:
an awakened cluster can still incur an activation minimum and idle tail.

The original acceptance gates therefore have a mixed outcome:

- Correctness, small-delta incrementality and fixed-delta scale behavior: pass.
- At least 80% less scan work **and active compute**: not met. Logical rows
  improved substantially, but CPU and input-byte improvements did not meet it.
- Equivalent production freshness SLA and lower isolated billed CRU-hours:
  not established by this short test or the currently available billing data.

`EXPLAIN REFRESH` encountered a join-optimizer error. Defining-SELECT EXPLAINs
were captured instead; successful refresh history is the evidence for actual
incrementality. Exact errors and minimal fixes are in [findings.md](../findings.md).

## Cost model — account-specific inputs withheld

Private account rates, billing records and account-derived dollar totals are
intentionally omitted from this public copy. The experiment's attributable
compute invoice had not posted at capture time. Resource measurements above
remain actual observations; the following dollar expressions are projections.

Let `R` be the reader's USD price per CRU-hour. For one CRU, a 60-second idle
tail, 15-minute cadence and 2,880 cycles per 30 days:

`modeled USD/cycle = (measured median server seconds + 60) × R / 3600`

| Refresh-only scenario | Work seconds | USD/cycle as a function of R | USD/30 days as a function of R |
|---|---:|---:|---:|
| 100k incremental | 5.520 | 0.018200 × R | 52.4160 × R |
| 100k full rebuild | 1.423 | 0.017062 × R | 49.1384 × R |
| 1M incremental | 5.571 | 0.018214 × R | 52.4568 × R |
| 1M full rebuild | 1.476 | 0.017077 × R | 49.1808 × R |
| 100k no-change refresh chain | 0.560 | 0.016822 × R | 48.4480 × R |

These exclude ingestion, between-job client gaps, validation, scheduling,
storage, network, consumption and background work. Do not add per-job proxies
to the same cluster's bill. Always-on one-CRU capacity would be `720 × R`
per 30 days. The fixed idle tail dominates: the five-job chain is slightly more
expensive than full rebuilding in this model despite lower CPU at 1M. Shared
capacity may benefit from lower CPU without lowering its bill immediately.

At 1M, divide the cycle projection by 100 for cost per customer reevaluation,
or by the observed median of 164 flag transitions for cost per transition.
Neither is a unique-monthly-customer cost. No-change cycles have no such
denominator. The retained private evidence can support later reconciliation;
this public report makes no claim about the actual account invoice.

### Storage and audience consumption

The 1M fixture contained 429,082,906 catalog-reported bytes across all benchmark
objects, including source/helper/control objects and hidden materialized state.
Derived tables plus internal state accounted for 73,071,845 bytes. Convert
bytes to GiB and apply your own storage rate; live snapshot metadata is not a
billed daily average and excludes any unrepresented retained versions or result
cache. Repetitive synthetic records compress well, so do not extrapolate these
sizes to realistic customer payloads.

A cross-sell audience query against 1M tag rows returned **100 synthetic customer
IDs** in 0.251 server seconds with 0.102 CPU seconds. Its one-CRU busy-capacity
proxy is `0.251 × R / 3600`. A 1,000-row export was not benchmarked; ten small
queries and one larger export need not have equivalent cost. No CRM push or
external delivery was performed. Snapshot audience retrieval is separate from
incremental delivery of tag changes.

The engine profile reports 101 internal output rows and a negative disk-byte
counter. Use the actual 100 SQL result rows, and do not infer physical I/O or
network charges from these counters.

## Reproduction and retained state

Run `python3 bss_tagging/analyze_measurements.py` to regenerate CSV and account-specific cost
summaries from your own local evidence without remote queries. Generated
`measured_costs.json` is private and ignored by Git. The remote measurement
driver is [measure.py](measure.py); executing it again can incur charges.
The `bss_tagging_100k` schema retains the edge-test end state, while
`bss_tagging_1m` retains the one-million-customer fixture and its delta changes.
No data was dropped during cleanup; the benchmark cluster was suspended.

Key raw evidence directories: `measured2_*`, `scale1m_*`, their corresponding
`profiles_*`, `observed_prices`, and `final_capture`. Within `final_capture`,
`000_usage.json` records the available billing window, `002_table_sizes.json`
records storage metadata, `005_recent_jobs.json` records server timings, and
`008_after_shutdown.json` confirms suspension. Raw evidence is required for an independent audit but is intentionally not
published here. Review and sanitize it before any separate authorized sharing.

Recommended next decision: reconcile posted billing, then test batched/shared
execution or fewer refresh jobs if a lower bill is the objective. No architecture
change or further spending was undertaken to force a favorable result.
