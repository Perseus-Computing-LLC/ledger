"""Dashboard rendering — dark theme anchored on perseus.observer ``#0c0814``.

Pure functions: ``(summary dict) -> HTML string``. No framework, no external
assets (CSP-safe, works offline). A tiny inline poller re-fetches
``/api/summary`` every few seconds and live-updates the headline numbers; the
page also degrades to a periodic full reload if JS is disabled.
"""
from __future__ import annotations

import datetime as _dt
import html

# Brand — #0c0814 base + Perseus deck accents (amber numbers, green positive,
# coral problem), JetBrains-Mono-style numerals.
CSS = """
:root{
  --bg:#0a1018; --surface:#101a27; --surface-raised:#142133; --surface-soft:#0e1723;
  --panel:var(--surface); --panel2:var(--surface-raised); --bg2:var(--surface-soft);
  --line:#223248; --line-strong:#334861; --line2:var(--line-strong); --txt:#f3f7fb; --dim:#aab8c8; --faint:#738399;
  --blue:#69a9ff; --blue-ink:#071525; --green:#54d39a; --green-soft:#123b32;
  --coral:#ff7a6a; --coral-soft:#44241f; --coral-dim:#674139; --amber:#ffbf5f; --amber-soft:#3d301a; --amber-dim:#5d4c27; --green-dim:#28644f;
  --mono:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;
}
*{box-sizing:border-box} html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased;min-height:100vh} a{color:var(--blue);text-decoration:none} a:hover{text-decoration:underline}
.wrap{max-width:1320px;margin:0 auto;padding:24px 28px 56px}.dashboard{display:grid;gap:16px}
.top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;padding-bottom:18px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px}.logo{font-size:20px;color:var(--blue)}.brand h1{font-size:18px;margin:0;font-weight:720;letter-spacing:-.02em}.brand .tag{color:var(--dim);font-size:12px;margin-top:1px}
.pill{font-size:11px;padding:4px 8px;border-radius:999px;border:1px solid var(--line-strong);color:var(--dim);white-space:nowrap}.pill.pro{color:#cce0ff;border-color:#355984;background:#142943}.pill.live{color:#8ce2ba;border-color:#28644f;background:#102b25}.pill.demo{color:#ffc0b8;border-color:#6c4139;background:#30201f}
.orgsel,.amt{background:var(--surface);color:var(--txt);border:1px solid var(--line-strong);border-radius:8px;padding:8px 10px;font:inherit}.amt{font-family:var(--mono)}
.banner{margin:0;border-radius:10px;border:1px solid #674139;background:var(--coral-soft);padding:11px 14px;color:#ffd8d2;font-size:13px}.banner .x{color:var(--coral);font-weight:800}
.upsell{border-radius:10px;border:1px solid #5d4c27;background:var(--amber-soft);padding:14px 16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}.upsell .u-txt{flex:1;min-width:240px}.upsell .u-h{font-weight:700;color:#ffda97}.upsell .u-s{color:#d6c69f;font-size:12.5px;margin-top:2px}.upsell form{display:flex;gap:8px;align-items:center;margin:0}
.hero-metric{display:grid;grid-template-columns:minmax(0,1fr) minmax(230px,.42fr);gap:26px;align-items:end;background:var(--surface-raised);border:1px solid var(--line-strong);border-radius:12px;padding:24px}.hero-metric .hero-label{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);font-weight:750}.hero-metric .hero-value{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:42px;line-height:1.04;letter-spacing:-.06em;font-weight:720;margin:7px 0;color:#f8fbff}.hero-metric .hero-sub{color:var(--dim);font-size:13px}.hero-aside{border-left:1px solid var(--line);padding-left:24px;color:var(--dim)}
.grid{display:grid;gap:16px}.stat-grid{grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:15px;min-height:126px}.card .l{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--faint);font-weight:750}.card .v{font-size:24px;font-weight:760;font-family:var(--mono);font-variant-numeric:tabular-nums;margin-top:8px;letter-spacing:-.04em}.card .v.amber{color:var(--amber)}.card .v.green{color:var(--green)}.card .v.coral{color:var(--coral)}.card .s{font-size:12px;color:var(--dim);margin-top:5px;line-height:1.4}.compact-status .s{max-width:18ch}.cols{grid-template-columns:1fr 1fr}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden}.section-title{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);margin:0;padding:15px 18px 11px;display:flex;justify-content:space-between;align-items:center;font-weight:750}.section-title .hint{color:var(--faint);font-weight:500;text-transform:none;letter-spacing:0}
table{width:100%;border-collapse:collapse}th,td{padding:11px 18px;text-align:right;border-top:1px solid var(--line)}th{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--faint);font-weight:750}th:first-child,td:first-child{text-align:left}.num{font-family:var(--mono);font-variant-numeric:tabular-nums}.name{font-weight:650;color:var(--txt)}.bar{height:4px;border-radius:99px;background:var(--line-strong);overflow:hidden;margin-top:7px}.bar>i{display:block;height:100%;background:var(--blue)}.bar.warn>i{background:var(--coral)}.bar.ok>i{background:var(--green)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;vertical-align:middle}.dot.healthy{background:var(--green)}.dot.idle{background:var(--amber)}.dot.stale{background:var(--faint)}.muted{color:var(--dim)}.empty{color:var(--faint);padding:18px;text-align:center}.feed{max-height:none}.feed .row{display:flex;justify-content:space-between;gap:12px;padding:11px 18px;border-top:1px solid var(--line);font-size:13px}.feed .row:first-child{border-top:none}.feed .meta{color:var(--dim);font-size:12px}.tag2{font-size:10px;padding:2px 6px;border-radius:4px;background:var(--surface-soft);border:1px solid var(--line);color:var(--dim);margin-left:6px}
.billing{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:14px 18px}.btn{border-radius:8px;padding:9px 13px;font-weight:720;font-size:13px;cursor:pointer;line-height:1.2}.btn-primary{background:var(--blue);color:var(--blue-ink);border:1px solid var(--blue)}.btn.ghost{background:transparent;color:var(--txt);border:1px solid var(--line-strong)}.btn.danger{color:#ffaaa1;border-color:#704139}.btn:disabled{opacity:.45;cursor:not-allowed}.foot{margin-top:10px;color:var(--faint);font-size:12px;text-align:center}.spark{display:flex;gap:2px;align-items:flex-end;height:26px}.spark i{flex:1;background:var(--blue);border-radius:1px;min-height:2px}
@media(max-width:1080px){.stat-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:760px){.wrap{padding:18px 16px 42px}.hero-metric,.cols{grid-template-columns:1fr}.hero-aside{border-left:none;border-top:1px solid var(--line);padding:15px 0 0}.stat-grid{grid-template-columns:repeat(2,minmax(0,1fr))}th,td{padding:10px 12px}.top{align-items:flex-start}}
@media print{html,body{background:var(--bg)!important;color:var(--txt)!important;-webkit-print-color-adjust:exact;print-color-adjust:exact}.wrap{max-width:none;padding:16px}.panel,.card,.hero-metric{break-inside:avoid}}
"""

POLLER = """
async function poll(){
  try{
    const u=new URL(location.href); const org=u.searchParams.get('org')||'';
    const r=await fetch('/api/summary'+(org?('?org='+encodeURIComponent(org)):''));
    if(!r.ok)return; const d=await r.json();
    const set=(id,v)=>{const e=document.getElementById(id); if(e)e.textContent=v;};
    const usd=v=>'$'+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
    set('v-balance',usd(d.balance));
    set('v-today',usd(d.windows.today.cost));
    set('v-mtd',usd(d.windows.mtd.cost));
    set('v-events',Number(d.windows.mtd.events).toLocaleString());
    document.getElementById('pulse')?.classList.remove('off');
    setTimeout(()=>document.getElementById('pulse')?.classList.add('off'),600);
  }catch(e){}
}
setInterval(poll,5000);
"""


FAVICON = ("<link rel='icon' href=\"data:image/svg+xml,"
           "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
           "%3Crect width='32' height='32' rx='6' fill='%230c0814'/%3E"
           "%3Ctext x='16' y='23' font-size='20' text-anchor='middle' fill='%23f5b63f'%3E"
           "%E2%97%86%3C/text%3E%3C/svg%3E\">")


def _usd(v):
    return "—" if v is None else f"${v:,.2f}"


def _ago(ts):
    if not ts:
        return "—"
    import time
    s = max(0, time.time() - ts)
    if s < 60:
        return f"{int(s)}s ago"
    if s < 3600:
        return f"{int(s/60)}m ago"
    if s < 86400:
        return f"{int(s/3600)}h ago"
    return f"{int(s/86400)}d ago"


def _e(s):
    return html.escape(str(s))


def render_dashboard(summary: dict, *, orgs: list, cfg: dict,
                     stripe_status: dict, demo: bool = False,
                     runway: dict | None = None, user=None,
                     api_keys: list | None = None, csrf: str = "",
                     integrity: dict | None = None,
                     checkpoint: dict | None = None) -> str:
    # Fix #58: hidden CSRF field embedded in every state-changing form.
    csrf_field = (f"<input type='hidden' name='_csrf' value='{_e(csrf)}'>"
                  if csrf else "")
    org = summary["org"]
    tier = summary["tier"]
    w = summary["windows"]
    # The dashboard tracks spend and savings, not a prepaid-credit balance.
    # Suppress legacy balance alerts from this surface.
    banner = ""
    visible_alerts = [a for a in summary["alerts"]
                      if "credit balance" not in str(a.get("message", "")).lower()]
    if visible_alerts:
        items = "".join(f"<div><span class='x'>▲</span> {_e(a['message'])}</div>"
                        for a in visible_alerts[:3])
        banner = f"<div class='banner'><div>{items}</div></div>"

    # free-tier upgrade nudge — the conversion lever
    upsell = ""
    ts = summary.get("tier_status") or {}
    can_pro = stripe_status["available"] and stripe_status["has_pro_price"]
    if ts.get("is_free") and (ts.get("over_limit") or ts.get("near_limit")
                              or ts.get("workspaces_over")):
        if ts.get("over_limit"):
            head = "You've reached your Free plan limit"
            sub = (f"{ts['tracked_tokens']:,} / {ts['tracked_limit']:,} tracked tokens "
                   "this month. Upgrade to Pro for unlimited tracking, prepaid credits, "
                   "and alerts.")
        elif ts.get("near_limit"):
            head = f"You're at {ts['tracked_pct']:.0f}% of your Free plan"
            sub = (f"{ts['tracked_tokens']:,} / {ts['tracked_limit']:,} tracked tokens used "
                   "this month. Pro removes the cap — $20/mo.")
        else:
            head = "You're at your Free plan's workspace limit"
            sub = (f"{ts['workspaces_used']} of {ts['workspaces_limit']} workspace used. "
                   "Pro includes up to 10 workspaces — $20/mo.")
        if can_pro:
            cta = (f"<form method='post' action='/billing/checkout/pro'>"
                   f"<input type='hidden' name='org' value='{_e(org['id'])}'>{csrf_field}"
                   f"<button class='btn btn-primary' type='submit'>Upgrade to Pro →</button></form>"
                   f"<a class='btn ghost' href='/pricing'>Compare plans</a>")
        else:
            cta = "<a class='btn btn-primary' href='/pricing'>See plans →</a>"
        upsell = (f"<div class='upsell'><div class='u-txt'>"
                  f"<div class='u-h'>{_e(head)}</div><div class='u-s'>{_e(sub)}</div></div>"
                  f"{cta}</div>")

    # org selector
    opts = "".join(
        f"<option value='{_e(o['id'])}' {'selected' if o['id']==org['id'] else ''}>{_e(o['name'])}</option>"
        for o in orgs
    )
    orgsel = (f"<select class='orgsel' onchange=\"location.href='/?org='+this.value\">{opts}</select>"
              if len(orgs) > 1 else "")

    # signed-in chip (only when auth is on and a user is bound to the request)
    userchip = ""
    if user is not None:
        ident = (user["name"] or user["email"]) if hasattr(user, "keys") else str(user)
        userchip = (
            "<span style='font-size:12px;color:var(--dim);display:flex;gap:6px;align-items:center'>"
            f"{_e(ident)} · "
            "<form method='post' action='/auth/logout' style='display:inline;margin:0'>"
            f"{csrf_field}"
            "<button type='submit' style='background:none;border:none;color:var(--dim);cursor:pointer;padding:0;font:inherit'>Sign out</button>"
            "</form></span>")

    # tracked-tokens meter (free tier limit)
    tracked, limit = summary["tracked_tokens_mtd"], summary["tracked_limit"]
    if limit:
        pct = min(100.0, summary["tracked_pct"] or 0)
        cls = "warn" if pct >= 90 else ("ok" if pct < 70 else "")
        meter = (f"<div class='card'><div class='l'>Tracked tokens · this month</div>"
                 f"<div class='v {'coral' if pct>=90 else 'amber'}'>{tracked:,}</div>"
                 f"<div class='s'>of {limit:,} ({pct:.0f}%) — {tier['name']} plan</div>"
                 f"<div class='bar {cls}'><i style='width:{pct:.0f}%'></i></div></div>")
    else:
        meter = (f"<div class='card'><div class='l'>Tracked tokens · this month</div>"
                 f"<div class='v amber'>{tracked:,}</div>"
                 f"<div class='s'>unlimited — {tier['name']} plan</div></div>")

    # Ledger integrity tile (#108): tamper-evidence status for this org's chain.
    integrity_card = ""
    if integrity is not None:
        o = next((x for x in integrity.get("orgs", []) if x["org_id"] == org["id"]),
                 None)
        keyed = bool((cfg.get("ledger") or {}).get("hmac_key"))
        mode = "keyed HMAC" if keyed else "SHA-256"
        if o is None or o["status"] == "empty":
            integrity_card = (
                "<div class='card'><div class='l'>Ledger integrity</div>"
                "<div class='v'>—</div>"
                "<div class='s'>no metered events yet</div></div>")
        elif o["status"] == "ok":
            pre = (f" · {o['pre_chain']:,} pre-chain" if o["pre_chain"] else "")
            integrity_card = (
                "<div class='card'><div class='l'>Ledger integrity</div>"
                f"<div class='v green'>✓ verified</div>"
                f"<div class='s'>{o['verified']:,} events chained ({mode}){pre}</div></div>")
        else:
            integrity_card = (
                "<div class='card'><div class='l'>Ledger integrity</div>"
                "<div class='v coral'>✗ tampered</div>"
                "<div class='s'>hash chain broken — run <span class='num'>plutus verify</span></div></div>")

    # Checkpoint anchor tile (#121): the latest externally-retainable anchor.
    # Shown alongside the integrity tile — integrity says "the chain
    # self-verifies today"; the anchor says "and here is the point a customer
    # can independently hold us to".
    checkpoint_card = ""
    if integrity is not None:
        if checkpoint:
            import time as _time
            when = _time.strftime("%Y-%m-%d %H:%M UTC",
                                  _time.gmtime(float(checkpoint["ts"])))
            keyed_cp = checkpoint.get("mode") == "hmac-sha256"
            checkpoint_card = (
                "<div class='card'><div class='l'>Checkpoint anchor</div>"
                f"<div class='v green'>⚓ {_e(when)}</div>"
                f"<div class='s'>rowid {int(checkpoint['through_rowid']):,} · "
                f"{int(checkpoint['event_count']):,} events · "
                f"{'signed' if keyed_cp else 'unsigned'} — verify with "
                "<span class='num'>plutus verify-checkpoints --file</span> "
                "against your retained copy "
                "(<a href='/v1/checkpoints'>download</a>)</div></div>")
        else:
            checkpoint_card = (
                "<div class='card compact-status'><div class='l'>Checkpoint anchor</div>"
                "<div class='v'>—</div>"
                "<div class='s'>No external checkpoint retained yet</div></div>")

    cards = f"""
    <div class="grid stat-grid">
      <div class="card"><div class="l">Spend today</div>
        <div class="v amber" id="v-today">{_usd(w['today']['cost'])}</div>
        <div class="s">{w['today']['events']:,} calls</div></div>
      <div class="card"><div class="l">Month to date</div>
        <div class="v" id="v-mtd">{_usd(w['mtd']['cost'])}</div>
        <div class="s"><span id="v-events">{w['mtd']['events']:,}</span> calls · 7d {_usd(w['7d']['cost'])}</div></div>
      {meter}
      {integrity_card}
      {checkpoint_card}
    </div>"""

    # workspaces with budget bars
    ws_rows = []
    ws_spend = {x["key"]: x for x in summary["by_workspace"]}
    for ws in summary["workspaces"]:
        sp = ws_spend.get(ws["name"], {"cost": 0, "events": 0, "tokens": 0})
        cap = ws["monthly_budget_usd"]
        if cap:
            pct = min(100.0, sp["cost"] / cap * 100.0) if cap else 0
            cls = "warn" if pct >= 80 else "ok"
            budget = (f"<div class='bar {cls}'><i style='width:{pct:.0f}%'></i></div>"
                      f"<div class='muted' style='font-size:11px;margin-top:3px'>{_usd(sp['cost'])} / {_usd(cap)} ({pct:.0f}%)</div>")
        else:
            budget = "<div class='muted' style='font-size:11px;margin-top:3px'>no cap</div>"
        ws_rows.append(
            f"<tr><td class='name'>{_e(ws['name'])}{budget}</td>"
            f"<td class='num'>{_usd(sp['cost'])}</td>"
            f"<td class='num'>{sp['tokens']:,}</td>"
            f"<td class='num'>{sp['events']:,}</td></tr>")
    ws_table = ("".join(ws_rows) or "<tr><td colspan=4 class='empty'>No workspaces yet.</td></tr>")

    # providers + health
    prov_health = {p["provider"]: p for p in summary["provider_health"]}
    maxp = max([p["cost"] for p in summary["by_provider"]] + [1e-9])
    prov_rows = []
    for p in summary["by_provider"]:
        h = prov_health.get(p["key"], {"status": "stale", "burn_per_day": 0, "last_ts": None})
        barw = p["cost"] / maxp * 100.0
        prov_rows.append(
            f"<tr><td class='name'><span class='dot {h['status']}'></span>{_e(p['key'])}"
            f"<div class='bar'><i style='width:{barw:.0f}%'></i></div></td>"
            f"<td class='num'>{_usd(p['cost'])}</td>"
            f"<td class='num'>{_usd(h['burn_per_day'])}</td>"
            f"<td class='num muted'>{_ago(h['last_ts'])}</td></tr>")
    prov_table = ("".join(prov_rows) or "<tr><td colspan=4 class='empty'>No usage yet.</td></tr>")

    # cost per task type
    task_rows = []
    for tt in summary["by_task_type"]:
        task_rows.append(
            f"<tr><td class='name'>{_e(tt['key'])}</td>"
            f"<td class='num'>{_usd(tt['cost'])}</td>"
            f"<td class='num'>{tt['events']:,}</td>"
            f"<td class='num amber'>{_usd(tt.get('cost_per_event',0))}</td></tr>")
    task_table = ("".join(task_rows) or "<tr><td colspan=4 class='empty'>No tasks yet.</td></tr>")

    # recent feed
    feed = []
    for ev in summary["recent_events"]:
        est = "<span class='tag2'>est</span>" if ev.get("estimated") else ""
        feed.append(
            f"<div class='row'><div><span class='name'>{_e(ev['provider'])}</span>"
            f"<span class='tag2'>{_e(ev.get('task_type','-'))}</span>"
            f"<div class='meta'>{_e(ev.get('workspace_name') or '—')} · {_e(ev.get('model') or '-')}</div></div>"
            f"<div style='text-align:right'><span class='num amber'>{_usd(ev['cost_usd'])}</span>{est}"
            f"<div class='meta'>{_ago(ev['ts'])}</div></div></div>")
    feed_html = ("".join(feed) or "<div class='empty'>No calls metered yet.</div>")

    # Free is intentionally a no-card experience for teams of up to ten. Stripe
    # is only a voluntary thank-you path after Plutus can show verified savings.
    can_checkout = stripe_status["available"]
    sb = stripe_status["mode"]
    if can_checkout:
        billing = """
        <div class="billing">
          <span class="muted">Free covers teams of up to 10 people. When Plutus can verify savings,
          it will show an optional Stripe thank-you — never an automatic charge.</span>
        </div>"""
    else:
        billing = f"""
        <div class="billing">
          <span class="muted">Free covers teams of up to 10 people. Stripe is {_e(sb)}; the optional
          savings thank-you link will appear after Stripe is connected and savings are verified.</span>
        </div>"""

    # Efficiency billboard — the headline stat, on every tier.
    # ATTRIBUTION: Plutus MEASURES; it does not save. Perseus (routing) + Vault
    # (memory) are what reduce spend. When the ecosystem has tagged events with a
    # baseline we can attribute *provable* savings ("Perseus saved you $X —
    # verified by Plutus"). Standalone (no baseline) we show spend + a flagship-
    # equivalent efficiency ratio as a tracking/verification stat — no "saved" claim.
    from .. import pricing as _pricing
    tobj = _pricing.tier(tier["key"] if isinstance(tier, dict) else tier)
    eff = summary.get("efficiency") or {}
    share = summary.get("savings_share") or {}
    billboard = ""
    if eff.get("events"):
        val = eff.get("flagship_value_usd") or 0.0
        basis = eff.get("basis_usd") or 0.0
        mult = eff.get("multiple")
        mult_s = (f"{mult:g}×" if mult else "—")
        covered = int(share.get("covered_events") or 0)
        gross = share.get("gross_savings_usd") or 0.0
        has_savings = covered > 0 and gross > 0   # Perseus baseline present

        audit = summary.get("audit") or {}
        recommended_donation = float(audit.get("recommended_donation_usd") or 0.0)
        donation_bps = int(audit.get("recommended_donation_bps") or 0)
        tip_html = ""
        if tobj.savings_share == "suggested" and can_checkout and has_savings:
            tip_amt = max(1, int(round(recommended_donation)))
            tip_html = (
                f"<form method='post' action='/billing/checkout/donate' "
                f"style='display:flex;gap:8px;align-items:center;margin-top:12px;flex-wrap:wrap'>"
                f"<input type='hidden' name='org' value='{_e(org['id'])}'>{csrf_field}"
                f"<span class='muted' style='font-size:13px'>"
                f"Optional {donation_bps / 100:.0f}% thank-you: {_usd(recommended_donation)} "
                f"of the {_usd(gross)} verified savings."
                f"</span>"
                f"<input class='amt' type='number' name='amount' value='{tip_amt}' min='1' step='1' style='width:80px'>"
                f"<button class='btn ghost' type='submit'>Chip in 5% →</button></form>")
        elif tobj.savings_share == "waived":
            tip_html = ("<div class='muted' style='font-size:13px;margin-top:10px'>"
                        "You're on Pro — a flat $20/mo, no savings-share.</div>")

        if has_savings:
            label = "Perseus saved you"
            num = _usd(gross)
            sub = f"this month · {_e(mult_s)} efficiency · verified by Plutus"
            aside = (f"<div class='muted' style='font-size:12px'>flagship-equivalent value</div>"
                     f"<div style='font-size:20px;font-weight:600'>{_usd(val)}</div>"
                     f"<div class='muted' style='font-size:12px'>for {_usd(basis)} actual</div>")
        else:
            label = "Your AI spend"
            num = _usd(basis)
            sub = f"this month · flagship-equivalent {_usd(val)} · {_e(mult_s)} efficient"
            aside = ("<div class='muted' style='font-size:12px'>tracking &amp; verification</div>"
                     "<div class='muted' style='font-size:13px;max-width:230px;margin-top:2px'>"
                     "Getting the tokens you pay for? Reconcile metered spend against your "
                     "provider console. Add Perseus to route spend down.</div>")
        billboard = (
            f'<section class="hero-metric">'
            f"<div><div class='hero-label'>{_e(label)}</div>"
            f"<div class='hero-value'>{num}</div>"
            f"<div class='hero-sub'>{sub}</div>{tip_html}</div>"
            f"<div class='hero-aside'>{aside}</div>"
            f"</section>")

    # Free keeps the savings and audit view; deeper task attribution can be
    # introduced later without pressuring a Free team into a checkout flow.
    if tobj.full_reporting:
        task_panel = (
            '<div class="panel"><h2 class="section-title">Cost per task type <span class="hint">ROI lens</span></h2>'
            '<table><thead><tr><th>Task type</th><th>Cost</th><th>Calls</th><th>$/task</th></tr></thead>'
            f'<tbody>{task_table}</tbody></table></div>')
    else:
        task_panel = (
            '<div class="panel"><h2 class="section-title">Cost per task type <span class="hint">coming later</span></h2>'
            '<div class="empty" style="padding:24px 14px;text-align:center">'
            '<div style="font-size:15px;margin-bottom:6px">Savings and audit stay available on Free</div>'
            '<div class="muted" style="font-size:13px">Per-task breakdowns will arrive separately; '
            'they are not required to track verified savings or optionally support Perseus.</div>'
            '</div></div>')

    # optional live runway panel (from the monitor bridge)
    runway_panel = ""
    if runway and runway.get("providers"):
        rr = []
        for p in runway["providers"]:
            bal_s = _usd(p.get("balance")) if p.get("balance") is not None else _usd(p.get("remaining"))
            days = p.get("days_left")
            days_s = "∞" if days is None else f"{days:.0f}d"
            src = "live" if p.get("source") == "live" else "ledger"
            rr.append(f"<tr><td class='name'>{_e(p['provider'])}<span class='tag2'>{src}</span></td>"
                      f"<td class='num green'>{bal_s}</td>"
                      f"<td class='num'>{_usd(p.get('burn_per_day'))}</td>"
                      f"<td class='num'>{days_s}</td></tr>")
        runway_panel = f"""
        <div class="panel">
          <h2 class="section-title">Provider runway <span class="hint">live, via plutus.py monitor</span></h2>
          <table><thead><tr><th>Provider</th><th>Balance</th><th>$/day</th><th>Runway</th></tr></thead>
          <tbody>{''.join(rr)}</tbody></table>
        </div>"""

    # API keys panel — how an org feeds usage into the hosted instance
    base_url = (cfg.get("auth", {}).get("base_url") or "").rstrip("/") or "http://localhost:8420"
    key_rows = []
    for k in (api_keys or []):
        used = _ago(k["last_used_at"]) if k.get("last_used_at") else "never used"
        key_rows.append(
            f"<tr><td class='name'>{_e(k.get('name') or '—')}"
            f"<div class='meta'>{_e(k['prefix'])}…</div></td>"
            f"<td class='num muted'>{_e(used)}</td>"
            f"<td style='text-align:right'><form method='post' action='/keys/revoke' style='margin:0'>"
            f"<input type='hidden' name='org' value='{_e(org['id'])}'>"
            f"<input type='hidden' name='key_id' value='{_e(k['id'])}'>{csrf_field}"
            f"<button class='btn ghost' type='submit'>Revoke</button></form></td></tr>")
    keys_table = ("".join(key_rows)
                  or "<tr><td colspan=3 class='empty'>No API keys yet — create one to start sending usage.</td></tr>")
    curl = (f"curl -X POST {base_url}/v1/usage \\\n"
            f"  -H 'Authorization: Bearer plutus_sk_…' \\\n"
            f"  -d '{{\"provider\":\"anthropic\",\"model\":\"claude-opus-4-8\","
            f"\"input_tokens\":1200,\"output_tokens\":800,\"workspace\":\"prod\"}}'")
    keys_panel = f"""
    <div class="panel" style="margin-top:16px">
      <h2 class="section-title">API keys <span class="hint">send usage to /v1/usage</span></h2>
      <table><thead><tr><th>Name</th><th>Last used</th><th></th></tr></thead>
      <tbody>{keys_table}</tbody></table>
      <form class="billing" method="post" action="/keys/create">
        <input type="hidden" name="org" value="{_e(org['id'])}">{csrf_field}
        <span class="muted">New key:</span>
        <input class="amt" style="width:160px" type="text" name="name" placeholder="e.g. prod agent">
        <button class="btn btn-primary" type="submit">Create key</button>
      </form>
      <pre style="margin:2px 18px 14px;padding:12px 14px;background:var(--bg2);border:1px solid var(--line2);
        border-radius:9px;overflow:auto;font-family:var(--mono);font-size:12px;color:var(--dim)">{_e(curl)}</pre>
    </div>"""

    gen = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    from .. import __version__, __tagline__
    badges = (f"<span class='pill {tier['key']}'>{_e(tier['name'])} plan</span>"
              + ("<span class='pill demo'>DEMO DATA</span>" if demo else "")
              + '<span class="pill live" id="pulse" aria-live="polite">● live</span>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Plutus — {_e(org['name'])} · spend dashboard</title>{FAVICON}
<style>{CSS}
#pulse{{transition:opacity .4s}}#pulse.off{{opacity:.4}}</style>
</head><body><main class="dashboard wrap">
  <div class="top">
    <div class="brand"><div class="logo">◆</div>
      <div><h1>Plutus</h1><div class="tag">{_e(__tagline__)}</div></div></div>
    <div style="display:flex;gap:8px;align-items:center">{orgsel}{userchip}{badges}</div>
  </div>
  {banner}
  {upsell}
  {billboard}
  {cards}
  <div class="grid cols">
    <div class="panel"><h2 class="section-title">Spend by workspace <span class="hint">budget caps</span></h2>
      <table><thead><tr><th>Workspace</th><th>Cost</th><th>Tokens</th><th>Calls</th></tr></thead>
      <tbody>{ws_table}</tbody></table></div>
    <div class="panel"><h2 class="section-title">Providers <span class="hint">health · trailing $/day</span></h2>
      <table><thead><tr><th>Provider</th><th>Cost</th><th>$/day</th><th>Last call</th></tr></thead>
      <tbody>{prov_table}</tbody></table></div>
  </div>
  <div class="grid cols" style="margin-top:16px">
    {task_panel}
    <div class="panel"><h2 class="section-title">Live activity</h2><div class="feed">{feed_html}</div></div>
  </div>
  {runway_panel}
  <div class="panel" style="margin-top:16px"><h2 class="section-title">Billing <span class="hint">prepaid credits · Stripe</span></h2>{billing}</div>
  {keys_panel}
  <div class="foot">Plutus v{__version__} · self-hosted · generated {gen} · live numbers refresh every 5s<br>
    Perseus Computing LLC · <a href="https://perseus.observer/plutus/">perseus.observer/plutus</a></div>
</main>
<script>{POLLER}</script>
</body></html>"""


def landing_page(*, signed_in: bool = False, savings_share_pct: float = 10.0) -> str:
    """Public marketing landing shown at ``/`` to logged-out visitors.

    Leads with the efficiency value proposition (API-equivalent value vs. actual
    cost) and a real, attributed proof point, then the on-ramps and a Google
    sign-in CTA that provisions a free org (when ``auth.allow_signup`` is on)."""
    cta = ('<a class="btn" href="/">Open dashboard →</a>' if signed_in
           else '<a class="btn" href="/auth/login">Start free with Google →</a>')
    step = ("border:1px solid var(--line2);border-radius:11px;padding:16px 18px;"
            "background:var(--panel)")
    onramp = ("display:inline-block;font-family:var(--mono);font-size:12px;"
              "color:var(--amber);background:var(--bg2);border:1px solid var(--line2);"
              "border-radius:7px;padding:2px 8px;margin:2px 4px 2px 0")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Plutus — see what your AI stack is really worth</title>{FAVICON}
<style>{CSS}
.hero{{text-align:center;padding:34px 0 10px}}
.hero h2{{font-size:34px;line-height:1.15;margin:8px 0 10px;letter-spacing:-.5px}}
.hero p.sub{{color:var(--dim);font-size:16px;max-width:620px;margin:0 auto 22px}}
.proof{{margin:26px auto;max-width:640px;text-align:center;padding:22px;border:1px solid var(--amber-dim);
  border-radius:14px;background:rgba(245,182,63,.05)}}
.proof .big{{font-size:40px;color:var(--amber);font-weight:600;letter-spacing:-1px}}
.steps{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin:28px 0}}
.foot2{{color:var(--faint);font-size:12px;text-align:center;margin-top:30px;line-height:1.7}}</style>
</head><body><div class="wrap" style="max-width:900px">
<div class="top"><div class="brand"><div class="logo">◆</div>
  <div><h1>Plutus</h1><div class="tag">the billing layer for AI agents</div></div></div>
  <a class="pill" href="/pricing">Pricing</a></div>

<div class="hero">
  <div class="pill pro" style="display:inline-block">verifiable efficiency, not a marketing slide</div>
  <h2 class="section-title">See what your AI&nbsp;stack is <span class="amber">really worth</span>.</h2>
  <p class="sub">Plutus meters your token usage across every provider and shows you the number that
  matters: how much your setup would cost at flagship API prices versus what you actually paid.
  Routing, local models, and subscriptions make that gap huge — and every dollar is on a
  tamper-evident chain you can recompute yourself.</p>
  {cta}
</div>

<div class="proof">
  <div class="muted" style="font-size:13px;margin-bottom:6px">Our own stack, measured (July, dogfooded)</div>
  <div class="big">22×</div>
  <div style="color:var(--dim);font-size:14px;margin-top:4px">
    <b class="amber">$4,144</b> of flagship-equivalent value delivered for <b class="amber">~$185</b> actual —
    token-derived, reconstructable, verifiable.</div>
</div>

<div class="steps">
  <div style="{step}"><div class="l amber">1 · Connect</div>
    <div class="s" style="color:var(--dim)">Drop in the on-ramp that fits — nothing to change in your stack.</div>
    <div style="margin-top:10px">
      <span style="{onramp}">Claude Code plugin</span>
      <span style="{onramp}">POST /v1/usage</span>
      <span style="{onramp}">provider adapters</span></div></div>
  <div style="{step}"><div class="l amber">2 · Meter</div>
    <div class="s" style="color:var(--dim)">Every call is recorded to an append-only, hash-chained ledger —
    auditable, tamper-evident, yours.</div></div>
  <div style="{step}"><div class="l amber">3 · See your efficiency</div>
    <div class="s" style="color:var(--dim)">Live dashboard: spend by provider — verify you're getting
    the tokens you pay for, and see what Perseus routing saves once it's in the loop.</div></div>
</div>

<div style="text-align:center;margin:26px 0 8px">
  <div style="color:var(--dim);font-size:14px;margin-bottom:14px">
    <b>Free for small teams.</b> $20/mo beyond that. Optional {savings_share_pct:.0f}% share of the
    savings <b>Perseus</b> provably delivers — verified by Plutus, never a blanket percentage,
    never an automatic charge.</div>
  {cta}
  &nbsp;<a class="btn ghost" href="/pricing">Compare plans</a>
</div>

<div class="foot2">Self-hostable · single binary · MCP-native · works offline.<br>
  Perseus Computing LLC · <a href="https://perseus.observer/plutus/">perseus.observer/plutus</a></div>
</div></body></html>"""


def pricing_page(*, stripe_status: dict, org_id: str | None = None,
                 user=None, signed_in: bool = False, csrf: str = "") -> str:
    """Public plans page — the comparison surface the upgrade nudges point to."""
    from .. import pricing
    can_pro = stripe_status.get("available") and stripe_status.get("has_pro_price")
    csrf_field = (f"<input type='hidden' name='_csrf' value='{_e(csrf)}'>"
                  if csrf else "")

    # The savings-share lever, in one human line per tier.
    share_line = {
        "suggested": "Savings-share: optional tip",
        "waived": "Savings-share: waived — flat price",
        "mandatory": "Savings-share: 10% of provable savings",
        "custom": "Savings-share: negotiated",
        "none": "",
    }
    cards = []
    for key in pricing.TIER_ORDER:
        t = pricing.TIERS[key]
        if key == "team":
            price = f"${t.per_seat_usd_month:,.0f}"
            per = "<span class='muted' style='font-size:13px'>/seat/mo</span>"
        elif key == "enterprise":
            price, per = "Custom", ""
        elif key == "free":
            price = "$0"
            per = "<span class='muted' style='font-size:13px'>/mo</span>"
        else:
            price = f"${t.price_usd_month:,.0f}"
            per = "<span class='muted' style='font-size:13px'>/mo</span>"
        feats = "".join(f"<li>{_e(f)}</li>" for f in t.features)
        featured = ""
        if key == "pro":
            if not signed_in:
                cta = "<a class='btn' href='/auth/login'>Sign in to upgrade →</a>"
            elif can_pro and org_id:
                cta = (f"<form method='post' action='/billing/checkout/pro' style='margin:0'>"
                       f"<input type='hidden' name='org' value='{_e(org_id)}'>{csrf_field}"
                       f"<button class='btn' type='submit'>Upgrade to Pro →</button></form>")
            else:
                cta = "<a class='btn ghost' href='/'>Open dashboard</a>"
            featured = " style='border-color:var(--amber-dim);box-shadow:0 0 0 1px var(--amber-dim)'"
        elif key == "free":
            cta = ("<a class='btn ghost' href='/'>Open dashboard</a>" if signed_in
                   else "<a class='btn ghost' href='/auth/login'>Start free →</a>")
        elif key == "team":
            cta = ("<a class='btn ghost' href='mailto:tcconnally@gmail.com?"
                   "subject=Plutus%20Team'>Talk to us →</a>")
        else:
            cta = ("<a class='btn ghost' href='mailto:tcconnally@gmail.com?"
                   "subject=Plutus%20Enterprise'>Contact sales</a>")
        sl = share_line.get(t.savings_share, "")
        share_html = (f"<div class='muted' style='font-size:12px;margin:0 0 10px'>{_e(sl)}</div>"
                      if sl else "")
        cards.append(
            f"<div class='card'{featured}>"
            f"<div class='l'>{_e(t.name)}</div>"
            f"<div class='v amber'>{price}{per}</div>"
            f"<div class='s' style='min-height:34px'>{_e(t.blurb)}</div>"
            f"{share_html}"
            f"<ul style='list-style:none;padding:0;margin:6px 0 16px;font-size:13px;color:var(--dim)'>"
            f"{feats}</ul>{cta}</div>")
    grid = "".join(cards)

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Plutus — Pricing</title>{FAVICON}
<style>{CSS}
.card ul li{{padding:3px 0;border-top:1px solid var(--line)}}
.card ul li:first-child{{border-top:none}}</style></head><body><div class="wrap" style="max-width:980px">
<div class="top"><div class="brand"><div class="logo">◆</div>
  <div><h1>Plutus</h1><div class="tag">Plans &amp; pricing</div></div></div>
  <a class="pill" href="/">← Dashboard</a></div>
<div class="grid cards" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))">{grid}</div>
<div class="foot">All plans are self-hostable. Stripe handles billing; cancel anytime from the customer portal.<br>
  Cost estimates use public list prices as of {_e(pricing.PRICE_TABLE_AS_OF)}; pass an exact <code>cost_usd</code> or calibrate for billing-grade accuracy.<br>
  Perseus Computing LLC · <a href="https://perseus.observer/plutus/">perseus.observer/plutus</a></div>
</div></body></html>"""


def api_key_created_page(secret: str, base_url: str) -> str:
    """Show a freshly-minted API key **once** — it can't be recovered later."""
    base = base_url.rstrip("/")
    curl = (f"curl -X POST {base}/v1/usage \\\n"
            f"  -H 'Authorization: Bearer {secret}' \\\n"
            f"  -d '{{\"provider\":\"anthropic\",\"model\":\"claude-opus-4-8\","
            f"\"input_tokens\":1200,\"output_tokens\":800,\"workspace\":\"prod\"}}'")
    pre = ("margin-top:10px;padding:12px 14px;background:var(--bg2);border:1px solid var(--line2);"
           "border-radius:9px;overflow:auto;font-family:var(--mono);font-size:13px")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Plutus — API key created</title>{FAVICON}
<style>{CSS}</style></head><body><div class="wrap" style="max-width:680px">
<div class="brand" style="margin-bottom:20px"><div class="logo">◆</div><div><h1>Plutus</h1></div></div>
<div class="panel" style="padding:26px 22px">
  <h2 style="color:var(--green);font-size:18px;padding:0;text-transform:none;letter-spacing:0">API key created</h2>
  <div class="muted" style="margin-top:8px">Copy it now — for your security, Plutus stores only a hash and
  <strong>won't show this key again</strong>.</div>
  <pre style="{pre};color:var(--amber)">{_e(secret)}</pre>
  <div class="muted" style="margin-top:18px">Send usage with it:</div>
  <pre style="{pre};color:var(--dim)">{_e(curl)}</pre>
  <p style="margin-top:20px"><a href="/">← Back to dashboard</a></p>
</div></div></body></html>"""


def login_page(login_href: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Plutus — Sign in</title>{FAVICON}
<style>{CSS}</style></head><body><div class="wrap" style="max-width:460px">
<div class="brand" style="margin-bottom:20px"><div class="logo">◆</div><div><h1>Plutus</h1></div></div>
<div class="panel" style="padding:28px 24px;text-align:center">
  <h2 style="font-size:18px;padding:0;text-transform:none;letter-spacing:0">Sign in to continue</h2>
  <div class="muted" style="margin:8px 0 22px">This dashboard is private to your organization.</div>
  <a class="btn" href="{html.escape(login_href)}">Sign in with Google →</a>
</div></div></body></html>"""


def checkout_handoff_page(checkout_url: str) -> str:
    """Render a visible handoff instead of relying solely on a cross-origin 303.

    Some embedded browsers swallow a POST redirect to Stripe, leaving the
    dashboard apparently unchanged even though Checkout was created. The
    authenticated page provides an explicit, new-tab link as a reliable
    fallback without logging or persisting the ephemeral Checkout URL.
    """
    url = html.escape(checkout_url, quote=True)
    body = (
        '<p>Stripe Checkout is ready. Open it in a new tab to complete your '
        'optional one-time payment.</p>'
        f'<p><a class="btn" href="{url}" target="_blank" '
        'rel="noopener noreferrer" referrerpolicy="no-referrer">'
        'Open secure Stripe checkout →</a></p>'
        '<p style="font-size:12px">No automatic charge is created by opening '
        'Checkout.</p>')
    return simple_page("Checkout ready", "Checkout ready", body)


def simple_page(title: str, heading: str, body_html: str, *, ok: bool = True) -> str:
    """Render a minimal status page. ``title`` and ``heading`` are auto-escaped,
    but ``body_html`` is inserted as RAW HTML — callers MUST pre-escape any
    untrusted/user-controlled data (e.g. ``html.escape(...)``) before passing it."""
    color = "var(--green)" if ok else "var(--coral)"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Plutus — {_e(title)}</title>{FAVICON}
<style>{CSS}</style></head><body><div class="wrap" style="max-width:620px">
<div class="brand" style="margin-bottom:20px"><div class="logo">◆</div><div><h1>Plutus</h1></div></div>
<div class="panel" style="padding:26px 22px">
  <h2 style="color:{color};font-size:18px;padding:0;text-transform:none;letter-spacing:0">{_e(heading)}</h2>
  <div class="muted" style="margin-top:8px">{body_html}</div>
  <p style="margin-top:20px"><a href="/">← Back to dashboard</a></p>
</div></div></body></html>"""
