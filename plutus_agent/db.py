"""SQLite data layer — multi-tenant schema + access functions.

Hierarchy: **organization → workspace**, with **users** belonging to an org.
Usage is metered per (org, workspace, provider, model, task_type). Prepaid
credit is an append-only ledger; the org balance is the sum of its deltas, so it
is always auditable and can never silently drift.

All money is stored as **integer micro-dollars** (1 USD = 1_000_000 micros) in
columns suffixed ``_micros``. Integers sum exactly in SQLite, so a large
``SUM(delta_micros)`` never accumulates the sub-cent float drift that a REAL
ledger would — we convert to float USD only once, at the read boundary, via the
``micros_to_usd`` helper. The public Python API of this module still speaks
float USD; the integer representation is an internal storage detail. All
timestamps are Unix epoch seconds (matching ``plutus.py``'s ``state.db``).

The connection uses WAL + a row factory returning ``sqlite3.Row`` so callers get
dict-like rows. Nothing here imports Flask/Stripe — it's pure stdlib and works
fully offline.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional

# Bump on every schema change; stamped into meta('schema_version'). Within the
# 1.0 line, changes are ADDITIVE only (new tables / nullable or defaulted columns)
# so an older reader keeps working — see docs/schema.md for the forward-compat
# contract. 5 = adds the ingest_idempotency table (#65).
# 6 = adds the usage_events hash chain (prev_hash/row_hash) for tamper-evidence (#108).
# 7 = adds usage_events.baseline_micros (savings-share counterfactual) + the
#     savings_invoices table (per-org/period savings-share billing).
# 8 = adds usage_events.optimal_micros (efficiency-leakage counterfactual: the
#     cheapest policy-passing option; actual > optimal = missed savings / off-policy).
# 9 = adds the chain_checkpoints table (externally-retained signed anchors) so the
#     tamper-evidence chain is INDEPENDENTLY verifiable — a customer-held checkpoint
#     pins a past head, catching a full-chain recompute the operator could otherwise
#     hide. See _CHECKPOINT_FIELDS / checkpoint_chain / verify_checkpoints (#120).
# 10 = adds usage_events.external_ref (per-task/per-question attribution id, e.g. an
#     Invarium task_id) + ix_usage_extref. Nullable and hash-chained as an optional
#     trailing field, so pre-v10 rows still verify byte-identically.
# 11 = adds usage_events.cache_write_tokens for provider cache-creation billing.
#     Nullable and hash-chained as an optional trailing field for the same reason.
# 12 = adds nullable usage_events.user_id and users.active for Team attribution /
#     seat billing. Both are additive; user_id is an optional chain field.
# 13 = adds organizations.stripe_subscription_id for Team seat quantity sync.
# 14 = adds api_keys.scope (JSON, org/workspace restrictions), api_keys.event_count
#     (usage counter), api_keys.rotation_of (rotation chain ref), and the
#     ingest_health table (per-source ingestion diagnostics). (#150)
SCHEMA_VERSION = 14

# ---- money: integer micro-dollars ------------------------------------------
# All money is stored as integer micro-dollars (1 USD == MICROS_PER_USD micros).
# Convert at the boundary only: integers accumulate exactly in SQL, so we incur
# a single rounding when crossing back to float USD for display / Stripe.
MICROS_PER_USD = 1_000_000


def usd_to_micros(usd) -> int:
    """Convert a float/Decimal/str USD amount to integer micro-dollars.

    Rounds to the nearest micro using banker-safe ``round`` on a scaled value.
    ``None`` maps to ``None`` so nullable money columns round-trip cleanly.
    """
    if usd is None:
        return None
    return int(round(float(usd) * MICROS_PER_USD))


def micros_to_usd(micros) -> float:
    """Convert integer micro-dollars back to float USD. ``None`` -> ``None``."""
    if micros is None:
        return None
    return micros / MICROS_PER_USD


# ---- tamper-evidence hash chain (#108) --------------------------------------
# The ledger is integer-exact and re-queryable, but was append-only *by
# convention* only — an operator with DB access could rewrite history
# undetectably. We chain every ``usage_events`` row: each row carries the hash
# of the previous row for the same org, so modifying, deleting, reordering, or
# inserting an event breaks the chain from that point on and ``verify_chain``
# reports the first divergence. This reuses the design Perseus Vault shipped for
# its memory audit trail (SHA-256 chain + optional keyed MAC).
#
# The chain is PER ORG (the two-party billing unit): each customer can verify
# their own stream independently. Ordering is by SQLite ``rowid`` (monotonic
# with insertion) since ``usage_events`` is append-only.
CHAIN_GENESIS = "plutus-usage-chain/v1"

# Immutable columns that define an event, in a fixed order. The hash covers all
# of them, so tampering with any (notably ``cost_micros``) is detectable.
_CHAIN_FIELDS = (
    "id", "org_id", "workspace_id", "provider", "model", "task_type",
    "input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens",
    "cost_micros", "estimated", "source", "ts",
)

# Optional trailing chain fields, added after v6. Each is appended to the
# canonical form ONLY when its value is present (not None). This keeps rows that
# predate the field (baseline_micros IS NULL) byte-identical to the old v6
# canonical form, so chains written before schema 7 still verify — while any row
# that DOES carry a baseline hashes it, making a billed savings figure just as
# tamper-evident as ``cost_micros``. (#7: savings-share.)
_CHAIN_FIELDS_OPTIONAL = (
    "baseline_micros",
    "optimal_micros",
    # #20-arc shape A: per-task attribution ref. Appended last so rows without
    # it (external_ref IS NULL) stay byte-identical to the pre-v10 canonical
    # form; a row that carries it hashes it, so a billed saving can't be
    # re-pointed to a different task undetected.
    "external_ref",
    # #136: optional per-user attribution, trailing to preserve old chains.
    "user_id",
    # Anthropic cache-creation input tokens (#135). Optional trailing field keeps
    # pre-v11 canonical forms unchanged while making cache-write usage immutable.
    "cache_write_tokens",
)


def _chain_scalar(value) -> str:
    """Deterministic string form of a column value for hashing.

    Floats use ``repr`` so a value round-tripped through SQLite REAL hashes
    identically at write time and at verify time; ``None`` is a distinct empty
    token so a NULL never collides with an empty string.
    """
    if value is None:
        return "\x00"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _canonical_event(fields: dict) -> str:
    # Unit separator between fields; the key name is included so a value can
    # never migrate across columns without changing the digest.
    parts = [f"{k}={_chain_scalar(fields.get(k))}" for k in _CHAIN_FIELDS]
    # Optional trailing fields are appended only when present, so a NULL (an
    # event with no recorded baseline) yields exactly the pre-v7 canonical form.
    for k in _CHAIN_FIELDS_OPTIONAL:
        v = fields.get(k)
        if v is not None:
            parts.append(f"{k}={_chain_scalar(v)}")
    return "\x1f".join(parts)


def compute_row_hash(prev_hash: Optional[str], fields: dict,
                     hmac_key: Optional[bytes] = None) -> str:
    """Hash one event, chained onto ``prev_hash`` (or the genesis constant).

    With ``hmac_key`` set, uses HMAC-SHA256 so only a holder of the key (held by
    the customer, not the operator) can produce a valid chain — this is the
    two-party property. Without it, plain SHA-256 (offline/self-hosted default).
    """
    payload = ((prev_hash or CHAIN_GENESIS) + "\x1e" + _canonical_event(fields)).encode("utf-8")
    if hmac_key:
        import hmac as _hmac
        return _hmac.new(hmac_key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def chain_head(conn, org_id: str) -> Optional[str]:
    """The most recent (highest rowid) ``row_hash`` for an org, or ``None``.

    Called inside ``record_usage``'s transaction; under ``BEGIN IMMEDIATE`` the
    write lock is held, so read-head + insert cannot interleave with another
    writer, and rows inserted earlier in the same batch are visible here.
    """
    row = conn.execute(
        "SELECT row_hash FROM usage_events "
        "WHERE org_id=? AND row_hash IS NOT NULL ORDER BY rowid DESC LIMIT 1",
        (org_id,),
    ).fetchone()
    return row["row_hash"] if row else None


def verify_chain(conn, org_id: Optional[str] = None,
                 hmac_key: Optional[bytes] = None) -> dict:
    """Walk the usage_events hash chain and report the first divergence per org.

    Returns ``{"ok": bool, "orgs": [ {org_id, events, verified, pre_chain,
    status, first_divergence} ]}``. ``status`` is ``"ok"`` (chain intact),
    ``"broken"`` (a divergence was found), or ``"empty"`` (no events). Rows with
    a NULL ``row_hash`` predate the chain and are counted in ``pre_chain`` and
    reported as unverifiable rather than treated as a failure — unless they
    appear *after* a chained row, which is itself a divergence (a hash was
    stripped).
    """
    if org_id is not None:
        org_ids = [org_id]
    else:
        org_ids = [r["org_id"] for r in conn.execute(
            "SELECT DISTINCT org_id FROM usage_events ORDER BY org_id").fetchall()]

    orgs = []
    all_ok = True
    for oid in org_ids:
        rows = conn.execute(
            "SELECT rowid AS _rowid, * FROM usage_events WHERE org_id=? ORDER BY rowid",
            (oid,),
        ).fetchall()
        events = len(rows)
        pre_chain = 0
        verified = 0
        chain_started = False
        running_prev: Optional[str] = None
        divergence = None
        for r in rows:
            stored_hash = r["row_hash"]
            if stored_hash is None:
                if chain_started:
                    divergence = {
                        "event_id": r["id"], "rowid": r["_rowid"],
                        "reason": "row_hash missing after chain start "
                                  "(event deleted or hash stripped)",
                    }
                    break
                pre_chain += 1
                continue
            chain_started = True
            fields = {k: r[k] for k in _CHAIN_FIELDS}
            # Include optional trailing fields when the column exists (it always
            # does post-migration); _canonical_event ignores None so a pre-v7 row
            # with a NULL baseline still hashes to the old canonical form.
            row_keys = set(r.keys())
            for k in _CHAIN_FIELDS_OPTIONAL:
                if k in row_keys:
                    fields[k] = r[k]
            expected = compute_row_hash(running_prev, fields, hmac_key=hmac_key)
            if r["prev_hash"] != running_prev:
                divergence = {
                    "event_id": r["id"], "rowid": r["_rowid"],
                    "reason": "prev_hash does not match the prior row's row_hash "
                              "(event inserted, deleted, or reordered)",
                    "expected_prev": running_prev, "stored_prev": r["prev_hash"],
                }
                break
            if expected != stored_hash:
                divergence = {
                    "event_id": r["id"], "rowid": r["_rowid"],
                    "reason": "row_hash mismatch (event contents were modified)",
                    "expected": expected, "stored": stored_hash,
                }
                break
            verified += 1
            running_prev = stored_hash

        if divergence is not None:
            status = "broken"
            all_ok = False
        elif events == 0:
            status = "empty"
        else:
            status = "ok"
        orgs.append({
            "org_id": oid, "events": events, "verified": verified,
            "pre_chain": pre_chain, "status": status,
            "first_divergence": divergence,
        })
    return {"ok": all_ok, "orgs": orgs}


# ---- externally-retained checkpoints (#120) ---------------------------------
# verify_chain proves internal consistency, but a chain is only as trustworthy as
# its head: recomputed from genesis, a rewritten history is internally consistent
# too. A checkpoint escrows a head the chain PROVABLY reached — the customer keeps
# a copy out-of-band — so a later recompute is caught the moment the live head no
# longer reproduces the anchored one. The anchor is small and portable (a JSON
# line); where it is stored (git commit, emailed receipt, S3 object-lock bucket,
# a countersignature) is the customer's call and is what makes it INDEPENDENT.
_CHECKPOINT_FIELDS = ("org_id", "through_rowid", "head_hash", "event_count", "mode")


def _canonical_checkpoint(cp: dict) -> str:
    """Deterministic string form of a checkpoint for signing/verification.

    Mirrors ``_canonical_event``: unit-separated ``key=value`` pairs in a fixed
    order, keys included so a value can never migrate columns unnoticed. The
    ``sig`` and ``ts`` are deliberately excluded — the signature covers identity
    (which head, which count, which mode), not its own value or wall-clock time.
    """
    return "\x1f".join(f"{k}={_chain_scalar(cp.get(k))}" for k in _CHECKPOINT_FIELDS)


def sign_checkpoint(cp: dict, hmac_key: Optional[bytes]) -> Optional[str]:
    """HMAC-SHA256 of a checkpoint's canonical form, or ``None`` without a key.

    A checkpoint with a signature can be handed to the customer and later shown
    to be authentic (unforged, unaltered) by anyone holding the key — closing the
    single-party gap where the operator is also the only signer.
    """
    if not hmac_key:
        return None
    import hmac as _hmac
    return _hmac.new(hmac_key, _canonical_checkpoint(cp).encode("utf-8"),
                     hashlib.sha256).hexdigest()


def checkpoint_chain(conn, org_id: str, hmac_key: Optional[bytes] = None,
                     through_rowid: Optional[int] = None,
                     ts: Optional[float] = None, commit: bool = True) -> Optional[dict]:
    """Record a tamper-evidence checkpoint for an org's current chain head.

    Captures the org's highest-rowid chained event (or the head at/below
    ``through_rowid`` when pinning a historical point) plus the count of chained
    events it covers, signs it if ``hmac_key`` is set, and stores it in
    ``chain_checkpoints``. Returns the checkpoint dict (the thing the customer
    retains) or ``None`` if the org has no chained events yet.

    Re-checkpointing the SAME ``through_rowid`` is idempotent — the row is
    replaced with a fresh timestamp/signature rather than erroring — so a periodic
    cron never trips the UNIQUE(org_id, through_rowid) constraint.
    """
    where = "org_id=? AND row_hash IS NOT NULL"
    params: list = [org_id]
    if through_rowid is not None:
        where += " AND rowid<=?"
        params.append(int(through_rowid))
    head = conn.execute(
        f"SELECT rowid AS _rowid, row_hash FROM usage_events WHERE {where} "
        "ORDER BY rowid DESC LIMIT 1", params).fetchone()
    if head is None:
        return None
    count = conn.execute(
        f"SELECT COUNT(*) n FROM usage_events WHERE {where} AND rowid<=?",
        params + [head["_rowid"]]).fetchone()["n"]
    cp = {
        "org_id": org_id,
        "through_rowid": int(head["_rowid"]),
        "head_hash": head["row_hash"],
        "event_count": int(count),
        "mode": "hmac-sha256" if hmac_key else "sha256",
    }
    cp["sig"] = sign_checkpoint(cp, hmac_key)
    cp["ts"] = ts if ts is not None else time.time()
    cp["id"] = new_id("ckpt")
    conn.execute(
        "INSERT INTO chain_checkpoints(id,org_id,through_rowid,head_hash,"
        "event_count,mode,sig,ts) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(org_id,through_rowid) DO UPDATE SET "
        "id=excluded.id, head_hash=excluded.head_hash, "
        "event_count=excluded.event_count, mode=excluded.mode, "
        "sig=excluded.sig, ts=excluded.ts",
        (cp["id"], cp["org_id"], cp["through_rowid"], cp["head_hash"],
         cp["event_count"], cp["mode"], cp["sig"], cp["ts"]))
    if commit:
        conn.commit()
    return cp


def list_checkpoints(conn, org_id: str) -> list:
    """All retained checkpoints for an org, oldest anchor first."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM chain_checkpoints WHERE org_id=? ORDER BY through_rowid",
        (org_id,)).fetchall()]


def verify_checkpoints(conn, checkpoints, hmac_key: Optional[bytes] = None) -> dict:
    """Check retained checkpoints against the live chain — the independent gate.

    ``checkpoints`` is an iterable of checkpoint dicts the CUSTOMER retained (as
    returned by :func:`checkpoint_chain`), typically loaded from an out-of-band
    store rather than this DB — that is the whole point. For each one:

    * the live event at ``through_rowid`` must still carry ``head_hash`` and its
      chain must ``verify_chain`` clean through that rowid, AND
    * the number of chained events at/below ``through_rowid`` must equal the
      anchored ``event_count`` (so history cannot be silently shortened or
      padded below the anchor), AND
    * if the checkpoint carries a ``sig`` and a key is supplied, the signature
      must recompute — proving the anchor itself was not forged.

    Any mismatch is a divergence: the operator rewrote history the customer had
    already anchored. Returns ``{"ok": bool, "checkpoints": [ {..., status} ]}``
    where ``status`` is ``"ok"``, ``"head_mismatch"``, ``"count_mismatch"``,
    ``"missing"`` (the anchored rowid no longer exists / lost its hash),
    ``"bad_signature"``, or ``"chain_broken"`` (the chain up to the anchor does
    not even self-verify).
    """
    results = []
    all_ok = True
    for cp in checkpoints:
        cp = dict(cp)
        org_id = cp.get("org_id")
        rowid = int(cp.get("through_rowid"))
        status = "ok"
        detail = None

        # 1) signature (identity authenticity) — only when both a sig and key exist.
        if cp.get("sig") and hmac_key:
            expected_sig = sign_checkpoint(cp, hmac_key)
            if expected_sig != cp["sig"]:
                status, detail = "bad_signature", "checkpoint signature does not recompute"

        # 2) the live head at the anchored rowid.
        if status == "ok":
            row = conn.execute(
                "SELECT rowid AS _rowid, row_hash FROM usage_events "
                "WHERE org_id=? AND rowid=?", (org_id, rowid)).fetchone()
            if row is None or row["row_hash"] is None:
                status, detail = "missing", (
                    "no chained event at the anchored rowid "
                    "(deleted or hash stripped)")
            elif row["row_hash"] != cp.get("head_hash"):
                status, detail = "head_mismatch", (
                    "live head_hash at the anchored rowid differs from the "
                    "retained checkpoint (history was rewritten)")

        # 3) the chain must self-verify at least up to the anchor, and the
        #    covered event count must match (no silent shorten/pad below it).
        if status == "ok":
            sub = verify_chain(conn, org_id=org_id, hmac_key=hmac_key)
            org_rep = next((o for o in sub["orgs"] if o["org_id"] == org_id), None)
            if org_rep and org_rep["status"] == "broken":
                d = org_rep.get("first_divergence") or {}
                if d.get("rowid", rowid + 1) <= rowid:
                    status, detail = "chain_broken", (
                        "chain diverges at or before the anchored rowid: "
                        + d.get("reason", "unknown"))
            if status == "ok":
                live_count = conn.execute(
                    "SELECT COUNT(*) n FROM usage_events WHERE org_id=? "
                    "AND row_hash IS NOT NULL AND rowid<=?",
                    (org_id, rowid)).fetchone()["n"]
                if int(live_count) != int(cp.get("event_count")):
                    status, detail = "count_mismatch", (
                        f"live chained-event count at/below the anchor "
                        f"({live_count}) != retained ({cp.get('event_count')})")

        if status != "ok":
            all_ok = False
        results.append({
            "org_id": org_id, "through_rowid": rowid,
            "event_count": cp.get("event_count"), "status": status,
            "detail": detail,
        })
    return {"ok": all_ok, "checkpoints": results}


SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    slug               TEXT UNIQUE NOT NULL,
    tier               TEXT NOT NULL DEFAULT 'free',
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    -- When 1, this org is exempt from the prepaid-credit hard-stop (#28): usage
    -- is always recorded and may drive the balance negative (track-only mode for
    -- trusted / internal orgs). 0 = enforce the hard-stop when it's enabled.
    allow_negative_balance INTEGER NOT NULL DEFAULT 0,
    created_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email      TEXT NOT NULL,
    name       TEXT,
    role       TEXT NOT NULL DEFAULT 'owner',
    active     INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    UNIQUE(org_id, email)
);

CREATE TABLE IF NOT EXISTS workspaces (
    id                 TEXT PRIMARY KEY,
    org_id             TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    slug               TEXT NOT NULL,
    monthly_budget_micros INTEGER,
    created_at         REAL NOT NULL,
    UNIQUE(org_id, slug)
);

CREATE TABLE IF NOT EXISTS usage_events (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    workspace_id      TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
    provider          TEXT NOT NULL,
    model             TEXT,
    task_type         TEXT NOT NULL DEFAULT 'general',
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    -- Provider-reported cache creation/write tokens. NULL means the integration
    -- did not provide this field; unlike cache reads, writes are billed at a
    -- provider/model-specific premium. Nullable for additive migration (#135).
    cache_write_tokens INTEGER,
    reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
    user_id           TEXT REFERENCES users(id) ON DELETE SET NULL,
    cost_micros       INTEGER NOT NULL DEFAULT 0,
    -- Counterfactual cost for savings-share billing (#7): what this same call
    -- would have cost without Perseus (same token counts, the customer's
    -- designated baseline model). NULL = no baseline recorded => this event
    -- NEVER contributes to billable savings (the conservative default). When
    -- present it is folded into the hash chain, so a billed saving is as
    -- tamper-evident as the actual cost.
    baseline_micros   INTEGER,
    -- Efficiency-leakage counterfactual (#8): the cheapest option the configured
    -- routing policy WOULD have chosen (and that still passed the quality bar).
    -- actual cost above this = missed savings / an off-policy turn. NULL = no
    -- policy target recorded => never counts toward leakage. Hash-chained, so the
    -- leakage figure is as tamper-evident as the cost and the baseline.
    optimal_micros    INTEGER,
    -- Per-task / per-question attribution (#20-arc, shape A): an opaque
    -- caller-supplied id (e.g. an Invarium task_id) linking this event back to
    -- the task that produced it. NULL = none. Hash-chained (optional trailing
    -- field) so a billed saving can't be re-pointed to a different task
    -- undetected. Indexed via ix_usage_extref (created in _migrate_add_columns
    -- so it applies to upgraded DBs too).
    external_ref      TEXT,
    estimated         INTEGER NOT NULL DEFAULT 1,
    source            TEXT NOT NULL DEFAULT 'api',
    ts                REAL NOT NULL,
    -- Tamper-evidence hash chain (#108). Per-org, in insertion (rowid) order:
    -- row_hash = H(prev_hash-or-genesis || canonical(row)); prev_hash is the
    -- previous event's row_hash for the same org. NULL on rows written before
    -- the chain existed ("pre-chain prefix" — verify reports these as
    -- unverifiable rather than pretending). Written inside record_usage's
    -- transaction; see compute_row_hash / verify_chain.
    prev_hash         TEXT,
    row_hash          TEXT
);
CREATE INDEX IF NOT EXISTS ix_usage_org_ts ON usage_events(org_id, ts);
CREATE INDEX IF NOT EXISTS ix_usage_ws_ts  ON usage_events(workspace_id, ts);
CREATE INDEX IF NOT EXISTS ix_usage_prov   ON usage_events(org_id, provider);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    delta_micros  INTEGER NOT NULL,          -- +topup/grant/refund, -debit
    kind          TEXT NOT NULL,           -- topup|grant|debit|refund|adjust
    reason        TEXT,
    stripe_ref    TEXT,
    balance_after_micros INTEGER NOT NULL,
    ts            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_org_ts ON credit_ledger(org_id, ts);

CREATE TABLE IF NOT EXISTS alerts_log (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL,            -- low_balance|budget_warn|budget_cap
    message      TEXT NOT NULL,
    delivered    INTEGER NOT NULL DEFAULT 0,
    ts           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS stripe_events (
    event_id     TEXT PRIMARY KEY,         -- idempotency: never process twice
    type         TEXT NOT NULL,
    processed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_idempotency (
    org_id   TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    idem_key TEXT NOT NULL,                 -- client Idempotency-Key, scoped per org
    status   INTEGER,                       -- stored response status (NULL = in-flight)
    response TEXT,                          -- stored response body JSON for replay
    ts       REAL NOT NULL,
    PRIMARY KEY (org_id, idem_key)
);
CREATE INDEX IF NOT EXISTS ix_idem_ts ON ingest_idempotency(ts);

-- #150: per-source ingestion health tracking. One row per (org, source) tuple,
-- updated on every ingest attempt. Sources are integration names or API key
-- prefixes. Rejection reasons are stored as text (not codes) so the dashboard
-- can show actionable diagnostics without raw secret exposure.
CREATE TABLE IF NOT EXISTS ingest_health (
    org_id       TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    source       TEXT NOT NULL,             -- integration name, e.g. "claude_code_hook"
    last_ts      REAL,                      -- most recent ingest attempt (success or fail)
    last_ok      INTEGER,                   -- 1 = last attempt succeeded, 0 = failed
    last_error   TEXT,                      -- last rejection reason (redacted, user-safe)
    total_events INTEGER NOT NULL DEFAULT 0,
    total_errors INTEGER NOT NULL DEFAULT 0,
    since_ts     REAL NOT NULL,             -- when this health row was first created
    PRIMARY KEY (org_id, source)
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,        -- opaque random; lives in an HttpOnly cookie
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,        -- key_...
    org_id       TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name         TEXT,
    prefix       TEXT NOT NULL,           -- shown in the UI, e.g. plutus_sk_AbC1
    token_hash   TEXT NOT NULL UNIQUE,    -- sha256 of the full secret; raw never stored
    created_at   REAL NOT NULL,
    last_used_at REAL,
    revoked_at   REAL,
    -- #150: optional JSON scope restrictions, e.g. {"workspaces": ["prod"]}
    -- When NULL the key has access to all workspaces in the org.
    scope        TEXT,
    -- #150: cumulative event count metered through this key (approximate).
    event_count  INTEGER NOT NULL DEFAULT 0,
    -- #150: when set, this key was created as part of a rotation chain —
    -- references the key it replaced (from which it inherited active status).
    rotation_of  TEXT
);
CREATE INDEX IF NOT EXISTS ix_apikeys_org  ON api_keys(org_id);
CREATE INDEX IF NOT EXISTS ix_apikeys_hash ON api_keys(token_hash);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Savings-share billing (#7). One row per org+period once a savings-share
-- invoice is raised, so billing is idempotent (a re-run for the same period is a
-- no-op) and the amount charged is auditable against the usage_events it was
-- derived from. Money is integer micro-dollars, like every other money column.
CREATE TABLE IF NOT EXISTS savings_invoices (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    period_label      TEXT NOT NULL,          -- 'YYYY-MM'
    gross_savings_micros INTEGER NOT NULL,    -- sum(max(0, baseline-cost)) over the period
    rate_bps          INTEGER NOT NULL,       -- savings-share rate in basis points (1000 = 10%)
    amount_micros     INTEGER NOT NULL,       -- billed = gross * rate (rounded to micros)
    covered_events    INTEGER NOT NULL DEFAULT 0,  -- events that carried a baseline
    total_events      INTEGER NOT NULL DEFAULT 0,  -- all metered events in the period
    stripe_invoice_id TEXT,                   -- NULL until a Stripe invoice is raised
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending|invoiced|void
    ts                REAL NOT NULL,
    UNIQUE(org_id, period_label)
);
CREATE INDEX IF NOT EXISTS ix_savinv_org ON savings_invoices(org_id, period_label);

-- Externally-retained tamper-evidence anchors (#120). The usage_events hash
-- chain is tamper-evident GIVEN a trusted head, but nothing pins the head: an
-- operator with DB access (and, in the single-party self-hosted case, the only
-- HMAC key) could rewrite history and recompute the whole chain from genesis,
-- and verify_chain would still pass. A checkpoint records a head the chain
-- reached at a point in time; the customer keeps a copy out-of-band (git, email,
-- S3 object-lock, a countersignature). verify_checkpoints then requires the live
-- DB to REPRODUCE that exact head_hash + event_count at through_rowid — which an
-- operator who rewrote earlier history cannot do. Signature is optional: with a
-- key set, sig = HMAC-SHA256(key, canonical(checkpoint)) so a holder of the key
-- (the customer) can confirm a retained anchor is authentic and unforged.
CREATE TABLE IF NOT EXISTS chain_checkpoints (
    id            TEXT PRIMARY KEY,           -- ckpt_...
    org_id        TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    through_rowid INTEGER NOT NULL,           -- usage_events.rowid this head covers
    head_hash     TEXT NOT NULL,              -- row_hash of the event at through_rowid
    event_count   INTEGER NOT NULL,           -- chained events at/below through_rowid
    mode          TEXT NOT NULL,              -- 'sha256' | 'hmac-sha256'
    sig           TEXT,                       -- HMAC-SHA256 over the checkpoint, if keyed
    ts            REAL NOT NULL,
    UNIQUE(org_id, through_rowid)
);
CREATE INDEX IF NOT EXISTS ix_ckpt_org ON chain_checkpoints(org_id, through_rowid);
"""

# Public prefix for ingest API keys. The secret is `plutus_sk_<random>`; only its
# sha256 is ever stored, and only the first few chars are kept for display.
API_KEY_PREFIX = "plutus_sk_"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def slugify(name: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in name.strip())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "default"


# ------------------------------------------------------------- connection ----
def connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    from . import config
    p = Path(path) if path else config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")  # Fix #30: wait on lock instead of error
    return conn


@contextlib.contextmanager
def immediate(conn: sqlite3.Connection):
    """Run a read-modify-write as one serialized SQLite ``IMMEDIATE`` transaction.

    ``BEGIN IMMEDIATE`` takes the RESERVED write lock up front, so a balance or
    free-tier-quota *read* and the dependent *insert* cannot interleave with
    another writer — closing the #28 prepaid hard-stop overrun and the #30
    free-tier-quota race under the threaded, connection-per-request server.
    (``balance_after_micros`` itself is computed in-SQL by :func:`add_ledger`,
    so it stays correct even outside this block.)

    Commits on success, rolls back on error. The connection's ``isolation_level``
    is set to manual for the duration and restored on exit, so callers that rely
    on the default implicit-transaction behavior are unaffected. If a transaction
    is already open (e.g. nested use), this is a no-op pass-through — the
    outermost caller owns the commit.
    """
    if conn.in_transaction:
        yield
        return
    prev = conn.isolation_level
    conn.isolation_level = None  # take manual control of BEGIN/COMMIT
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.isolation_level = prev


def get_schema_version(conn) -> Optional[int]:
    """The schema version stamped in this database, or None for a DB that predates
    the meta table / has never been initialized."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error:
        return None
    try:
        return int(row["value"]) if row else None
    except (TypeError, ValueError):
        return None


def init_schema(conn: sqlite3.Connection) -> None:
    # Forward-compat guard: refuse a database written by a NEWER Plutus rather
    # than silently running old code against a future schema (which could corrupt
    # money data). Within the 1.0 line schema changes are additive, so an equal-or-
    # older stored version is always safe to bring forward. See docs/schema.md.
    existing = get_schema_version(conn)
    if existing is not None and existing > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema_version {existing} is newer than this Plutus "
            f"supports ({SCHEMA_VERSION}); upgrade the package before opening it"
        )
    _migrate_money_to_micros(conn)
    conn.executescript(SCHEMA)
    _migrate_add_columns(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _migrate_add_columns(conn) -> None:
    """Idempotently add columns introduced after a table's first release.

    ``CREATE TABLE IF NOT EXISTS`` in SCHEMA is a no-op on an existing table, so
    new columns need an explicit ``ALTER``. Each entry is added only if absent.
    """
    additions = [
        # (table, column, definition) — #28: per-org hard-stop exemption.
        ("organizations", "allow_negative_balance", "INTEGER NOT NULL DEFAULT 0"),
        ("organizations", "stripe_subscription_id", "TEXT"),
        # #108: usage_events tamper-evidence chain. Nullable (no default) so the
        # chain starts at upgrade and pre-existing rows stay NULL = "unverifiable
        # (pre-chain)" rather than being back-filled with a hash we can't attest.
        ("usage_events", "prev_hash", "TEXT"),
        ("usage_events", "row_hash", "TEXT"),
        # #7: savings-share counterfactual. Nullable (no default) so existing
        # rows stay NULL = "no baseline" and never contribute to billable
        # savings, and the hash chain over pre-v7 rows is unchanged.
        ("usage_events", "baseline_micros", "INTEGER"),
        # #8: efficiency-leakage counterfactual (cheapest policy-passing option).
        ("usage_events", "optimal_micros", "INTEGER"),
        # #20-arc shape A: per-task/per-question attribution ref. Nullable so
        # existing rows stay NULL and the hash chain over pre-v10 rows is
        # unchanged (external_ref is an optional trailing chain field).
        ("usage_events", "external_ref", "TEXT"),
        # #135: provider cache-creation/write tokens. Nullable so old rows remain
        # semantically absent and pre-v11 chain canonical forms are preserved.
        ("usage_events", "cache_write_tokens", "INTEGER"),
        # #136: nullable per-user attribution; old events remain unattributed.
        ("usage_events", "user_id", "TEXT"),
        ("users", "active", "INTEGER NOT NULL DEFAULT 1"),
        # #150: api_keys scoping, event counter, and rotation chain
        ("api_keys", "scope", "TEXT"),
        ("api_keys", "event_count", "INTEGER NOT NULL DEFAULT 0"),
        ("api_keys", "rotation_of", "TEXT"),
    ]
    for table, col, defn in additions:
        cols = _table_columns(conn, table)
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
    # Index on the attribution ref, created here (not in SCHEMA) so it runs after
    # the ALTER above adds the column on an upgraded DB — a fresh DB already has
    # the column from SCHEMA, so IF NOT EXISTS makes this a no-op there.
    if "external_ref" in _table_columns(conn, "usage_events"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_usage_extref "
            "ON usage_events(org_id, external_ref)"
        )
    if "user_id" in _table_columns(conn, "usage_events"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_usage_user "
            "ON usage_events(org_id, user_id)"
        )


def _table_columns(conn, table: str) -> set:
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _migrate_money_to_micros(conn) -> None:
    """Convert a pre-v4 database (REAL USD money columns) to integer micros.

    Idempotent: detects the legacy column on each table and, if present, adds the
    new ``*_micros`` column, back-fills it as ``round(usd * 1e6)``, and drops the
    old column (via SQLite's column-drop, available 3.35+, with a table-rebuild
    fallback for older SQLite). A fresh database has no legacy columns, so this
    is a no-op and the canonical SCHEMA creates the integer columns directly.
    """
    plan = [
        ("usage_events", "cost_usd", "cost_micros"),
        ("credit_ledger", "delta_usd", "delta_micros"),
        ("credit_ledger", "balance_after", "balance_after_micros"),
        ("workspaces", "monthly_budget_usd", "monthly_budget_micros"),
    ]
    did_any = False
    for table, old_col, new_col in plan:
        cols = _table_columns(conn, table)
        if not cols or old_col not in cols:
            continue  # fresh DB or already migrated
        did_any = True
        if new_col not in cols:
            coltype = "INTEGER NOT NULL DEFAULT 0" if old_col != "monthly_budget_usd" else "INTEGER"
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {new_col} {coltype}")
        # Back-fill: round to nearest micro. NULL budgets stay NULL.
        conn.execute(
            f"UPDATE {table} SET {new_col} = CAST(ROUND({old_col} * 1000000) AS INTEGER) "
            f"WHERE {old_col} IS NOT NULL"
        )
        # Drop the legacy column. SQLite >= 3.35 supports DROP COLUMN directly.
        try:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {old_col}")
        except sqlite3.OperationalError:
            pass  # older SQLite: leave the now-unused REAL column in place
    if did_any:
        conn.commit()


# ---------------------------------------------------------- organizations ----
def create_org(conn, name: str, tier: str = "free",
               owner_email: Optional[str] = None,
               owner_name: Optional[str] = None) -> sqlite3.Row:
    oid = new_id("org")
    slug = slugify(name)
    # ensure slug uniqueness
    n, base = 1, slug
    while conn.execute("SELECT 1 FROM organizations WHERE slug=?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    conn.execute(
        "INSERT INTO organizations(id,name,slug,tier,created_at) VALUES(?,?,?,?,?)",
        (oid, name, slug, tier, time.time()),
    )
    if owner_email:
        conn.execute(
            "INSERT INTO users(id,org_id,email,name,role,created_at) VALUES(?,?,?,?,?,?)",
            (new_id("usr"), oid, owner_email, owner_name, "owner", time.time()),
        )
    conn.commit()
    return get_org(conn, oid)


def get_org(conn, org_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()


def get_org_by_slug(conn, slug: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM organizations WHERE slug=?", (slug,)).fetchone()


def list_orgs(conn, limit: Optional[int] = None, offset: int = 0) -> list[sqlite3.Row]:
    """All orgs, oldest first. Fix #66: optional ``limit``/``offset`` paging."""
    sql = "SELECT * FROM organizations ORDER BY created_at"
    args: list = []
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
    return conn.execute(sql, args).fetchall()


def count_orgs_created_since(conn, since_ts: float) -> int:
    """How many organizations were created at or after ``since_ts`` (epoch s).

    Backs the #33 per-day self-serve org-creation cap; DB-backed so it survives
    process restarts (unlike the in-memory hourly limiter)."""
    row = conn.execute(
        "SELECT COUNT(*) n FROM organizations WHERE created_at >= ?", (since_ts,)
    ).fetchone()
    return int(row["n"])


def set_org_tier(conn, org_id: str, tier: str, commit: bool = True) -> None:
    conn.execute("UPDATE organizations SET tier=? WHERE id=?", (tier, org_id))
    if commit:
        conn.commit()


def set_org_allow_negative(conn, org_id: str, allow: bool) -> None:
    """Toggle the #28 prepaid hard-stop exemption for one org."""
    conn.execute("UPDATE organizations SET allow_negative_balance=? WHERE id=?",
                 (1 if allow else 0, org_id))
    conn.commit()


def set_stripe_customer(conn, org_id: str, customer_id: str) -> None:
    conn.execute("UPDATE organizations SET stripe_customer_id=? WHERE id=?",
                 (customer_id, org_id))
    conn.commit()


def org_by_stripe_customer(conn, customer_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM organizations WHERE stripe_customer_id=?", (customer_id,)
    ).fetchone()


def set_stripe_subscription(conn, org_id: str, subscription_id: str,
                            commit: bool = True) -> None:
    conn.execute("UPDATE organizations SET stripe_subscription_id=? WHERE id=?",
                 (subscription_id, org_id))
    if commit:
        conn.commit()


# ------------------------------------------------------------------- users ---
def get_user(conn, user_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def users_by_email(conn, email: str) -> list[sqlite3.Row]:
    """All user rows for an email (a person may belong to several orgs)."""
    return conn.execute(
        "SELECT * FROM users WHERE lower(email)=lower(?) ORDER BY created_at", (email,)
    ).fetchall()


def ensure_user(conn, org_id: str, email: str, name: Optional[str] = None,
                role: str = "member") -> sqlite3.Row:
    """Get-or-create the user row for (org, email); backfill name if newly known."""
    row = conn.execute(
        "SELECT * FROM users WHERE org_id=? AND lower(email)=lower(?)",
        (org_id, email),
    ).fetchone()
    if row:
        if name and not row["name"]:
            conn.execute("UPDATE users SET name=? WHERE id=?", (name, row["id"]))
            conn.commit()
            return get_user(conn, row["id"])
        return row
    uid = new_id("usr")
    conn.execute(
        "INSERT INTO users(id,org_id,email,name,role,created_at) VALUES(?,?,?,?,?,?)",
        (uid, org_id, email, name, role, time.time()),
    )
    conn.commit()
    return get_user(conn, uid)


def list_orgs_for_email(conn, email: str) -> list[sqlite3.Row]:
    """Orgs the email is a member of, ordered by org creation."""
    return conn.execute(
        "SELECT o.* FROM organizations o JOIN users u ON u.org_id=o.id "
        "WHERE lower(u.email)=lower(?) ORDER BY o.created_at",
        (email,),
    ).fetchall()


def list_users(conn, org_id: str, include_inactive: bool = False) -> list[sqlite3.Row]:
    """List an org's seat roster, active users first."""
    sql = "SELECT * FROM users WHERE org_id=?"
    if not include_inactive:
        sql += " AND active=1"
    return conn.execute(sql + " ORDER BY created_at", (org_id,)).fetchall()


def active_seat_count(conn, org_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) n FROM users WHERE org_id=? AND active=1", (org_id,)
    ).fetchone()
    return int(row["n"])


def set_user_active(conn, user_id: str, org_id: str, active: bool) -> bool:
    cur = conn.execute(
        "UPDATE users SET active=? WHERE id=? AND org_id=?",
        (1 if active else 0, user_id, org_id),
    )
    conn.commit()
    return cur.rowcount == 1


def email_in_org(conn, email: str, org_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM users WHERE org_id=? AND lower(email)=lower(?)",
        (org_id, email),
    ).fetchone() is not None


# ---------------------------------------------------------------- sessions ---
def create_session(conn, user_id: str, ttl_seconds: float) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
        (token, user_id, now, now + ttl_seconds),
    )
    conn.commit()
    return token


def session_user(conn, token: str) -> Optional[sqlite3.Row]:
    """Resolve a session token to its user row, or None if missing/expired."""
    if not token:
        return None
    return conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token=? AND s.expires_at > ?",
        (token, time.time()),
    ).fetchone()


def delete_session(conn, token: str) -> None:
    if not token:
        return
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()


def purge_expired_sessions(conn) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------- api keys ----
def _hash_token(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_api_key(conn, org_id: str, name: Optional[str] = None) -> tuple[sqlite3.Row, str]:
    """Mint an ingest API key for an org.

    Returns ``(row, secret)``. The full ``secret`` is shown to the caller **once**
    — only its hash is stored, so it can never be recovered later.
    """
    secret = API_KEY_PREFIX + secrets.token_urlsafe(24)
    kid = new_id("key")
    prefix = secret[:len(API_KEY_PREFIX) + 4]   # e.g. "plutus_sk_AbC1"
    conn.execute(
        "INSERT INTO api_keys(id,org_id,name,prefix,token_hash,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (kid, org_id, name, prefix, _hash_token(secret), time.time()),
    )
    conn.commit()
    return get_api_key(conn, kid), secret


def get_api_key(conn, key_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()


def list_api_keys(conn, org_id: str, include_revoked: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM api_keys WHERE org_id=?"
    if not include_revoked:
        q += " AND revoked_at IS NULL"
    return conn.execute(q + " ORDER BY created_at DESC", (org_id,)).fetchall()


# ------------------------------------------------------------- LAST_USED ---
LAST_USED_THROTTLE_S = 60  # don't rewrite last_used_at more than once a minute


def api_key_org(conn, secret: str) -> Optional[str]:
    """Resolve a presented API-key secret to its org_id, or None.

    Touches ``last_used_at`` on success, but at most once per
    ``LAST_USED_THROTTLE_S`` seconds per key: every ``/v1/usage`` ingest
    authenticates, and writing+committing this column on every call thrashed the
    WAL and added write contention with the metering transaction (Fix #37 item
    5). ``last_used_at`` is a coarse "recently used?" signal — minute precision
    is plenty. Revoked keys never resolve.
    """
    if not secret or not secret.startswith(API_KEY_PREFIX):
        return None
    row = conn.execute(
        "SELECT * FROM api_keys WHERE token_hash=? AND revoked_at IS NULL",
        (_hash_token(secret),),
    ).fetchone()
    if not row:
        return None
    now = time.time()
    if now - (row["last_used_at"] or 0) >= LAST_USED_THROTTLE_S:
        conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?",
                     (now, row["id"]))
        conn.commit()
    # #150: increment event count on every key use (not throttled — this is the
    # authoritative counter; a hot key may be slightly behind on last_used_at but
    # the event_count monotonic counter stays accurate)
    conn.execute(
        "UPDATE api_keys SET event_count = event_count + 1 WHERE id=?",
        (row["id"],),
    )
    conn.commit()
    return row["org_id"]


def api_key_row(conn, secret: str) -> Optional[sqlite3.Row]:
    """#150: Resolve an API key secret to its full row, or None.

    Like ``api_key_org`` but returns the entire row so the caller can inspect
    scope, prefix, last_used_at, etc. Does NOT update last_used_at or
    event_count — that is ``api_key_org``'s job.
    """
    if not secret or not secret.startswith(API_KEY_PREFIX):
        return None
    return conn.execute(
        "SELECT * FROM api_keys WHERE token_hash=? AND revoked_at IS NULL",
        (_hash_token(secret),),
    ).fetchone()


def create_api_key_scoped(conn, org_id: str,
                          name: Optional[str] = None,
                          scope: Optional[dict] = None,
                          commit: bool = True) -> tuple[sqlite3.Row, str]:
    """#150: Mint an API key with optional scope restrictions.

    ``scope`` is an optional dict limiting the key's access, e.g.:
    ``{"workspaces": ["prod"]}`` or ``{"org_id": "org_abc"}``.
    When None, the key has unrestricted access within the org.
    Returns ``(row, secret)`` like :func:`create_api_key`.
    """
    secret = API_KEY_PREFIX + secrets.token_urlsafe(24)
    kid = new_id("key")
    prefix = secret[:len(API_KEY_PREFIX) + 4]
    scope_json = json.dumps(scope) if scope else None
    conn.execute(
        "INSERT INTO api_keys(id,org_id,name,prefix,token_hash,created_at,scope)"
        " VALUES(?,?,?,?,?,?,?)",
        (kid, org_id, name, prefix, _hash_token(secret), time.time(), scope_json),
    )
    if commit:
        conn.commit()
    return get_api_key(conn, kid), secret


def rotate_api_key(conn, org_id: str, old_key_id: str,
                   overlap_seconds: int = 300,
                   name: Optional[str] = None,
                   scope: Optional[dict] = None,
                   commit: bool = True) -> tuple[sqlite3.Row, str, sqlite3.Row]:
    """#150: Rotate an API key with a bounded overlap period.

    Creates a new key (inheriting the old key's scope if none given) and records
    the rotation chain. The old key is NOT immediately revoked — it remains valid
    for ``overlap_seconds`` so in-flight requests can complete (zero-downtime).
    The caller should schedule ``complete_key_rotation`` after the overlap.

    Returns ``(new_key_row, new_secret, old_key_row)``.
    """
    old = get_api_key(conn, old_key_id)
    if not old:
        raise ValueError(f"key {old_key_id} not found")
    if old["org_id"] != org_id:
        raise ValueError("key does not belong to this org")
    if old["revoked_at"] is not None:
        raise ValueError("cannot rotate a revoked key")

    # Inherit scope from old key if none specified
    if scope is None and old["scope"]:
        try:
            scope = json.loads(old["scope"])
        except (json.JSONDecodeError, TypeError):
            scope = None

    # Create new key with rotation_of pointing to the old key
    new_row, secret = create_api_key_scoped(
        conn, org_id, name=name or old["name"], scope=scope, commit=False)

    # Record rotation chain: new key points to old key id
    conn.execute(
        "UPDATE api_keys SET rotation_of=? WHERE id=?",
        (old_key_id, new_row["id"]),
    )
    if commit:
        conn.commit()

    old_refreshed = get_api_key(conn, old_key_id)
    return get_api_key(conn, new_row["id"]), secret, old_refreshed


def complete_key_rotation(conn, org_id: str, new_key_id: str,
                          commit: bool = True) -> bool:
    """#150: Complete a rotation by revoking the old key.

    Given the NEW key's id, finds the old key in the rotation chain and
    revokes it. Returns True if a key was revoked.
    """
    new_key = get_api_key(conn, new_key_id)
    if not new_key or new_key["org_id"] != org_id:
        return False
    rotation_of = new_key["rotation_of"]
    if not rotation_of:
        return False
    # rotation_of on the new key points to the old key id
    return revoke_api_key(conn, rotation_of, org_id, commit=commit)


def rotate_and_revoke(conn, org_id: str, old_key_id: str,
                      overlap_seconds: int = 0,
                      name: Optional[str] = None,
                      scope: Optional[dict] = None,
                      commit: bool = True) -> tuple[sqlite3.Row, str]:
    """#150: Rotate and immediately revoke (no overlap — for emergency rotation).

    Equivalent to calling rotate_api_key with overlap_seconds=0 followed by
    complete_key_rotation. Returns ``(new_key_row, new_secret)``.
    """
    new_row, secret, old_row = rotate_api_key(
        conn, org_id, old_key_id,
        overlap_seconds=max(1, overlap_seconds) if overlap_seconds else 0,
        name=name, scope=scope, commit=False)
    if overlap_seconds <= 0:
        revoke_api_key(conn, old_key_id, org_id, commit=False)
    if commit:
        conn.commit()
    return get_api_key(conn, new_row["id"]), secret


def revoke_api_key(conn, key_id: str, org_id: Optional[str] = None,
                   commit: bool = True) -> bool:
    """Revoke a key (optionally scoped to an org). Returns True if one changed."""
    if org_id:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at=? WHERE id=? AND org_id=? AND revoked_at IS NULL",
            (time.time(), key_id, org_id))
    else:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (time.time(), key_id))
    if commit:
        conn.commit()
    return cur.rowcount > 0


# ----------------------------------------------------------------- ingest health ---
def record_ingest_health(conn, org_id: str, source: str,
                         ok: bool, error: Optional[str] = None,
                         ts: Optional[float] = None) -> None:
    """#150: Record an ingest health event for an org+source.

    Creates or updates the health row. ``source`` is the integration name
    (e.g. ``"claude_code_hook"``, ``"api_key:plutus_sk_AbC1"``).
    ``error`` is a user-safe rejection reason (never raw secrets).
    """
    now = ts if ts is not None else time.time()
    conn.execute(
        "INSERT INTO ingest_health(org_id,source,last_ts,last_ok,last_error,"
        "total_events,total_errors,since_ts) "
        "VALUES(?,?,?,?,?,1,?,?) "
        "ON CONFLICT(org_id,source) DO UPDATE SET "
        "last_ts=excluded.last_ts, last_ok=excluded.last_ok, "
        "last_error=COALESCE(excluded.last_error, ingest_health.last_error), "
        "total_events=ingest_health.total_events+1, "
        "total_errors=ingest_health.total_errors+excluded.total_errors",
        (org_id, source, now, 1 if ok else 0,
         error, 0 if ok else 1, now),
    )
    conn.commit()


def get_ingest_health(conn, org_id: str,
                      limit: int = 50) -> list[dict]:
    """#150: Ingestion health diagnostics for an org, by source.

    Returns rows ordered by most recent ingest first, each containing:
    source, last_ts, last_ok, last_error, total_events, total_errors.
    """
    rows = conn.execute(
        "SELECT source, last_ts, last_ok, last_error, total_events, total_errors "
        "FROM ingest_health WHERE org_id=? ORDER BY last_ts DESC LIMIT ?",
        (org_id, int(limit)),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["last_ok"] = bool(d["last_ok"])
        out.append(d)
    return out


def all_ingest_health(conn, limit_per_org: int = 5) -> list[dict]:
    """#150: Aggregated ingest health across all orgs for the admin dashboard.

    Returns the health row per org+source, newest first per org.
    """
    rows = conn.execute(
        "SELECT ih.org_id, o.name AS org_name, ih.source, ih.last_ts, "
        "ih.last_ok, ih.last_error, ih.total_events, ih.total_errors "
        "FROM ingest_health ih "
        "LEFT JOIN organizations o ON o.id=ih.org_id "
        "ORDER BY ih.last_ts DESC",
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------- workspaces ----
def create_workspace(conn, org_id: str, name: str,
                     monthly_budget_usd: Optional[float] = None,
                     commit: bool = True) -> sqlite3.Row:
    wid = new_id("ws")
    slug = slugify(name)
    n, base = 1, slug
    while conn.execute(
        "SELECT 1 FROM workspaces WHERE org_id=? AND slug=?", (org_id, slug)
    ).fetchone():
        n += 1
        slug = f"{base}-{n}"
    conn.execute(
        "INSERT INTO workspaces(id,org_id,name,slug,monthly_budget_micros,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (wid, org_id, name, slug, usd_to_micros(monthly_budget_usd), time.time()),
    )
    if commit:  # skip when inside a caller-owned transaction (e.g. db.immediate)
        conn.commit()
    return get_workspace(conn, wid)


def _workspace_row(row: Optional[sqlite3.Row]):
    """Expose ``monthly_budget_usd`` (float USD) alongside the stored micros."""
    if row is None:
        return None
    d = dict(row)
    d["monthly_budget_usd"] = micros_to_usd(d.get("monthly_budget_micros"))
    return d


def list_workspaces(conn, org_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM workspaces WHERE org_id=? ORDER BY created_at", (org_id,)
    ).fetchall()
    return [_workspace_row(r) for r in rows]


def get_workspace(conn, workspace_id: str):
    row = conn.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
    return _workspace_row(row)


# ----------------------------------------------------------------- credit ----
def get_balance(conn, org_id: str) -> float:
    """Authoritative balance in float USD = sum of all ledger deltas.

    Computed from the integer micro-dollar deltas rather than the latest row's
    ``balance_after_micros`` so it is correct regardless of insertion /
    timestamp order (live metering arrives in order; demo seeding and historical
    back-fill do not). Integers sum exactly, so there is no float drift; we
    convert to USD once, here at the boundary.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(delta_micros),0) bal FROM credit_ledger WHERE org_id=?",
        (org_id,),
    ).fetchone()
    return micros_to_usd(int(row["bal"]))


def get_balance_micros(conn, org_id: str) -> int:
    """Authoritative balance in integer micro-dollars (no float involved)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(delta_micros),0) bal FROM credit_ledger WHERE org_id=?",
        (org_id,),
    ).fetchone()
    return int(row["bal"])


def add_ledger(conn, org_id: str, delta_usd: float, kind: str,
               reason: str = "", stripe_ref: Optional[str] = None,
               ts: Optional[float] = None, commit: bool = True) -> sqlite3.Row:
    """Add a ledger entry. ``delta_usd`` is float USD on the API; it is stored
    as integer micro-dollars. Balance is authoritative via SUM(delta_micros);
    ``balance_after_micros`` is the running balance through this row.

    Fix #30: the running balance is computed **in the INSERT itself**, as
    ``SUM(existing deltas) + this delta``, so it is atomic with the write — two
    concurrent debits for one org can no longer read the same stale balance and
    persist a wrong/duplicate ``balance_after`` (SQLite serializes writers, so
    the second INSERT's subquery sees the first row). The returned row exposes
    float ``delta_usd`` / ``balance_after`` aliases so existing callers work.
    """
    ts = ts if ts is not None else time.time()
    delta_micros = usd_to_micros(delta_usd)
    lid = new_id("led")
    conn.execute(
        "INSERT INTO credit_ledger(id,org_id,delta_micros,kind,reason,stripe_ref,balance_after_micros,ts)"
        " VALUES(?,?,?,?,?,?,"
        " COALESCE((SELECT SUM(delta_micros) FROM credit_ledger WHERE org_id=?),0)+?,?)",
        (lid, org_id, delta_micros, kind, reason, stripe_ref, org_id, delta_micros, ts),
    )
    if commit:
        conn.commit()
    return get_ledger_entry(conn, lid)


def _ledger_row_with_usd(row: Optional[sqlite3.Row]) -> Optional[dict]:
    """Return a ledger row as a dict with float USD aliases added.

    Adds ``delta_usd`` and ``balance_after`` (float USD) alongside the stored
    ``*_micros`` integer columns so callers and templates can use either.
    """
    if row is None:
        return None
    d = dict(row)
    d["delta_usd"] = micros_to_usd(d["delta_micros"])
    d["balance_after"] = micros_to_usd(d["balance_after_micros"])
    return d


def get_ledger_entry(conn, ledger_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM credit_ledger WHERE id=?", (ledger_id,)).fetchone()
    return _ledger_row_with_usd(row)


def ledger_history(conn, org_id: str, limit: int = 50,
                   before: Optional[int] = None) -> list[dict]:
    """Most-recent ledger entries, newest first. Fix #66: pass ``before`` (the
    ``_rowid`` of the last row from the previous page) for cursor pagination."""
    sql = "SELECT rowid AS _rowid, * FROM credit_ledger WHERE org_id=?"
    args: list = [org_id]
    if before is not None:
        sql += " AND rowid < ?"
        args.append(int(before))
    sql += " ORDER BY ts DESC, rowid DESC LIMIT ?"
    args.append(int(limit))
    rows = conn.execute(sql, args).fetchall()
    return [_ledger_row_with_usd(r) for r in rows]


def events_by_ref(conn, org_id: str, external_ref: str) -> list[sqlite3.Row]:
    """All usage events an org recorded under one attribution ref (#20-arc, A).

    The join from a billed saving back to the task that produced it: given an
    Invarium ``task_id`` (stored as ``external_ref``), return its event rows
    (newest first) so cost/baseline/savings can be tied to the exact task.
    """
    return conn.execute(
        "SELECT * FROM usage_events WHERE org_id=? AND external_ref=? "
        "ORDER BY rowid DESC",
        (org_id, external_ref),
    ).fetchall()


def export_events(conn, org_id: str, since: Optional[float] = None,
                  until: Optional[float] = None, limit: int = 50_000) -> list[dict]:
    """Org-scoped usage events for CSV/JSON export (fix #66), newest first,
    optionally bounded by [since, until) epoch seconds. ``limit`` caps the rows
    returned so an export can't exhaust memory."""
    sql = ("SELECT ue.id, ue.ts, ue.provider, ue.model, ue.task_type, "
           "w.name AS workspace, ue.input_tokens, ue.output_tokens, "
           "ue.cache_read_tokens, ue.cache_write_tokens, ue.reasoning_tokens, "
           "ue.user_id, ue.cost_micros, "
           "ue.baseline_micros, ue.optimal_micros, ue.external_ref, "
           "ue.estimated, ue.source "
           "FROM usage_events ue "
           "LEFT JOIN workspaces w ON w.id=ue.workspace_id WHERE ue.org_id=?")
    args: list = [org_id]
    if since is not None:
        sql += " AND ue.ts >= ?"
        args.append(float(since))
    if until is not None:
        sql += " AND ue.ts < ?"
        args.append(float(until))
    sql += " ORDER BY ue.ts DESC, ue.rowid DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(r)
        d["cost_usd"] = micros_to_usd(int(d.pop("cost_micros")))
        bm = d.pop("baseline_micros", None)
        # baseline_usd is blank (not 0) when no baseline was recorded, so an
        # auditor can tell "no counterfactual" apart from "counterfactual of $0".
        d["baseline_usd"] = None if bm is None else micros_to_usd(int(bm))
        om = d.pop("optimal_micros", None)
        d["optimal_usd"] = None if om is None else micros_to_usd(int(om))
        d["estimated"] = bool(d["estimated"])
        out.append(d)
    return out


def reversed_for_ref(conn, org_id: str, stripe_ref: str) -> float:
    """USD already reversed (``refund``/``adjust``) against a Stripe reference.

    Returned as a positive amount. Fix #60: refund/dispute webhook handlers use
    this to make a reversal *converge* to a target cumulative amount, so a
    partial-then-full refund, a dispute fired twice (created + funds_withdrawn),
    or a replayed event can never double-reverse the ledger.
    """
    if not stripe_ref:
        return 0.0
    row = conn.execute(
        "SELECT COALESCE(SUM(delta_micros),0) m FROM credit_ledger "
        "WHERE org_id=? AND stripe_ref=? AND kind IN ('refund','adjust')",
        (org_id, stripe_ref),
    ).fetchone()
    return -micros_to_usd(int(row["m"]))  # stored deltas are negative; report positive


def org_by_topup_ref(conn, stripe_ref: str) -> Optional[str]:
    """Org credited by a ``topup``/``grant`` keyed by this Stripe reference.

    Fix #60: a dispute object carries no ``customer``, so we map it back to its
    org via the PaymentIntent id stored on the original top-up's ``stripe_ref``.
    """
    if not stripe_ref:
        return None
    row = conn.execute(
        "SELECT org_id FROM credit_ledger WHERE stripe_ref=? "
        "AND kind IN ('topup','grant') ORDER BY ts LIMIT 1",
        (stripe_ref,),
    ).fetchone()
    return row["org_id"] if row else None


# ---------------------------------------------------------- savings-share ---
def get_savings_invoice(conn, org_id: str, period_label: str) -> Optional[dict]:
    """The savings-share invoice row for an org+period, or None."""
    row = conn.execute(
        "SELECT * FROM savings_invoices WHERE org_id=? AND period_label=?",
        (org_id, period_label),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["gross_savings_usd"] = micros_to_usd(int(d["gross_savings_micros"]))
    d["amount_usd"] = micros_to_usd(int(d["amount_micros"]))
    d["rate_pct"] = d["rate_bps"] / 100.0
    return d


def record_savings_invoice(conn, org_id: str, period_label: str, *,
                           gross_savings_micros: int, rate_bps: int,
                           amount_micros: int, covered_events: int,
                           total_events: int, stripe_invoice_id: Optional[str] = None,
                           status: str = "pending", ts: Optional[float] = None,
                           commit: bool = True) -> dict:
    """Insert-or-update the savings-share invoice for an org+period.

    Idempotent by the ``UNIQUE(org_id, period_label)`` constraint: the first call
    inserts, a re-run for the same period updates the same row (e.g. to attach a
    Stripe invoice id or restate the amount after more usage landed). Returns the
    stored row via :func:`get_savings_invoice`.
    """
    ts = ts if ts is not None else time.time()
    existing = conn.execute(
        "SELECT id FROM savings_invoices WHERE org_id=? AND period_label=?",
        (org_id, period_label),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE savings_invoices SET gross_savings_micros=?, rate_bps=?, "
            "amount_micros=?, covered_events=?, total_events=?, "
            "stripe_invoice_id=COALESCE(?, stripe_invoice_id), status=?, ts=? "
            "WHERE org_id=? AND period_label=?",
            (int(gross_savings_micros), int(rate_bps), int(amount_micros),
             int(covered_events), int(total_events), stripe_invoice_id, status, ts,
             org_id, period_label),
        )
    else:
        conn.execute(
            "INSERT INTO savings_invoices(id,org_id,period_label,gross_savings_micros,"
            "rate_bps,amount_micros,covered_events,total_events,stripe_invoice_id,status,ts)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("savinv"), org_id, period_label, int(gross_savings_micros),
             int(rate_bps), int(amount_micros), int(covered_events),
             int(total_events), stripe_invoice_id, status, ts),
        )
    if commit:
        conn.commit()
    return get_savings_invoice(conn, org_id, period_label)


# ------------------------------------------------------------- stripe idemp ---
def stripe_event_seen(conn, event_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM stripe_events WHERE event_id=?", (event_id,)
    ).fetchone() is not None


def mark_stripe_event(conn, event_id: str, type_: str, commit: bool = True) -> bool:
    """Mark a Stripe event as processed atomically. Returns True if newly inserted.

    Call with ``commit=False`` inside a ``db.immediate`` block so the claim is
    atomic with the side effects it guards — a crash between the claim and the
    credit must not leave a claimed-but-unapplied event (silent credit loss)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO stripe_events(event_id,type,processed_at) VALUES(?,?,?)",
        (event_id, type_, time.time()),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def unmark_stripe_event(conn, event_id: str) -> None:
    """Remove a Stripe event claim (for rollback on side-effect failure)."""
    conn.execute("DELETE FROM stripe_events WHERE event_id=?", (event_id,))
    conn.commit()


# ------------------------------------------------------- ingest idempotency ---
# Fix #80 (review F3): how long a claimed-but-unanswered Idempotency-Key row may
# sit before it's treated as orphaned (the original request crashed between the
# claim commit and storing its response) and the key is allowed to be re-claimed,
# instead of 409-ing that key forever. Comfortably longer than any real request.
IDEMPOTENCY_INFLIGHT_GRACE = 120.0


def claim_idempotency_key(conn, org_id: str, idem_key: str,
                          ts: Optional[float] = None, commit: bool = True) -> bool:
    """Atomically claim an ingest Idempotency-Key for an org (fix #65).

    Returns True when newly claimed (caller should process and then store the
    response), False when the key was already seen (caller should replay the
    stored response instead of re-recording). Call with ``commit=False`` inside a
    ``db.immediate`` block so the claim is atomic with the recording it guards.

    Fix #80 (F3): an orphaned in-flight claim (``status`` still NULL, older than
    ``IDEMPOTENCY_INFLIGHT_GRACE`` — the original request died before storing its
    response) is reclaimable, so a crash can't 409 that key forever. A *completed*
    claim (``status`` set) is never reclaimed, preserving replay.
    """
    now = ts if ts is not None else time.time()
    conn.execute(
        "DELETE FROM ingest_idempotency WHERE org_id=? AND idem_key=? "
        "AND status IS NULL AND ts < ?",
        (org_id, idem_key, now - IDEMPOTENCY_INFLIGHT_GRACE),
    )
    cur = conn.execute(
        "INSERT OR IGNORE INTO ingest_idempotency(org_id,idem_key,status,response,ts)"
        " VALUES(?,?,NULL,NULL,?)",
        (org_id, idem_key, now),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def store_idempotency_response(conn, org_id: str, idem_key: str, status: int,
                               response: str, commit: bool = True) -> None:
    """Persist the response for a claimed Idempotency-Key so a later duplicate
    replays it verbatim."""
    conn.execute(
        "UPDATE ingest_idempotency SET status=?, response=? WHERE org_id=? AND idem_key=?",
        (int(status), response, org_id, idem_key),
    )
    if commit:
        conn.commit()


def idempotency_response(conn, org_id: str, idem_key: str):
    """Return ``(status, response)`` for a claimed key, or ``None`` if unknown.
    ``status`` is ``None`` when the original request is still in flight."""
    row = conn.execute(
        "SELECT status, response FROM ingest_idempotency WHERE org_id=? AND idem_key=?",
        (org_id, idem_key),
    ).fetchone()
    if row is None:
        return None
    return row["status"], row["response"]


def purge_idempotency(conn, older_than_seconds: float = 86400) -> int:
    """Housekeeping sweep: drop ingest idempotency rows older than the cutoff
    (default 24h) so the table can't grow without bound. Replay protection only
    needs to cover a client's realistic retry window. Returns rows removed."""
    cur = conn.execute(
        "DELETE FROM ingest_idempotency WHERE ts < ?",
        (time.time() - older_than_seconds,),
    )
    conn.commit()
    return cur.rowcount


# -------------------------------------------------------------- alerts log ---
def log_alert(conn, org_id: str, kind: str, message: str,
              workspace_id: Optional[str] = None, delivered: bool = False,
              commit: bool = True) -> sqlite3.Row:
    aid = new_id("alr")
    conn.execute(
        "INSERT INTO alerts_log(id,org_id,workspace_id,kind,message,delivered,ts)"
        " VALUES(?,?,?,?,?,?,?)",
        (aid, org_id, workspace_id, kind, message, int(delivered), time.time()),
    )
    if commit:
        conn.commit()
    return conn.execute("SELECT * FROM alerts_log WHERE id=?", (aid,)).fetchone()


def recent_alerts(conn, org_id: str, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alerts_log WHERE org_id=? ORDER BY ts DESC LIMIT ?",
        (org_id, limit),
    ).fetchall()
