"""Alerts — email notifications for low balance and budget caps.

Alerts are *raised* during metering (``metering._check_thresholds`` logs them to
``alerts_log``). This module is the *delivery* side: it picks up undelivered
alerts and emails them via SMTP. It is offline-safe — with alerts disabled or
SMTP unconfigured, :func:`send_pending` runs as a dry run, returning what *would*
be sent without touching the network, and leaves the alerts marked undelivered.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

from . import db


def pending(conn, org_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM alerts_log WHERE org_id=? AND delivered=0 ORDER BY ts",
        (org_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _mark_delivered(conn, alert_id: str) -> None:
    conn.execute("UPDATE alerts_log SET delivered=1 WHERE id=?", (alert_id,))
    conn.commit()


def _build_message(alert: dict, from_addr: str, to_addrs: list[str],
                   org_name: str) -> EmailMessage:
    kind = alert["kind"].replace("_", " ").title()
    msg = EmailMessage()
    msg["Subject"] = f"[Ledger] {kind} — {org_name}"
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(
        f"{alert['message']}\n\n"
        f"Organization: {org_name}\n"
        f"Alert type:   {alert['kind']}\n\n"
        f"— Ledger, the billing layer for AI agents\n"
        f"  https://perseus.observer/ledger/\n"
    )
    return msg


def _build_checkpoint_message(cp: dict, from_addr: str, to_addrs: list[str],
                              org_name: str) -> EmailMessage:
    """#121: the out-of-band checkpoint receipt. The JSON line in the body IS
    the retained anchor — the recipient's mailbox is the independent store."""
    import json as _json
    msg = EmailMessage()
    msg["Subject"] = (f"[Ledger] Ledger checkpoint — {org_name} "
                      f"(rowid {cp['through_rowid']}, {cp['event_count']} events)")
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(
        "Retain this message: the JSON line below is a tamper-evidence anchor "
        "for your Ledger usage ledger. Your operator cannot rewrite history "
        "you hold a copy of.\n\n"
        f"{_json.dumps(cp)}\n\n"
        "Verify at any time:\n"
        "  ledger verify-checkpoints --file <file containing the line above>\n\n"
        f"Organization: {org_name}\n"
        f"Chain head:   {cp['head_hash']}\n"
        f"Signing mode: {cp['mode']}\n\n"
        "— Ledger, the billing layer for AI agents\n"
        "  https://perseus.observer/ledger/\n"
    )
    return msg


def mail_checkpoint(cfg: dict, org_name: str, cp: dict,
                    force: bool = False) -> dict:
    """#121: deliver one checkpoint receipt out of band via the alerts SMTP
    path. Offline-safe like :func:`send_pending` — unconfigured SMTP degrades
    to a dry run describing what would be sent. ``force`` bypasses the
    ``alerts.enabled`` gate (delivery explicitly requested via --deliver).

    Trust model: the anchor is only *independent* if it lands somewhere the
    operator cannot rewrite — the customer's mailbox qualifies; this DB does
    not. In-DB checkpoints remain the weaker fallback (see verify-checkpoints
    --file).
    """
    acfg = cfg.get("alerts", {})
    enabled = acfg.get("enabled") or force
    to_addrs = acfg.get("to_addrs") or []
    smtp_host = acfg.get("smtp_host") or ""
    if not (enabled and smtp_host and to_addrs):
        reason = []
        if not enabled:
            reason.append("alerts.enabled is false")
        if not smtp_host:
            reason.append("no smtp_host")
        if not to_addrs:
            reason.append("no to_addrs")
        return {"sent": 0, "dry_run": True,
                "detail": "dry run — " + "; ".join(reason)}

    from_addr = acfg.get("from_addr", "ledger@perseus.observer")
    port = int(acfg.get("smtp_port", 587))
    user = acfg.get("smtp_user") or ""
    password = acfg.get("smtp_password") or ""
    require_tls = bool(acfg.get("require_tls"))
    msg = _build_checkpoint_message(cp, from_addr, to_addrs, org_name)
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(smtp_host, port, context=ctx, timeout=20) as server:
                if user:
                    server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, port, timeout=20) as server:
                server.ehlo()
                tls_secured = False
                if "starttls" in server.esmtp_features:
                    try:
                        server.starttls(context=ctx)
                        server.ehlo()
                        tls_secured = True
                    except Exception:
                        pass
                # Same #15 discipline as send_pending: fail closed rather than
                # leak over an unencrypted link.
                if require_tls and not tls_secured:
                    return {"sent": 0, "dry_run": False,
                            "error": "alerts.require_tls is set but STARTTLS "
                                     "could not be established"}
                if user and password:
                    if not tls_secured:
                        return {"sent": 0, "dry_run": False,
                                "error": "SMTP credentials provided but TLS "
                                         "could not be established"}
                    server.login(user, password)
                server.send_message(msg)
    except Exception as e:
        return {"sent": 0, "dry_run": False, "error": str(e)}
    return {"sent": 1, "dry_run": False, "to": list(to_addrs)}


def send_pending(conn, cfg: dict, org_id: str,
                 force: bool = False) -> dict:
    """Deliver undelivered alerts for an org. Returns a summary.

    ``force`` bypasses the ``alerts.enabled`` gate (used by ``ledger alerts
    --test``). SMTP is still required to actually send; without it this is a dry
    run.
    """
    acfg = cfg.get("alerts", {})
    org = db.get_org(conn, org_id)
    org_name = org["name"] if org else org_id
    items = pending(conn, org_id)
    if not items:
        return {"sent": 0, "dry_run": False, "pending": 0, "detail": "nothing pending"}

    enabled = acfg.get("enabled") or force
    to_addrs = acfg.get("to_addrs") or []
    smtp_host = acfg.get("smtp_host") or ""
    can_send = bool(enabled and smtp_host and to_addrs)

    if not can_send:
        reason = []
        if not enabled:
            reason.append("alerts.enabled is false")
        if not smtp_host:
            reason.append("no smtp_host")
        if not to_addrs:
            reason.append("no to_addrs")
        return {
            "sent": 0, "dry_run": True, "pending": len(items),
            "would_send": [a["message"] for a in items],
            "detail": "dry run — " + "; ".join(reason),
        }

    from_addr = acfg.get("from_addr", "ledger@perseus.observer")
    port = int(acfg.get("smtp_port", 587))
    user = acfg.get("smtp_user") or ""
    password = acfg.get("smtp_password") or ""
    require_tls = bool(acfg.get("require_tls"))  # #15: fail closed if TLS unavailable

    sent = 0
    errors = []
    try:
        ctx = ssl.create_default_context()
        
        # Port 465: implicit TLS (SMTP_SSL)
        if port == 465:
            with smtplib.SMTP_SSL(smtp_host, port, context=ctx, timeout=20) as server:
                if user:
                    server.login(user, password)
                for a in items:
                    try:
                        server.send_message(_build_message(a, from_addr, to_addrs, org_name))
                        _mark_delivered(conn, a["id"])
                        sent += 1
                    except Exception as e:
                        errors.append(f"{a['id']}: {e}")
        else:
            # Other ports: use STARTTLS if available
            with smtplib.SMTP(smtp_host, port, timeout=20) as server:
                server.ehlo()
                tls_secured = False
                
                # Try STARTTLS if supported
                if "starttls" in server.esmtp_features:
                    try:
                        server.starttls(context=ctx)
                        server.ehlo()
                        tls_secured = True
                    except Exception:
                        pass
                
                # Fix (#15): with require_tls set, refuse to send at all over an
                # unencrypted link — protects alert bodies (mild PII), not just
                # credentials, from a STARTTLS downgrade/MITM.
                if require_tls and not tls_secured:
                    return {"sent": 0, "dry_run": False, "pending": len(items),
                            "error": "alerts.require_tls is set but STARTTLS "
                                     "could not be established"}

                # Only login if TLS is secured OR if no credentials are required
                if user and password:
                    if not tls_secured:
                        return {"sent": 0, "dry_run": False, "pending": len(items),
                                "error": "SMTP credentials provided but TLS could not be established"}
                    server.login(user, password)
                
                for a in items:
                    try:
                        server.send_message(_build_message(a, from_addr, to_addrs, org_name))
                        _mark_delivered(conn, a["id"])
                        sent += 1
                    except Exception as e:
                        errors.append(f"{a['id']}: {e}")
    except Exception as e:
        return {"sent": sent, "dry_run": False, "pending": len(items) - sent,
                "error": str(e)}
    return {"sent": sent, "dry_run": False, "pending": len(items) - sent,
            "errors": errors or None}


def check_and_notify(conn, cfg: dict, org_id: Optional[str] = None) -> list[dict]:
    """Convenience for cron: deliver pending alerts for one or all orgs."""
    org_ids = [org_id] if org_id else [o["id"] for o in db.list_orgs(conn)]
    return [{"org_id": oid, **send_pending(conn, cfg, oid)} for oid in org_ids]
