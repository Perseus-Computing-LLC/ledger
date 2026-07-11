# Plutus money-path concurrency proof — 2026-07

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

## A real bug the soak found (and fixed in this PR)

With the rate limiter off, saturation drove some ingest transactions past the 5 s
`busy_timeout`, and the handler's blanket `except Exception` returned **HTTP 400
"batch recording failed"**. A 400 tells the client "your request is malformed,
don't retry" — wrong for a transient, retryable server-side lock, and it would
make a well-behaved client drop a chargeable event.

Fixed in `server/app.py`: a `sqlite3.OperationalError` on the ingest path now
returns **HTTP 503 with `Retry-After`** (the batch rolled back under
`BEGIN IMMEDIATE`, so a retry is safe), while genuine bad input still returns 400.
This is the `busy_503` column above — 226 requests correctly told to retry, zero
misclassified as 400, ledger untouched throughout.

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

## Optional next step (human-gated): heavier soak on Lambda

A Linux cloud instance would push contention further (higher ephemeral-port
range, more cores, `SO_REUSEADDR`) and let the storm run to tens of thousands of
requests. The harness is ready — same command, larger `--threads/--requests`.

Not run here on purpose: the Lambda GPU credits are earmarked for the Perseus
Vault benchmark campaign, and this is a CPU driver-load test (no GPU needed). Per
the follow-up's cap, a Plutus soak instance should stay ≤ ~$1,000 of the credit
and terminate on completion (keep the persistent FS). Left for the human, who owns
the Lambda account and the credit split, to run if a bigger headline number is
wanted — the correctness proof above does not depend on it.
