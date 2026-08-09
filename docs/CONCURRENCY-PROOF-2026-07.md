# Ledger money-path concurrency proof — 2026-07

Closes review finding **P2** and the roadmap exit criteria for #28/#30 ("a
concurrency/load test on the ingest + webhook paths proves no double-count, no
lost writes, correct balances"). Until now that was *asserted* — every atomicity
test was single-threaded (a crash simulated by monkeypatch + serial replay). This
proves it under real contention.

## What is proven

The invariant that catches both failure modes at once:

> **final balance == grant + topups − refunds − (recorded debits × cost)**, exact
> to the micro-dollar.

If two concurrent debits read the same stale balance (a **lost write**), or an
idempotent retry records twice (a **double count**), this equality breaks. It
holds under saturation. Plus: `usage_events` count == recorded debits (no
double-count on ingest), and an identical-`Idempotency-Key` burst charges exactly
once.

## Harness

`tools/concurrency_soak.py` boots the real threaded `ThreadingHTTPServer`
(connection-per-request) on a temp DB with one pro org, then:

- **Ingest storm:** N threads, each holding one keep-alive connection, fire
  `requests_per` `POST /v1/usage` debits at a single org concurrently.
- **Reversals racing debits:** a set of `charge.refunded` webhooks reverse seeded
  top-ups *during* the storm, each fired twice (distinct event ids) to prove a
  replayed reversal converges and never double-reverses.
- **Idempotency burst:** K threads POST the same body with one shared
  `Idempotency-Key`, racing the crash-reclaim window.

Then it reads the balance back in integer micro-dollars and checks the invariant.
It exits non-zero on any violation, so it is a real assertion at any scale. The
bounded version runs in CI (`tests/test_concurrency.py`, 3 tests); the heavy soak
runs from the CLI.

## Measured result (local, 2026-07-11)

Host: 16-core Windows box. Rate limiter disabled (`--rate-per-min 0`) so every
request reaches the DB writer path rather than being shed by the limiter (which
is tested separately). Raw output committed at
[`docs/exhibits/concurrency-soak-2026-07.json`](exhibits/concurrency-soak-2026-07.json).

Command:
```
python tools/concurrency_soak.py --threads 96 --requests 80 \
    --with-reversals --idempotency-threads 64 --rate-per-min 0
```

Ingest storm:

| metric | value |
|---|---|
| threads × requests | 96 × 80 = **7,680** attempted debits |
| recorded debits | 7,454 |
| retryable-busy (503) | 226 |
| hard errors (5xx/4xx) | **0** |
| client connection errors | 0 |
| concurrent top-ups / refunds | 96 / 48 (each refund fired twice) |
| `usage_events` count | 7,454 (== recorded → **no double-count**) |
| expected balance | $246.26 |
| **actual balance** | **$246.26** (exact micro-dollar match) |
| wall time | ~40.6 s |

Idempotency burst: 64 threads, one shared key → **1** event recorded, charged
exactly once, balance exact.

`SOAK OK`. The money invariant held exactly while 7,680 debits, 96 top-ups, and 96
refund events (48 × 2) contended on one org.

## Cloud run — Lambda A10, 30 vCPU (2026-07-11)

Re-run on a Lambda Cloud `gpu_1x_a10` (30 vCPU, Ubuntu 22.04, Linux) to push
contention past what a laptop can reach: **500 concurrent keep-alive connections**
hammering one org. Raw output committed at
[`docs/exhibits/concurrency-soak-lambda-500way-2026-07.json`](exhibits/concurrency-soak-lambda-500way-2026-07.json)
and [`…-reversals-2026-07.json`](exhibits/concurrency-soak-lambda-reversals-2026-07.json).

Run 1 — 500-way ingest storm (`--threads 500 --requests 60 --rate-per-min 0`):

| metric | value |
|---|---|
| attempted / recorded debits | 30,000 / 19,906 |
| retryable-busy (503) | 9,480 |
| client connection errors | 614 |
| hard errors | **0** |
| `usage_events` == recorded | yes (**no double-count**) |
| expected vs actual balance | **$200.94 == $200.94** (exact) |
| idempotency burst (256 threads) | charged exactly once |

Run 2 — debits racing concurrent reversals (`--threads 200 --requests 50
--with-reversals`): 9,543 recorded debits + 200 top-ups + 100 refunds (each fired
twice), balance **$404.57 == $404.57** exact, no double-count, idempotency once,
0 hard errors.

The single-node SQLite ledger is fsync-bound (~100 durable commits/sec on this
disk), so most of the 500-way storm is correctly shed as retryable 503 rather
than queued — and the money invariant holds exactly on every recorded debit
regardless.

## Two real bugs the soak found (both fixed in this PR)

**1. Ingest lock-timeout returned a non-retryable 400.** With the limiter off,
saturation drove some ingest transactions past the 5 s `busy_timeout`, and the
ingest handler's blanket `except Exception` returned **HTTP 400 "batch recording
failed"**. A 400 says "malformed, don't retry" — wrong for a transient lock (the
batch rolled back under `BEGIN IMMEDIATE`, so a retry is safe), and a well-behaved
client would drop a chargeable event. Fixed: a `sqlite3.OperationalError` on the
ingest path now returns **503 + `Retry-After`**; genuine bad input still 400s.

**2. Lock *outside* the ingest transaction returned a 500.** The 500-way cloud
run surfaced 5 / 30,000 requests returning **HTTP 500 "internal error"** — a lock
lost in the pre-transaction path (the API-key auth read / idempotency replay),
which the outer request handler mapped to a generic 500 (non-retryable, alarming).
Fixed: the outer POST handler now maps `sqlite3.OperationalError` to the same
retryable **503 + `Retry-After`**. The re-run after the fix reported **0 hard
errors** (the 5 became clean 503s), balance still exact.

In both cases the ledger stayed exact throughout — the bugs were about returning
the *right status* for a transient lock, never about money correctness. Both
paths have deterministic tests in `tests/test_concurrency.py`.

## Honest limitations

- **This is a driver-load test on one box, not a distributed one.** True
  parallelism is bounded by the GIL and by SQLite's single-writer model (writers
  serialize — which is exactly the property under test). It proves correctness
  under contention, not peak throughput.
- **Windows ephemeral-port ceiling.** An earlier fresh-socket-per-request driver
  exhausted the Windows ephemeral port range (WinError 10048) in the thousands of
  rapid connects. The harness now reuses one keep-alive connection per thread,
  which both removes the artifact and models real clients; the numbers above ran
  clean with `client_conn_errors: 0`.

## Scaling further

The single-node SQLite ledger's durable-commit rate (~100/sec here) is the
throughput ceiling, so raw request counts scale with wall time, not core count;
the value of the cloud run is the **500-way concurrency**, not volume. To push
harder, raise `--threads` (contention) rather than `--requests` (which just adds
fsync-bound wall time). A multi-node / Postgres-backed deployment would be the
next frontier for throughput, but the correctness invariant proven here is
independent of scale.
