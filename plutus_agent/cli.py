"""``plutus`` command-line interface.

Subcommands:

    plutus init                 create ~/.plutus/{config.yaml,plutus.db}
    plutus serve [--demo]       run the dashboard + API at :8420
    plutus demo                 serve with realistic sample data (no setup)
    plutus status               orgs, balances, Stripe mode
    plutus org create|list      manage organizations
    plutus workspace create|list  manage workspaces
    plutus meter ...            record a usage event (deplete credit)
    plutus topup ...            add prepaid credit
    plutus report ...           monthly PDF/HTML spend report
    plutus reconcile ...        true-up estimated cost to a provider's real billing
    plutus alerts [--test]      deliver pending low-balance/budget alerts
    plutus monitor              print live provider runway (monitor bridge)

Everything except Stripe Checkout and email delivery works fully offline.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import (__version__, __tagline__, config as cfgmod, db, metering,
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
        sys.exit(f"plutus: no organization '{ident}'")
    orgs = db.list_orgs(conn)
    if len(orgs) == 1:
        return orgs[0]
    if not orgs:
        sys.exit("plutus: no organizations. Run `plutus init` or `plutus org create NAME`.")
    sys.exit("plutus: multiple orgs — pass --org <id|slug|name>.")


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
    print(f"\n  Next: plutus serve   →   http://localhost:8420")
    print(f"        plutus demo    →   explore with sample data\n")


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
    print(f"\n  ◆ Plutus v{__version__} — {__tagline__}")
    print(f"    config:   {cfgmod.config_path()}")
    print(f"    database: {cfgmod.db_path()}")
    print(f"    stripe:   {st['mode']}\n")
    orgs = db.list_orgs(conn)
    if not orgs:
        print("    (no organizations — run `plutus init`)\n")
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
            sys.exit('plutus: `plutus org create` needs a NAME, '
                     'e.g. `plutus org create "Acme Inc"`')
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
            sys.exit('plutus: `plutus workspace create` needs a NAME, '
                     'e.g. `plutus workspace create prod`')
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
        optimal_cost_usd=getattr(args, "optimal", None), source="cli",
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
    elif args.action == "list":
        keys = db.list_api_keys(conn, org["id"])
        if not keys:
            print("  (no API keys)")
        for k in keys:
            used = "never" if not k["last_used_at"] else f"{int((time.time()-k['last_used_at'])/86400)}d ago"
            print(f"  {k['id']}  {k['prefix']+'…':<22} {(k['name'] or '-'):<18} used {used}")
    elif args.action == "revoke":
        if not args.key_id:
            sys.exit("plutus: pass the key id to revoke, e.g. `plutus keys revoke key_…`")
        if db.revoke_api_key(conn, args.key_id, org["id"]):
            _ok(f"revoked {args.key_id}")
        else:
            print(f"  no active key '{args.key_id}' for this org")
    conn.close()


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
    out = args.out or f"plutus-{org['slug']}-{year}-{month:02d}.pdf"
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


HOOK_MODULE = "plutus_agent.integrations.claude_code_hook"


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
            sys.exit(f"plutus: could not parse {path}: {e}")
        # Back up the *pristine* original once — copying the raw bytes so comments
        # and formatting survive. Re-running must not clobber that first backup
        # with an already-modified file, so we only write it if none exists.
        backup = path.with_suffix(path.suffix + ".plutus-bak")
        if not backup.exists():
            import shutil
            shutil.copy2(path, backup)
    settings, changed = _merge_stop_hook(settings, command)
    if not changed:
        _ok(f"Claude Code hook already installed in {path}")
        return
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    _ok(f"installed Plutus Stop hook → {path}")
    _ok("every Claude Code turn now meters into Plutus (org 'Claude Code', "
        "workspace = project name)")
    print(f"\n  Try it:  run a Claude Code turn, then  plutus serve  → http://localhost:8420")
    print(f"  Set PLUTUS_ORG to attribute turns to a specific org.\n")


def cmd_stripe_setup(args):
    cfg = cfgmod.load()
    key = cfg.get("billing", {}).get("stripe_secret_key") or ""
    if not key:
        sys.exit("plutus: no Stripe key. Set STRIPE_SECRET_KEY (use a sk_test_… key first).")
    try:
        import stripe
    except ImportError:
        sys.exit("plutus: Stripe SDK not installed. Run `pip install 'plutus-agent[stripe]'`.")
    stripe.api_key = key
    mode = "TEST" if key.startswith("sk_test_") else "LIVE"
    print(f"  Stripe {mode} mode — setting up the Pro plan…")

    lookup = "plutus_pro_monthly"
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
            name="Plutus Pro",
            description="Plutus Pro — unlimited tracking, prepaid credits, alerts, reports.",
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
    print("    1. plutus serve                       # dashboard with Checkout enabled")
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
        sys.exit("plutus: supply --totals FILE (provider->USD from the provider "
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


def cmd_close(args):
    """#109: fetch provider authoritative totals and reconcile — the cron close."""
    conn = _conn()
    org = _resolve_org(conn, args.org)
    period = args.period or reconcile.previous_month_label()
    providers = None
    if args.providers:
        providers = [p.strip().lower() for p in args.providers.split(",") if p.strip()]
    out = reconcile.close_period(conn, org["id"], period,
                                 providers=providers, apply=args.apply)
    conn.close()

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
    _ok("adjust entries written" if args.apply else "no changes written (dry run)")
    if out["fetch_errors"]:
        sys.exit(1)


def _savings_rate_bps(cfg, args) -> int:
    """Resolve the savings-share rate: --rate (percent) overrides config."""
    if getattr(args, "rate", None) is not None:
        pct = float(args.rate)
        if not (0.0 <= pct <= 100.0):
            sys.exit("plutus: --rate must be a percentage between 0 and 100")
        return int(round(pct * 100))  # percent -> basis points
    return savings_mod.rate_bps_from_config(cfg)


def _print_savings_report(d, org_name, mode):
    print(f"\n  savings-share {d['period']} for '{org_name}' — {mode}\n")
    cov = "" if d["coverage_pct"] is None else f" ({d['coverage_pct']:.0f}% coverage)"
    print(f"    events with a baseline : {d['covered_events']}/{d['total_events']}{cov}")
    print(f"    billable (cost > $0)   : {d.get('billable_events', d['covered_events'])}")
    print(f"    baseline cost          : ${d['baseline_usd']:,.4f}")
    print(f"    actual cost (covered)  : ${d['cost_on_covered_usd']:,.4f}")
    print(f"    verified savings       : ${d['gross_savings_usd']:,.4f}")
    print(f"    share rate             : {d['rate_pct']:.1f}%")
    print(f"    ── billable share      : ${d['billable_share_usd']:,.4f}")
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
    rate_bps = _savings_rate_bps(cfg, args)
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
        sys.exit(f"plutus: won't bill savings-share on a '{org['tier']}' org — {why}. "
                 f"Mandatory billing is a Team feature. Pass --force to override.")
    period = args.period or savings_mod.previous_month_label()
    rate_bps = _savings_rate_bps(cfg, args)
    stripe_client = None
    if args.apply:
        from .billing import StripeClient
        stripe_client = StripeClient(cfg)
    out = savings_mod.bill_savings_share(
        conn, org["id"], period, rate_bps=rate_bps,
        stripe_client=stripe_client, apply=args.apply,
        min_charge_usd=cfg.get("billing", {}).get("savings_min_charge_usd", 0.50),
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
    print(f"plutus v{__version__} — {__tagline__}")


def cmd_pricing(args):
    print(f"\n  Plutus plans — {__tagline__}\n")
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
        prog="plutus", description=f"Plutus — {__tagline__}")
    p.add_argument("--version", action="version", version=f"plutus v{__version__}")
    p.add_argument("--db", help="database path (overrides PLUTUS_DB)")
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
    pm.add_argument("--json", action="store_true")
    pm.set_defaults(func=cmd_meter)

    pk = sub.add_parser("keys", help="manage ingest API keys")
    pk.add_argument("action", choices=["create", "list", "revoke"])
    pk.add_argument("key_id", nargs="?", help="key id (for revoke)")
    pk.add_argument("--name", help="label for a new key")
    pk.add_argument("--org")
    pk.set_defaults(func=cmd_keys)

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
                    help="keyed-MAC secret (default: config/PLUTUS_CHAIN_HMAC_KEY; "
                         "pass empty string to force plain SHA-256)")
    pv.add_argument("--json", action="store_true")
    pv.set_defaults(func=cmd_verify)

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
    pcl.add_argument("--json", action="store_true")
    pcl.set_defaults(func=cmd_close)

    psv = sub.add_parser(
        "savings",
        help="show verified savings + the savings-share due for a period (#7)")
    psv.add_argument("--org")
    psv.add_argument("--period", help="YYYY-MM (default: previous month)")
    psv.add_argument("--rate", type=float,
                     help="savings-share percent (default: billing.savings_share_pct or 18)")
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
                     help="savings-share percent (default: billing.savings_share_pct or 18)")
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
                        help="wire Plutus into Claude Code / Codex (Stop hook)")
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
    # piped. Make stdout/stderr UTF-8 (replace on failure) so Plutus prints the
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
    # Wire --db through to PLUTUS_DB env var so all db.connect() calls honor it
    if hasattr(args, 'db') and args.db:
        os.environ['PLUTUS_DB'] = args.db
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
