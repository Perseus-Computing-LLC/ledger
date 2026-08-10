"""``ledger`` command-line interface.

Subcommands:

    ledger init                 create ~/.ledger/{config.yaml,ledger.db}
    ledger serve [--demo]       run the dashboard + API at :8420
    ledger demo                 serve with realistic sample data (no setup)
    ledger status               orgs, balances, Stripe mode
    ledger org create|list      manage organizations
    ledger workspace create|list  manage workspaces
    ledger meter ...            record a usage event (deplete credit)
    ledger topup ...            add prepaid credit
    ledger report ...           monthly PDF/HTML spend report
    ledger reconcile ...        true-up estimated cost to a provider's real billing
    ledger reconcile-webhooks   detect + replay Stripe events missed in deploy windows
    ledger alerts [--test]      deliver pending low-balance/budget alerts
    ledger monitor              print live provider runway (monitor bridge)

Everything except Stripe Checkout and email delivery works fully offline.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import (__version__, __tagline__, alerts, config as cfgmod, db, metering,
               pricing, reconcile, savings as savings_mod, efficiency as eff_mod)


# ----------------------------------------------------------------- helpers ---
def _conn():
    return db.connect()


def _resolve_org(conn, ident: str | None):
    if ident:
        o = db.get_org(conn, ident) or db.get_org_by_slug(conn, ident)
        if o:
            return o
        for o in db.list_orgs(conn):
            if o["name"] == ident:
                return o
        sys.exit(f"ledger: no organization '{ident}'")
    orgs = db.list_orgs(conn)
    if len(orgs) == 1:
        return orgs[0]
    if not orgs:
        sys.exit("ledger: no organizations. Run `ledger init` or `ledger org create NAME`.")
    sys.exit("ledger: multiple orgs — pass --org <id|slug|name>.")


def _ok(msg):
    print(f"  ✓ {msg}")


# ------------------------------------------------------------------ commands --
def cmd_init(args):
    path, created = cfgmod.ensure_initialized()
    _ok(f"config {'created' if created else 'present'}: {path}")
    conn = _conn()
    db.init_schema(conn)
    _ok(f"database ready: {cfgmod.db_path()}")
    if args.org:
        org = db.create_org(conn, args.org, tier=args.tier, owner_email=args.email)
        _ok(f"organization '{org['name']}' ({org['id']}) on {org['tier']} plan")
        if args.workspace:
            ws = db.create_workspace(conn, org["id"], args.workspace, args.budget)
            _ok(f"workspace '{ws['name']}' ({ws['id']})")
    conn.close()
    print(f"\n  Next: ledger serve   →   http://localhost:8420")
    print(f"        ledger demo    →   explore with sample data\n")


def cmd_serve(args, demo=False):
    from . import server
    cfg = cfgmod.load()
    if getattr(args, "allow_insecure", False):
        cfg.setdefault("server", {})["allow_insecure"] = True
    demo = demo or args.demo
    db_path = str(cfgmod.db_path())
    if demo:
        from . import demo as demo_mod
        db_path = str(cfgmod.home_dir() / "demo.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        for ext in ("", "-wal", "-shm"):
            p = Path(db_path + ext)
            if p.exists():
                p.unlink()
        c = db.connect(db_path)
        org_id = demo_mod.seed(c)
        c.close()
        sys.stderr.write(f"  seeded demo org {org_id}\n")
    server.serve(host=args.host, port=args.port, db_path=db_path, demo=demo,
                 cfg=cfg, open_browser=args.open)


def cmd_status(args):
    cfg = cfgmod.load()
    conn = _conn()
    from .billing import StripeClient
    st = StripeClient(cfg).status()
    print(f"\n  ◆ Ledger v{__version__} — {__tagline__}")
    print(f"    config:   {cfgmod.config_path()}")
    print(f"    database: {cfgmod.db_path()}")
    print(f"    stripe:   {st['mode']}\n")
    orgs = db.list_orgs(conn)
    if not orgs:
        print("    (no organizations — run `ledger init`)\n")
        conn.close()
        return
    print(f"    {'ORG':<22} {'TIER':<11} {'BALANCE':>11} {'MTD SPEND':>11}  WORKSPACES")
    print("    " + "-" * 72)
    for o in orgs:
        s = metering.org_spend_windows(conn, o["id"])
        bal = db.get_balance(conn, o["id"])
        nws = len(db.list_workspaces(conn, o["id"]))
        print(f"    {o['name'][:22]:<22} {o['tier']:<11} "
              f"${bal:>9,.2f} ${s['mtd']['cost']:>9,.2f}  {nws}")
    print()
    conn.close()


def cmd_org(args):
    conn = _conn()
    if args.action == "create":
        if not (args.name or "").strip():
            conn.close()
            sys.exit('ledger: `ledger org create` needs a NAME, '
                     'e.g. `ledger org create "Acme Inc"`')
        org = db.create_org(conn, args.name, tier=args.tier, owner_email=args.email)
        _ok(f"organization '{org['name']}' ({org['id']}) on {org['tier']} plan")
    elif args.action == "list":
        for o in db.list_orgs(conn):
            policy = " ⚠ allow-negative" if o["allow_negative_balance"] else ""
            print(f"  {o['id']}  {o['name']:<24} {o['tier']:<11} "
                  f"${db.get_balance(conn, o['id']):,.2f}{policy}")
    elif args.action in ("allow-negative", "enforce-balance"):
        org = _resolve_org(conn, args.name)
        allow = args.action == "allow-negative"
        db.set_org_allow_negative(conn, org["id"], allow)
        if allow:
            _ok(f"'{org['name']}' is now EXEMPT from the prepaid hard-stop "
                f"(track-only — usage may drive the balance negative)")
        else:
            _ok(f"'{org['name']}' now ENFORCES the prepaid hard-stop "
                f"(usage past a zero balance is rejected when block_over_balance is on)")
    conn.close()


def cmd_workspace(args):
    conn = _conn()
    org = _resolve_org(conn, args.org)
    if args.action == "create":
        if not (args.name or "").strip():
            conn.close()
            sys.exit('ledger: `ledger workspace create` needs a NAME, '
                     'e.g. `ledger workspace create prod`')
        ws = db.create_workspace(conn, org["id"], args.name, args.budget)
        cap = f"${args.budget:,.2f}/mo cap" if args.budget else "no cap"
        _ok(f"workspace '{ws['name']}' ({ws['id']}) — {cap}")
    elif args.action == "list":
        for w in db.list_workspaces(conn, org["id"]):
            cap = f"${w['monthly_budget_usd']:,.2f}/mo" if w["monthly_budget_usd"] else "no cap"
            print(f"  {w['id']}  {w['name']:<22} {cap}")
    conn.close()


def cmd_meter(args):
    conn = _conn()
    org = _resolve_org(conn, args.org)
    cfg = cfgmod.load()
    res = metering.record_usage(
        conn, org["id"], provider=args.provider, model=args.model,
        task_type=args.task, input_tokens=args.input, output_tokens=args.output,
        cache_read_tokens=args.cache, reasoning_tokens=args.reasoning,
        workspace=args.workspace, cost_usd=args.cost,
        baseline_cost_usd=getattr(args, "baseline", None),
        optimal_cost_usd=getattr(args, "optimal", None),
        external_ref=getattr(args, "ref", None), source="cli",
        pricing_overrides=cfg.get("pricing", {}).get("overrides"),
        alert_cfg=cfg.get("alerts", {}),
        block_over_limit=bool(cfg.get("pricing", {}).get("block_over_free_limit")),
    )
    if args.json:
        from dataclasses import asdict
        print(json.dumps(asdict(res), default=str, indent=2))
    elif not res.recorded:
        _ok(f"NOT metered — free-tier token quota reached. Upgrade to Pro for "
            f"unlimited tracking. (set pricing.block_over_free_limit=false to keep recording)")
    else:
        tag = "estimated" if res.estimated else "exact"
        _ok(f"metered {args.provider}/{args.model or '-'} {args.task}: "
            f"${res.cost_usd:.6f} ({tag}) → balance ${res.balance_after:,.2f}")
        if res.over_free_limit:
            print("  ▲ over free-tier token quota — upgrade to Pro for unlimited tracking")
        for a in res.alerts:
            print(f"  ▲ {a['kind']}: {a['message']}")
    conn.close()


def cmd_keys(args):
    conn = _conn()
    org = _resolve_org(conn, args.org)
    if args.action == "create":
        _, secret = db.create_api_key(conn, org["id"], name=args.name)
        _ok(f"API key for '{org['name']}' — store it now, it won't be shown again:")
        print(f"    {secret}")
    elif args.action == "create-scoped":
        scope = None
        if args.workspace:
            scope = {"workspaces": [args.workspace]}
        row, secret = db.create_api_key_scoped(
            conn, org["id"], name=args.name, scope=scope)
        _ok(f"Scoped API key for '{org['name']}' — store it now:")
        scope_txt = f" (scoped to workspace '{args.workspace}')" if args.workspace else ""
        print(f"    {secret}{scope_txt}")
    elif args.action == "list":
        keys = db.list_api_keys(conn, org["id"])
        if not keys:
            print("  (no API keys)")
        for k in keys:
            used = "never" if not k["last_used_at"] else f"{int((time.time()-k['last_used_at'])/86400)}d ago"
            scope_txt = ""
            if k.get("scope"):
                try:
                    scope = json.loads(k["scope"])
                    if scope.get("workspaces"):
                        scope_txt = f"  [ws:{','.join(scope['workspaces'])}]"
                except Exception:
                    pass
            event_txt = f" ev:{k.get('event_count', 0)}" if k.get("event_count") else ""
            print(f"  {k['id']}  {k['prefix']+'…':<22} {(k['name'] or '-'):<18} used {used}{scope_txt}{event_txt}")
    elif args.action == "revoke":
        if not args.key_id:
            sys.exit("ledger: pass the key id to revoke, e.g. `ledger keys revoke key_…`")
        if db.revoke_api_key(conn, args.key_id, org["id"]):
            _ok(f"revoked {args.key_id}")
        else:
            print(f"  no active key '{args.key_id}' for this org")
    elif args.action == "rotate":
        if not args.key_id:
            sys.exit("ledger: pass the key id to rotate, e.g. `ledger keys rotate key_…`")
        overlap = getattr(args, "overlap", 300)
        new_row, secret, old_row = db.rotate_api_key(
            conn, org["id"], args.key_id, overlap_seconds=overlap,
            name=args.name)
        _ok(f"rotation started for {args.key_id}")
        print(f"    new key id  : {new_row['id']}")
        print(f"    new secret  : {secret}")
        print(f"    old key expires in {overlap}s (zero-downtime)")
        if overlap > 0:
            print(f"  To complete now: `ledger keys rotate-complete {new_row['id']}`")
    elif args.action == "rotate-complete":
        if not args.key_id:
            sys.exit("ledger: pass the id of the NEW key to complete rotation")
        if db.complete_key_rotation(conn, org["id"], args.key_id):
            _ok(f"rotation completed — old key revoked")
        else:
            print(f"  no active rotation found for {args.key_id}")
    elif args.action == "rotate-now":
        if not args.key_id:
            sys.exit("ledger: pass the key id to rotate and immediately revoke")
        new_row, secret = db.rotate_and_revoke(
            conn, org["id"], args.key_id, name=args.name)
        _ok(f"emergency rotation — old key immediately revoked")
        print(f"    new key id  : {new_row['id']}")
        print(f"    new secret  : {secret}")
    conn.close()


def cmd_ingest_health(args):
    """#150: Ingestion health diagnostics per source."""
    conn = _conn()
    org = _resolve_org(conn, args.org)
    rows = db.get_ingest_health(conn, org["id"])
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        _ok("no ingestion events recorded yet")
        return
    print(f"\n  ingest health for '{org['name']}' — last-24h view\n")
    print(f"    {'SOURCE':<30} {'STATUS':<8} {'EVENTS':>8} {'ERRORS':>8}  LAST SEEN")
    print("    " + "-" * 78)
    now = time.time()
    for r in rows:
        status = "✓" if r["last_ok"] else "✗"
        last = "never" if not r["last_ts"] else (
            f"{int((now - r['last_ts'])/60)}m ago" if (now - r["last_ts"]) < 3600
            else f"{int((now - r['last_ts'])/3600)}h ago")
        err = (r["last_error"] or "")[:40]
        print(f"    {r['source']:<30} {status:<8} {r['total_events']:>8} {r['total_errors']:>8}  {last}")
        if err:
            print(f"      ↳ {err}")
    print()


def cmd_topup(args):
    conn = _conn()
    org = _resolve_org(conn, args.org)
    row = db.add_ledger(conn, org["id"], args.amount, "topup",
                        reason=args.reason or "manual top-up (cli)")
    _ok(f"added ${args.amount:,.2f} to '{org['name']}' → balance ${row['balance_after']:,.2f}")
    conn.close()


def cmd_report(args):
    from . import reports
    conn = _conn()
    org = _resolve_org(conn, args.org)
    if args.month:
        year, month = (int(x) for x in args.month.split("-"))
    else:
        import datetime as dt
        now = dt.date.today()
        year, month = now.year, now.month
    rep = reports.build_report(conn, org["id"], year, month)
    out = args.out or f"ledger-{org['slug']}-{year}-{month:02d}.pdf"
    path = reports.write(rep, out)
    kind = "PDF" if path.suffix == ".pdf" else "HTML (install reportlab for PDF)"
    _ok(f"{reports.MONTHS[month]} {year} report → {path} [{kind}]")
    _ok(f"total ${rep['total']['cost']:,.2f} · {rep['total']['tokens']:,} tokens · "
        f"{rep['total']['events']:,} calls")
    conn.close()


def cmd_alerts(args):
    from . import alerts
    cfg = cfgmod.load()
    conn = _conn()
    org = _resolve_org(conn, args.org) if args.org else None
    results = alerts.check_and_notify(conn, cfg, org["id"] if org else None)
    for r in results:
        if r.get("dry_run"):
            print(f"  (dry run) {r['org_id']}: {r['pending']} pending — {r.get('detail','')}")
            for m in r.get("would_send", []):
                print(f"      ▲ {m}")
        else:
            print(f"  {r['org_id']}: sent {r['sent']}, {r['pending']} pending"
                  + (f" — error: {r['error']}" if r.get("error") else ""))
    conn.close()


def cmd_monitor(args):
    from . import bridge
    cfg = cfgmod.load()
    data = bridge.runway(cfg.get("monitor", {}))
    if data is None:
        print("  monitor bridge disabled or unavailable. Set monitor.enabled + "
              "monitor.command in config.yaml to fold live provider runway into "
              "the dashboard.")
        return
    print(json.dumps(data, indent=2))


HOOK_MODULE = "ledger_agent.integrations.claude_code_hook"


def _hook_command():
    exe = sys.executable or "python"
    q = f'"{exe}"' if " " in exe else exe
    return f"{q} -m {HOOK_MODULE}"


def _merge_stop_hook(settings: dict, command: str):
    """Merge a Stop-hook entry into a Claude Code settings dict (idempotent).

    Returns (settings, changed). Pure function so it's easy to test.
    """
    hooks = settings.setdefault("hooks", {})
    stop = hooks.setdefault("Stop", [])
    for group in stop:
        for h in (group or {}).get("hooks", []):
            if HOOK_MODULE in (h.get("command") or ""):
                return settings, False
    stop.append({"hooks": [{"type": "command", "command": command}]})
    return settings, True


def cmd_install_hook(args):
    command = _hook_command()
    if args.print:
        import json as _json
        snippet = {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": command}]}]}}
        print(_json.dumps(snippet, indent=2))
        print(f"\n  Add the above to {args.path or '~/.claude/settings.json'}")
        return
    path = Path(args.path) if args.path else (Path.home() / ".claude" / "settings.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception as e:
            sys.exit(f"ledger: could not parse {path}: {e}")
        # Back up the *pristine* original once — copying the raw bytes so comments
        # and formatting survive. Re-running must not clobber that first backup
        # with an already-modified file, so we only write it if none exists.
        backup = path.with_suffix(path.suffix + ".ledger-bak")
        if not backup.exists():
            import shutil
            shutil.copy2(path, backup)
    settings, changed = _merge_stop_hook(settings, command)
    if not changed:
        _ok(f"Claude Code hook already installed in {path}")
        return
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    _ok(f"installed Ledger Stop hook → {path}")
    _ok("every Claude Code turn now meters into Ledger (org 'Claude Code', "
        "workspace = project name)")
    print(f"\n  Try it:  run a Claude Code turn, then  ledger serve  → http://localhost:8420")
    print(f"  Set LEDGER_ORG to attribute turns to a specific org.\n")


def cmd_stripe_setup(args):
    cfg = cfgmod.load()
    key = cfg.get("billing", {}).get("stripe_secret_key") or ""
    if not key:
        sys.exit("ledger: no Stripe key. Set STRIPE_SECRET_KEY (use a sk_test_… key first).")
    try:
        import stripe
    except ImportError:
        sys.exit("ledger: Stripe SDK not installed. Run `pip install 'ledger-agent[stripe]'`.")
    stripe.api_key = key
    mode = "TEST" if key.startswith("sk_test_") else "LIVE"
    print(f"  Stripe {mode} mode — setting up the Pro plan…")

    lookup = "ledger_pro_monthly"
    price = None
    try:
        existing = stripe.Price.list(lookup_keys=[lookup], limit=1, expand=["data.product"])
        if existing.data:
            price = existing.data[0]
            _ok(f"found existing Pro price: {price.id}")
    except Exception:
        pass
    if price is None:
        product = stripe.Product.create(
            name="Ledger Pro",
            description="Ledger Pro — unlimited tracking, prepaid credits, alerts, reports.",
        )
        price = stripe.Price.create(
            product=product.id, unit_amount=2000, currency="usd",
            recurring={"interval": "month"}, lookup_key=lookup,
        )
        _ok(f"created Pro product {product.id} + price {price.id} ($20/mo)")

    # Save onto the file-only config (never the env-merged one) so the live
    # key provided via STRIPE_SECRET_KEY is not persisted to disk.
    to_save = cfgmod.load_base()
    to_save["billing"]["stripe_price_pro"] = price.id
    saved = cfgmod.save(to_save)
    _ok(f"wrote stripe_price_pro to {saved} (key NOT persisted — keep it in env)")
    print("\n  Next:")
    print("    1. ledger serve                       # dashboard with Checkout enabled")
    print("    2. stripe listen --forward-to localhost:8420/webhook/stripe")
    print("    3. Buy credit on the dashboard, or:")
    print("       stripe trigger checkout.session.completed")
    print("    4. Watch the balance top up. See BILLING.md for the full flow.\n")


def cmd_reconcile(args):
    conn = _conn()
    org = _resolve_org(conn, args.org)
    totals: dict = {}
    if args.totals:
        totals = reconcile.load_authoritative(args.totals)
    if args.provider is not None and args.amount is not None:
        totals[args.provider] = args.amount
    if not totals:
        conn.close()
        sys.exit("ledger: supply --totals FILE (provider->USD from the provider "
                 "export) or --provider NAME --amount USD (the provider's own "
                 "billed total for the period)")
    start_ts = end_ts = None
    if args.period:
        start_ts, end_ts = reconcile.month_window(args.period)
    rep = reconcile.reconcile(conn, org["id"], totals,
                              period_label=args.period or "all",
                              start_ts=start_ts, end_ts=end_ts, apply=args.apply)
    if args.json:
        print(json.dumps(rep.as_dict(), indent=2))
        conn.close()
        return
    mode = "APPLIED" if args.apply else "DRY RUN (pass --apply to write)"
    print(f"\n  reconcile {rep.period_label} for '{org['name']}' - {mode}\n")
    print(f"    {'PROVIDER':<12} {'RECORDED':>12} {'AUTHORITATIVE':>14} {'ADJUST':>12}  NOTE")
    print("    " + "-" * 74)
    for i in rep.items:
        print(f"    {i.provider:<12} ${i.recorded_usd:>10.4f} "
              f"${i.authoritative_usd:>12.4f} ${i.delta_usd:>+10.4f}  {i.note}")
    print("    " + "-" * 74)
    label = "balance" if args.apply else "projected balance"
    print(f"    net adjust ${rep.total_adjust_usd:+,.4f}  ->  {label} "
          f"${rep.balance_after_usd:,.4f}")
    if rep.unreconciled_providers:
        print(f"\n  not reconciled (no authoritative total supplied): "
              f"{', '.join(rep.unreconciled_providers)}")
    _ok("adjust entries written" if args.apply else "no changes written (dry run)")
    conn.close()


def cmd_reconcile_webhooks(args):
    """#177: diff Stripe's event log vs stripe_events; replay gaps with --apply."""
    from . import reconcile_webhooks as rw
    cfg = cfgmod.load()
    secret = (cfg.get("billing") or {}).get("stripe_secret_key") or \
        os.environ.get("STRIPE_SECRET_KEY", "")
    if not secret:
        raise SystemExit(
            "reconcile-webhooks: no Stripe key — set billing.stripe_secret_key "
            "or the STRIPE_SECRET_KEY env var.")
    types = [t.strip() for t in args.types.split(",") if t.strip()] \
        if args.types else None
    conn = _conn()
    try:
        report = rw.reconcile(conn, secret, days=args.days, types=types,
                              apply=args.apply)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        sys.exit(2 if report["missing"] and not args.apply else 0)

    print(f"\n  stripe webhook reconciliation — last {report['window_days']:g}d\n")
    print(f"    stripe events: {report['stripe_events_found']} | "
          f"already applied: {report['already_processed']} | "
          f"missing: {len(report['missing'])}")
    for m in report["missing"]:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(m["created"] or 0))
        print(f"    MISSING {m['id']}  {m['type']}  {ts}")
    for r in report["applied"]:
        print(f"    APPLIED {r.get('id', '')}  {r.get('type', '')}  "
              f"-> {r.get('status', '')}")
    if report["missing"] and not args.apply:
        print("\n  dry-run — pass --apply to replay via the app handler\n")
    else:
        print()


def cmd_verify(args):
    """#108: walk the usage_events tamper-evidence chain and report divergences."""
    conn = _conn()
    if args.hmac_key is not None:
        hmac_key = args.hmac_key.encode("utf-8") if args.hmac_key else None
    else:
        hmac_key = cfgmod.chain_hmac_key(cfgmod.load())
    org_id = None
    if args.org:
        org_id = _resolve_org(conn, args.org)["id"]
    report = db.verify_chain(conn, org_id=org_id, hmac_key=hmac_key)
    conn.close()

    if args.json:
        print(json.dumps(report, indent=2))
        sys.exit(0 if report["ok"] else 2)

    mode = "keyed HMAC-SHA256" if hmac_key else "SHA-256"
    print(f"\n  ledger integrity — usage_events hash chain ({mode})\n")
    print(f"    {'ORG':<28} {'EVENTS':>7} {'VERIFIED':>9} {'PRE-CHAIN':>10}  STATUS")
    print("    " + "-" * 72)
    for o in report["orgs"]:
        mark = {"ok": "✓", "broken": "✗", "empty": "·"}.get(o["status"], "?")
        print(f"    {o['org_id']:<28} {o['events']:>7} {o['verified']:>9} "
              f"{o['pre_chain']:>10}  {mark} {o['status']}")
        if o["first_divergence"]:
            d = o["first_divergence"]
            print(f"        ↳ first divergence at event {d['event_id']} "
                  f"(rowid {d['rowid']}): {d['reason']}")
    print("    " + "-" * 72)
    if report["ok"]:
        _ok("chain intact — no tampering detected")
        sys.exit(0)
    else:
        print("  ✗ chain BROKEN — see divergence(s) above")
        sys.exit(2)


def cmd_checkpoint(args):
    """#120: escrow a signed tamper-evidence checkpoint the customer retains.

    Prints the checkpoint as a JSON line — the operator hands (or the customer
    fetches) this and stores it OUT OF BAND (git, email, S3 object-lock). Later,
    ``ledger verify-checkpoints`` replays it against the live DB; an operator who
    rewrote history cannot reproduce a head the customer already holds.
    """
    conn = _conn()
    if args.hmac_key is not None:
        hmac_key = args.hmac_key.encode("utf-8") if args.hmac_key else None
    else:
        hmac_key = cfgmod.chain_hmac_key(cfgmod.load())
    if args.org:
        orgs = [_resolve_org(conn, args.org)["id"]]
    else:
        orgs = [r["id"] for r in db.list_orgs(conn)]
    out = []
    deliveries = []
    for oid in orgs:
        cp = db.checkpoint_chain(conn, oid, hmac_key=hmac_key)
        if cp is not None:
            out.append(cp)
            if getattr(args, "deliver", False):
                org_row = db.get_org(conn, oid)
                deliveries.append((oid, alerts.mail_checkpoint(
                    cfgmod.load(), org_row["name"] if org_row else oid, cp,
                    force=True)))
    conn.close()
    if args.json:
        print(json.dumps({"checkpoints": out,
                          "deliveries": [{"org_id": o, **d} for o, d in deliveries]}
                         if deliveries else out, indent=2))
        sys.exit(0)
    if not out:
        _ok("no chained events yet — nothing to checkpoint")
        sys.exit(0)
    mode = "keyed HMAC-SHA256" if hmac_key else "SHA-256"
    print(f"\n  tamper-evidence checkpoints ({mode}) — RETAIN THESE OUT OF BAND\n")
    for cp in out:
        print(f"    {cp['org_id']:<28} rowid {cp['through_rowid']:>7}  "
              f"{cp['event_count']:>6} events  head {cp['head_hash'][:16]}…")
        print(f"      {json.dumps(cp)}")
    print()
    for oid, d in deliveries:
        if d.get("sent"):
            print(f"    ✉ {oid}: receipt emailed to {', '.join(d['to'])}")
        else:
            print(f"    ✉ {oid}: NOT sent — {d.get('error') or d.get('detail')}")
    _ok(f"{len(out)} checkpoint(s) recorded — save the JSON line(s) above")
    sys.exit(0)


def cmd_verify_checkpoints(args):
    """#120: replay customer-retained checkpoints against the live chain."""
    conn = _conn()
    if args.hmac_key is not None:
        hmac_key = args.hmac_key.encode("utf-8") if args.hmac_key else None
    else:
        hmac_key = cfgmod.chain_hmac_key(cfgmod.load())

    checkpoints = []
    if args.file:
        raw = sys.stdin.read() if args.file == "-" else open(args.file).read()
        raw = raw.strip()
        if raw.startswith("["):
            checkpoints = json.loads(raw)
        else:
            checkpoints = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
    else:
        # No retained file supplied: fall back to the checkpoints stored in this
        # DB. Weaker (an operator could rewrite these too) but still catches
        # accidental corruption; the strong guarantee needs --file with an
        # out-of-band copy.
        if args.org:
            checkpoints = db.list_checkpoints(conn, _resolve_org(conn, args.org)["id"])
        else:
            for r in db.list_orgs(conn):
                checkpoints += db.list_checkpoints(conn, r["id"])

    report = db.verify_checkpoints(conn, checkpoints, hmac_key=hmac_key)
    conn.close()

    if args.json:
        print(json.dumps(report, indent=2))
        sys.exit(0 if report["ok"] else 2)

    src = args.file or "(checkpoints stored in this DB — supply --file for the strong guarantee)"
    print(f"\n  checkpoint verification — source: {src}\n")
    print(f"    {'ORG':<28} {'ROWID':>7} {'EVENTS':>7}  STATUS")
    print("    " + "-" * 66)
    for c in report["checkpoints"]:
        mark = "✓" if c["status"] == "ok" else "✗"
        print(f"    {c['org_id']:<28} {c['through_rowid']:>7} "
              f"{str(c['event_count']):>7}  {mark} {c['status']}")
        if c["detail"]:
            print(f"        ↳ {c['detail']}")
    print("    " + "-" * 66)
    if report["ok"]:
        _ok("all checkpoints reproduce — history is intact and independently confirmed")
        sys.exit(0)
    print("  ✗ checkpoint(s) FAILED — the live ledger no longer matches a retained anchor")
    sys.exit(2)


def cmd_close(args):
    """#109: fetch provider authoritative totals and reconcile — the cron close.

    #121: an applied close also records (and optionally emails) a
    tamper-evidence checkpoint, so every billing period ends anchored."""
    conn = _conn()
    cfg = cfgmod.load()
    org = _resolve_org(conn, args.org)
    period = args.period or reconcile.previous_month_label()
    providers = None
    if args.providers:
        providers = [p.strip().lower() for p in args.providers.split(",") if p.strip()]
    out = reconcile.close_period(conn, org["id"], period,
                                 providers=providers, apply=args.apply,
                                 checkpoint=getattr(args, "checkpoint", True),
                                 hmac_key=cfgmod.chain_hmac_key(cfg))
    conn.close()
    if out.get("checkpoint") and getattr(args, "deliver", False):
        out["checkpoint_delivery"] = alerts.mail_checkpoint(
            cfg, org["name"], out["checkpoint"], force=True)

    if args.json:
        print(json.dumps(out, indent=2))
        # non-zero exit if a requested provider could not be fetched, so a cron
        # job surfaces the gap instead of silently under-reconciling.
        sys.exit(1 if out["fetch_errors"] else 0)

    mode = "APPLIED" if args.apply else "DRY RUN (pass --apply to write)"
    print(f"\n  close {period} for '{org['name']}' — {mode}\n")
    if out["fetched"]:
        print("    fetched authoritative totals:")
        for prov, usd in sorted(out["fetched"].items()):
            print(f"      {prov:<12} ${usd:,.4f}")
    else:
        print("    fetched authoritative totals: (none)")
    print(f"    {'PROVIDER':<12} {'RECORDED':>12} {'AUTHORITATIVE':>14} {'ADJUST':>12}  NOTE")
    print("    " + "-" * 74)
    for i in out["items"]:
        print(f"    {i['provider']:<12} ${i['recorded_usd']:>10.4f} "
              f"${i['authoritative_usd']:>12.4f} ${i['delta_usd']:>+10.4f}  {i['note']}")
    print("    " + "-" * 74)
    label = "balance" if args.apply else "projected balance"
    print(f"    net adjust ${out['total_adjust_usd']:+,.4f}  ->  {label} "
          f"${out['balance_after_usd']:,.4f}")
    if out["fetch_errors"]:
        print("\n  ⚠ could not fetch (left unreconciled — NOT zeroed):")
        for prov, msg in sorted(out["fetch_errors"].items()):
            print(f"      {prov}: {msg}")
    if out["unreconciled_providers"]:
        print(f"\n  not reconciled (no authoritative total): "
              f"{', '.join(out['unreconciled_providers'])}")
    cp = out.get("checkpoint")
    if cp:
        print(f"\n    ⚓ checkpoint recorded — rowid {cp['through_rowid']:,}, "
              f"{cp['event_count']:,} events, head {cp['head_hash'][:16]}… "
              f"({cp['mode']})")
        print(f"      {json.dumps(cp)}")
        print("      retain the JSON line above out of band "
              "(ledger verify-checkpoints --file)")
        d = out.get("checkpoint_delivery")
        if d is not None:
            if d.get("sent"):
                print(f"      ✉ receipt emailed to {', '.join(d['to'])}")
            else:
                print(f"      ✉ NOT emailed — {d.get('error') or d.get('detail')}")
    _ok("adjust entries written" if args.apply else "no changes written (dry run)")
    if out["fetch_errors"]:
        sys.exit(1)


def _savings_rate_bps(cfg, args, tier_key=None) -> int:
    """Resolve the savings-share rate.

    Explicit --rate wins. Otherwise Enterprise uses its canonical 10% contract;
    other tiers use the configured default for backward-compatible reports.
    """
    if getattr(args, "rate", None) is not None:
        pct = float(args.rate)
        if not (0.0 <= pct <= 100.0):
            sys.exit("ledger: --rate must be a percentage between 0 and 100")
        return int(round(pct * 100))
    if tier_key and pricing.tier(tier_key).savings_share_bps:
        return pricing.tier(tier_key).savings_share_bps
    return savings_mod.rate_bps_from_config(cfg)


def _print_savings_report(d, org_name, mode):
    print(f"\n  savings-share {d['period']} for '{org_name}' — {mode}\n")
    cov = "" if d["coverage_pct"] is None else f" ({d['coverage_pct']:.0f}% coverage)"
    print(f"    events with a baseline : {d['covered_events']}/{d['total_events']}{cov}")
    if d.get("window"):
        print(f"    authoritative events   : {d.get('authoritative_events', 0)}")
        print(f"    estimated events       : {d.get('estimated_events', 0)}")
        print(f"    reconciliation         : {d.get('reconciliation_status', 'unknown')}")
        print(f"    fresh at               : {d.get('freshness_ts')}")
    print(f"    billable (cost > $0)   : {d.get('billable_events', d['covered_events'])}")
    print(f"    baseline cost          : ${d['baseline_usd']:,.4f}")
    print(f"    actual cost (covered)  : ${d['cost_on_covered_usd']:,.4f}")
    print(f"    verified savings       : ${d['gross_savings_usd']:,.4f}")
    print(f"    share rate             : {d['rate_pct']:.1f}%")
    print(f"    ── billable share      : ${d['billable_share_usd']:,.4f}")
    if d.get("billing_blocked"):
        print("    ⛔ billing blocked     : coverage below minimum threshold")
    if d.get("billing_provisional"):
        print("    ⚠  billing provisional : estimated events exceed threshold")
    if d.get("already_invoiced"):
        print(f"\n    already invoiced (Stripe {d.get('stripe_invoice_id')})")
    for n in d.get("notes", []):
        print(f"    · {n}")


def cmd_savings(args):
    """#7: dry-run savings-share figure for a period (reads only)."""
    cfg = cfgmod.load()
    conn = _conn()
    org = _resolve_org(conn, args.org)
    period = args.period or savings_mod.previous_month_label()
    rate_bps = _savings_rate_bps(cfg, args, org["tier"])
    if getattr(args, "window", None):
        rep = savings_mod.operational_savings_report(
            conn, org["id"], args.window, rate_bps=rate_bps
        )
    else:
        rep = savings_mod.savings_share_report(conn, org["id"], period, rate_bps=rate_bps)
    conn.close()
    d = rep.as_dict()
    if args.json:
        print(json.dumps(d, indent=2))
        return
    _print_savings_report(d, org["name"], "REPORT")


def cmd_bill_savings(args):
    """#7: compute and (with --apply) raise a savings-share invoice."""
    cfg = cfgmod.load()
    conn = _conn()
    org = _resolve_org(conn, args.org)
    # Savings-share is mandatory only on Team; Pro is waived (flat $20) and Free
    # is a voluntary tip. Guard --apply on non-mandatory tiers behind --force so a
    # Pro/Free org is never invoiced by reflex.
    mode = pricing.savings_mode(org["tier"])
    if args.apply and mode in ("waived", "suggested", "none") and not getattr(args, "force", False):
        conn.close()
        why = {"waived": "Pro is a flat $20/mo — savings-share is waived",
               "suggested": "Free treats savings-share as a voluntary tip",
               "none": "this tier has no savings-share"}.get(mode, mode)
        sys.exit(f"ledger: won't bill savings-share on a '{org['tier']}' org — {why}. "
                 f"Mandatory billing is a Team feature. Pass --force to override.")
    period = args.period or savings_mod.previous_month_label()
    rate_bps = _savings_rate_bps(cfg, args, org["tier"])
    stripe_client = None
    if args.apply:
        from .billing import StripeClient
        stripe_client = StripeClient(cfg)
    billing_cfg = cfg.get("billing", {})
    out = savings_mod.bill_savings_share(
        conn, org["id"], period, rate_bps=rate_bps,
        stripe_client=stripe_client, apply=args.apply,
        min_charge_usd=billing_cfg.get("savings_min_charge_usd", 0.50),
        min_coverage_pct=savings_mod.coverage_threshold_from_config(cfg),
        max_estimated_pct=savings_mod.estimated_threshold_from_config(cfg),
    )
    conn.close()
    if args.json:
        print(json.dumps(out, indent=2))
        return
    mode = "APPLIED" if args.apply else "DRY RUN (pass --apply to bill)"
    _print_savings_report(out, org["name"], mode)
    if args.apply:
        _ok(f"savings-share recorded (status: {out['status']})"
            + (f", Stripe invoice {out['stripe_invoice_id']}"
               if out.get("stripe_invoice_id") else ""))


def cmd_efficiency(args):
    """#8: value-vs-actual efficiency for an org+period."""
    cfg = cfgmod.load()
    conn = _conn()
    org = _resolve_org(conn, args.org)
    period = None if args.all else (args.period or eff_mod.previous_month_label())
    rep = eff_mod.org_efficiency(
        conn, org["id"], period_label=period,
        baseline_models=eff_mod.baseline_models_from_config(cfg),
        pricing_overrides=cfg.get("pricing", {}).get("overrides"),
        actual_paid_usd=args.actual)
    conn.close()
    d = rep.as_dict()
    if args.json:
        print(json.dumps(d, indent=2))
        return
    print(f"\n  efficiency — '{org['name']}' ({d['period']})\n")
    print(f"    events / tokens       : {d['events']:,} / {d['tokens']:,}")
    print(f"    flagship-equiv value  : ${d['flagship_value_usd']:,.2f}   "
          f"(same tokens on the best API model)")
    print(f"    API-list value        : ${d['list_value_usd']:,.2f}   "
          f"(the models you actually used, at API prices)")
    basis_label = "actual paid (console)" if d['actual_paid_usd'] is not None \
        else "metered cost (no console reconcile yet)"
    print(f"    {basis_label:<22}: ${d['basis_usd']:,.2f}")
    print(f"    ── efficiency         : ${d['efficiency_usd']:,.2f}"
          + (f"   ({d['multiple']}x value-for-money)" if d['multiple'] else ""))
    if d.get("policy_events"):
        adh = d.get("adherence_pct")
        print(f"\n    policy adherence      : {d['on_policy_events']}/{d['policy_events']}"
              + (f" ({adh:.0f}% on-policy)" if adh is not None else ""))
        print(f"    ── leaked (off-policy): ${d['leaked_usd']:,.2f}   "
              f"(missed savings from turns above the policy-optimal)")
    if d["by_family"]:
        print("\n    by provider family:")
        for fam, a in sorted(d["by_family"].items(),
                             key=lambda x: -x[1]["flagship_value_usd"]):
            print(f"      {fam:<12} {a['events']:>5} ev  "
                  f"${a['flagship_value_usd']:>9,.2f} flagship-value")


def cmd_version(args):
    print(f"ledger v{__version__} — {__tagline__}")


def cmd_mcp(args):
    """Run the MCP stdio server (agents meter/query/verify themselves)."""
    from . import mcp_server
    mcp_server.serve(demo_mode=bool(getattr(args, "demo", False)))
    return 0


def cmd_backtest(args):
    """Replay historical estimated events without mutating the ledger (#145)."""
    from . import backtest
    conn = _conn()
    org = _resolve_org(conn, args.org)
    report = backtest.replay(
        conn, org["id"],
        tolerance_micros=args.tolerance_micros,
    )
    conn.close()
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        status = "PASS" if report.passed else "FAIL"
        print(f"  backtest {status}: {report.checked} events, "
              f"{report.mismatches} mismatches, "
              f"tolerance {report.tolerance_micros} micros")
    if not report.passed:
        raise SystemExit(2)


def cmd_pricing(args):
    print(f"\n  Ledger plans — {__tagline__}\n")
    for key in pricing.TIER_ORDER:
        t = pricing.tier(key)
        if key == "team":
            price = f"${t.per_seat_usd_month:.0f}/seat/mo + savings-share"
        elif key == "enterprise":
            price = "custom"
        else:
            price = "free" if t.price_usd_month == 0 else f"${t.price_usd_month:.0f}/mo"
        seats = "unlimited seats" if t.seats is None else f"up to {t.seats} seats"
        print(f"  {t.name} ({price}, {seats})")
        for f in t.features:
            print(f"     · {f}")
        print()
    cfg = cfgmod.load()
    rate = savings_mod.rate_bps_from_config(cfg) / 100.0
    print(f"  Savings-share (opt-in, Pro & Enterprise)")
    print(f"     · {rate:.0f}% of independently-verified monthly savings")
    print(f"     · billed only on savings you can reconstruct from a tamper-")
    print(f"       evident usage chain — never a blanket percentage")
    print()


# -------------------------------------------------------------------- parser --
def build_parser():
    p = argparse.ArgumentParser(
        prog="ledger", description=f"Ledger — {__tagline__}")
    p.add_argument("--version", action="version", version=f"ledger v{__version__}")
    p.add_argument("--db", help="database path (overrides LEDGER_DB)")
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("init", help="create config + database")
    pi.add_argument("--org", help="also create this organization")
    pi.add_argument("--email", help="owner email for the org")
    pi.add_argument("--tier", default="free", choices=["free", "pro", "team", "enterprise"])
    pi.add_argument("--workspace", help="also create this workspace")
    pi.add_argument("--budget", type=float, help="workspace monthly budget USD")
    pi.set_defaults(func=cmd_init)

    ps = sub.add_parser("serve", help="run dashboard + API at :8420")
    ps.add_argument("--host"); ps.add_argument("--port", type=int)
    ps.add_argument("--demo", action="store_true", help="serve realistic sample data")
    ps.add_argument("--open", action="store_true", help="open a browser")
    ps.add_argument("--allow-insecure", action="store_true",
                    help="permit binding a non-loopback host with auth disabled "
                         "(trusted networks only; the default fails closed)")
    ps.set_defaults(func=cmd_serve)

    pd = sub.add_parser("demo", help="serve with sample data (zero setup)")
    pd.add_argument("--host"); pd.add_argument("--port", type=int)
    pd.add_argument("--open", action="store_true")
    pd.add_argument("--allow-insecure", action="store_true",
                    help="permit binding a non-loopback host with auth disabled")
    pd.set_defaults(func=lambda a: cmd_serve(a, demo=True), demo=True)

    pm = sub.add_parser(
        "mcp", help="run the MCP stdio server (agents meter/query/verify "
                    "ledger events themselves)")
    pm.add_argument("--demo", action="store_true",
                    help="seed sample data into a throwaway database first")
    pm.set_defaults(func=cmd_mcp)

    sub.add_parser("status", help="show orgs, balances, Stripe mode").set_defaults(func=cmd_status)

    po = sub.add_parser("org", help="manage organizations")
    po.add_argument("action",
                    choices=["create", "list", "allow-negative", "enforce-balance"])
    po.add_argument("name", nargs="?")
    po.add_argument("--tier", default="free", choices=["free", "pro", "team", "enterprise"])
    po.add_argument("--email")
    po.set_defaults(func=cmd_org)

    pw = sub.add_parser("workspace", help="manage workspaces")
    pw.add_argument("action", choices=["create", "list"])
    pw.add_argument("name", nargs="?")
    pw.add_argument("--org"); pw.add_argument("--budget", type=float)
    pw.set_defaults(func=cmd_workspace)

    pm = sub.add_parser("meter", help="record a usage event")
    pm.add_argument("--org"); pm.add_argument("--provider", required=True)
    pm.add_argument("--model"); pm.add_argument("--task", default="general")
    pm.add_argument("--workspace")
    pm.add_argument("--input", type=int, default=0)
    pm.add_argument("--output", type=int, default=0)
    pm.add_argument("--cache", type=int, default=0)
    pm.add_argument("--reasoning", type=int, default=0)
    pm.add_argument("--cost", type=float, help="exact cost USD (else estimated)")
    pm.add_argument("--baseline", type=float,
                    help="counterfactual cost USD without Perseus (records "
                         "savings for savings-share billing, #7)")
    pm.add_argument("--optimal", type=float,
                    help="cheapest policy-passing cost USD; cost above it is "
                         "efficiency leakage / off-policy (#8)")
    pm.add_argument("--ref", dest="ref",
                    help="per-task/per-question attribution id (e.g. an Invarium "
                         "task_id); links this event to the task that produced it")
    pm.add_argument("--json", action="store_true")
    pm.set_defaults(func=cmd_meter)

    pk = sub.add_parser("keys", help="manage ingest API keys")
    pk.add_argument("action",
                    choices=["create", "create-scoped", "list", "revoke",
                             "rotate", "rotate-complete", "rotate-now"])
    pk.add_argument("key_id", nargs="?", help="key id (for revoke/rotate)")
    pk.add_argument("--name", help="label for a new key or rotated key")
    pk.add_argument("--org")
    pk.add_argument("--overlap", type=int, default=300,
                    help="rotation overlap in seconds (default: 300 = 5min)")
    pk.add_argument("--workspace", help="scope the key to a specific workspace (create-scoped)")
    pk.set_defaults(func=cmd_keys)

    pih = sub.add_parser("ingest-health",
                         help="show ingestion health diagnostics per source (#150)")
    pih.add_argument("--org")
    pih.add_argument("--json", action="store_true")
    pih.set_defaults(func=cmd_ingest_health)

    pt = sub.add_parser("topup", help="add prepaid credit")
    pt.add_argument("--org"); pt.add_argument("--amount", type=float, required=True)
    pt.add_argument("--reason")
    pt.set_defaults(func=cmd_topup)

    pr = sub.add_parser("report", help="monthly spend report (PDF/HTML)")
    pr.add_argument("--org"); pr.add_argument("--month", help="YYYY-MM (default: current)")
    pr.add_argument("--out", help="output path (.pdf or .html)")
    pr.set_defaults(func=cmd_report)

    prc = sub.add_parser(
        "reconcile",
        help="true-up estimated cost to a provider's authoritative billing")
    prc.add_argument("--org")
    prc.add_argument("--period", help="YYYY-MM billing period (windows usage by ts)")
    prc.add_argument("--totals", help="JSON/CSV of provider->USD from the provider export")
    prc.add_argument("--provider", help="reconcile a single provider inline")
    prc.add_argument("--amount", type=float, help="that provider's authoritative billed USD")
    prc.add_argument("--apply", action="store_true",
                     help="write adjust entries (default: dry run)")
    prc.add_argument("--json", action="store_true")
    prc.set_defaults(func=cmd_reconcile)

    pv = sub.add_parser(
        "verify",
        help="verify the usage-event tamper-evidence hash chain (#108)")
    pv.add_argument("--org", help="verify a single org (default: all)")
    pv.add_argument("--hmac-key", dest="hmac_key", default=None,
                    help="keyed-MAC secret (default: config/LEDGER_CHAIN_HMAC_KEY; "
                         "pass empty string to force plain SHA-256)")
    pv.add_argument("--json", action="store_true")
    pv.set_defaults(func=cmd_verify)

    prw = sub.add_parser(
        "reconcile-webhooks",
        help="detect + replay Stripe webhook events missed in deploy windows (#177)")
    prw.add_argument("--days", type=float, default=7.0,
                     help="lookback window in days (default: 7)")
    prw.add_argument("--apply", action="store_true",
                     help="replay missing events via the app handler (default: dry-run)")
    prw.add_argument("--types", default=None,
                     help="comma-separated event types (default: all handled types)")
    prw.add_argument("--json", action="store_true")
    prw.set_defaults(func=cmd_reconcile_webhooks)

    pbt = sub.add_parser(
        "backtest",
        help="replay estimated usage events against current billing logic (#145)")
    pbt.add_argument("--org", help="organization id, slug, or name")
    pbt.add_argument("--tolerance-micros", type=int, default=0,
                     help="allowed absolute per-event delta in micro-dollars")
    pbt.add_argument("--json", action="store_true")
    pbt.set_defaults(func=cmd_backtest)

    pck = sub.add_parser(
        "checkpoint",
        help="escrow a signed tamper-evidence checkpoint to retain out-of-band (#120)")
    pck.add_argument("--org", help="checkpoint a single org (default: all)")
    pck.add_argument("--hmac-key", dest="hmac_key", default=None,
                     help="keyed-MAC secret (default: config/LEDGER_CHAIN_HMAC_KEY; "
                          "pass empty string to force plain SHA-256)")
    pck.add_argument("--deliver", action="store_true",
                     help="also email each anchor as an out-of-band receipt via "
                          "the alerts SMTP config (#121); dry-run when SMTP is "
                          "unconfigured")
    pck.add_argument("--json", action="store_true")
    pck.set_defaults(func=cmd_checkpoint)

    pvc = sub.add_parser(
        "verify-checkpoints",
        help="replay customer-retained checkpoints against the live chain (#120)")
    pvc.add_argument("--org", help="verify a single org (default: all)")
    pvc.add_argument("--file", help="path to retained checkpoint JSON (one object, "
                                    "a JSON array, or one JSON object per line); "
                                    "'-' reads stdin. Omit to use DB-stored anchors "
                                    "(weaker — supply --file for the strong guarantee)")
    pvc.add_argument("--hmac-key", dest="hmac_key", default=None,
                     help="keyed-MAC secret (default: config/LEDGER_CHAIN_HMAC_KEY)")
    pvc.add_argument("--json", action="store_true")
    pvc.set_defaults(func=cmd_verify_checkpoints)

    pcl = sub.add_parser(
        "close",
        help="fetch provider authoritative totals and reconcile (cron close, #109)")
    pcl.add_argument("--org")
    pcl.add_argument("--period", help="YYYY-MM (default: previous month, for a "
                                      "cron run just after month end)")
    pcl.add_argument("--providers", help="comma-separated provider list to fetch "
                                         "(default: providers with recorded usage)")
    pcl.add_argument("--apply", action="store_true",
                     help="write adjust entries (default: dry run)")
    pcl.add_argument("--no-checkpoint", dest="checkpoint", action="store_false",
                     help="skip the automatic tamper-evidence checkpoint an "
                          "applied close records (#121)")
    pcl.add_argument("--deliver", action="store_true",
                     help="email the close's checkpoint as an out-of-band "
                          "receipt via the alerts SMTP config (#121)")
    pcl.add_argument("--json", action="store_true")
    pcl.set_defaults(func=cmd_close)

    psv = sub.add_parser(
        "savings",
        help="show verified savings + the savings-share due for a period (#7)")
    psv.add_argument("--org")
    psv.add_argument("--period", help="YYYY-MM (default: previous month)")
    psv.add_argument("--window", choices=["today", "24h", "7d", "mtd", "billing"],
                     help="live operational window; overrides --period")
    psv.add_argument("--rate", type=float,
                     help="savings-share percent (default: billing.savings_share_pct or 10)")
    psv.add_argument("--json", action="store_true")
    psv.set_defaults(func=cmd_savings)

    pef = sub.add_parser(
        "efficiency",
        help="value-vs-actual efficiency: flagship-equivalent value, cost, multiple (#8)")
    pef.add_argument("--org")
    pef.add_argument("--period", help="YYYY-MM (default: previous month)")
    pef.add_argument("--all", action="store_true", help="all-time, ignore period")
    pef.add_argument("--actual", type=float,
                     help="reconciled actual paid USD for the period (console truth)")
    pef.add_argument("--json", action="store_true")
    pef.set_defaults(func=cmd_efficiency)

    pbs = sub.add_parser(
        "bill-savings",
        help="raise a savings-share invoice for a period (dry run without --apply)")
    pbs.add_argument("--org")
    pbs.add_argument("--period", help="YYYY-MM (default: previous month)")
    pbs.add_argument("--rate", type=float,
                     help="savings-share percent (default: billing.savings_share_pct or 10)")
    pbs.add_argument("--apply", action="store_true",
                     help="record the invoice + raise it in Stripe (default: dry run)")
    pbs.add_argument("--force", action="store_true",
                     help="bill even on a tier where savings-share is waived (Pro) "
                          "or a tip (Free)")
    pbs.add_argument("--json", action="store_true")
    pbs.set_defaults(func=cmd_bill_savings)

    pa = sub.add_parser("alerts", help="deliver pending alerts")
    pa.add_argument("--org")
    pa.add_argument("--test", action="store_true", help="(reserved) force-check")
    pa.set_defaults(func=cmd_alerts)

    ph = sub.add_parser("install-claude-hook",
                        help="wire Ledger into Claude Code / Codex (Stop hook)")
    ph.add_argument("--path", help="settings.json path (default ~/.claude/settings.json)")
    ph.add_argument("--print", action="store_true", help="print the snippet, don't write")
    ph.set_defaults(func=cmd_install_hook)

    pss = sub.add_parser("stripe-setup",
                         help="create the $20/mo Pro price in your Stripe account")
    pss.set_defaults(func=cmd_stripe_setup)

    sub.add_parser("monitor", help="print live provider runway (bridge)").set_defaults(func=cmd_monitor)
    sub.add_parser("pricing", help="show plan tiers").set_defaults(func=cmd_pricing)
    sub.add_parser("version", help="print version").set_defaults(func=cmd_version)
    return p


def _force_utf8():
    # Windows consoles default to cp1252 and crash on ◆/✓/em-dash when output is
    # piped. Make stdout/stderr UTF-8 (replace on failure) so Ledger prints the
    # same everywhere.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None):
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    # Wire --db through to LEDGER_DB env var so all db.connect() calls honor it
    if hasattr(args, 'db') and args.db:
        os.environ['LEDGER_DB'] = args.db
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
