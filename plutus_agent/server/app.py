"""HTTP server — stdlib ``http.server`` only. No web framework dependency.

Serves the dark dashboard, a JSON API, Stripe Checkout/Portal redirects, and the
Stripe webhook, all at ``:8420`` by default. Threaded, with a fresh SQLite
connection per request (SQLite connections aren't thread-safe to share).
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .. import __version__, bridge, config as cfgmod, db, pricing
from ..billing import StripeClient, BillingError, handle_webhook_event
from ..utils import strict_int
from . import api, views, auth as authmod

# Paths reachable without a session when auth is enabled.
_PUBLIC_PATHS = {"/", "/index.html",  # public landing for logged-out visitors
                 "/healthz", "/favicon.ico", "/webhook/stripe", "/pricing",
                 "/v1/usage",  # authenticated by its own Bearer API key, not a session
                 "/v1/usage/export.csv", "/v1/usage/export.json",  # Bearer-auth (#66)
                 "/v1/checkpoints",  # Bearer-auth tamper-evidence anchors (#121)
                 "/v1/admin/orgs", "/v1/admin/credits", "/v1/admin/keys",  # admin-token (#66)
                 "/v1/admin/verify",  # ledger tamper-evidence (#108), admin-token
                 "/auth/login", "/auth/callback", "/auth/logout"}

# Max request body size (1 MiB) — protects /v1/usage and /webhook/stripe from DoS
MAX_BODY_BYTES = 1 * 1024 * 1024

# Fix #63: sane bounds on a prepaid-credit checkout. Stripe itself only rejects
# amounts <= 0, so inf / nan / a 9-figure typo would otherwise create a checkout.
CREDIT_MIN_USD = 1.0
CREDIT_MAX_USD = 10_000.0


def _parse_credit_amount(raw) -> float:
    """Parse + bound a credit top-up amount from form input. Defaults to $50 when
    blank. Raises :class:`BillingError` (rendered as a 400) on a non-numeric,
    non-finite, or out-of-range value."""
    try:
        amount = float(raw) if raw not in (None, "") else 50.0
    except (TypeError, ValueError):
        raise BillingError("amount must be a number")
    if not math.isfinite(amount) or not (CREDIT_MIN_USD <= amount <= CREDIT_MAX_USD):
        raise BillingError(
            f"amount must be between ${CREDIT_MIN_USD:,.0f} and ${CREDIT_MAX_USD:,.0f}"
        )
    return amount


def _qs_int(q: dict, name: str, default, lo: int, hi: int):
    """Parse a bounded integer query param (parse_qs dict). Returns ``default``
    when absent or unparseable; clamps into [lo, hi] otherwise (fix #66)."""
    raw = (q.get(name) or [None])[0]
    if raw is None or raw == "":
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


def _qs_float(q: dict, name: str):
    raw = (q.get(name) or [None])[0]
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class _BodyTooLarge(Exception):
    """Raised when Content-Length exceeds MAX_BODY_BYTES."""


class _OrgRequired(Exception):
    """A signed-in user who belongs to >1 org issued a state-changing request
    without naming one. We refuse rather than silently act on the earliest org
    (Fix #37 item 4) — the caller turns this into a 400."""


class _Ctx:
    """Shared, read-mostly server context."""
    def __init__(self, cfg, db_path, demo=False):
        self.cfg = cfg
        self.db_path = db_path
        self.demo = demo
        self.stripe = StripeClient(cfg)
        self.auth_on = cfgmod.auth_enabled(cfg)
        self._runway = None
        self._runway_ts = 0
        # Fix #65: per-key ingest token buckets {key_hash: [tokens, last_ts]}.
        self._buckets: dict[str, list] = {}
        self._bucket_lock = threading.Lock()

    def runway(self, authed: bool = False):
        """Cached monitor bridge (refresh at most every 60s).

        Fix #65: when auth is enabled, the bridge subprocess only runs for an
        authenticated request — an unauthenticated dashboard hit never triggers
        the shell-out. (With auth off the server is in local trusted mode.)
        """
        mon = self.cfg.get("monitor", {})
        if not mon.get("enabled"):
            return None
        if self.auth_on and not authed:
            return self._runway  # serve the last cached value, don't shell out
        now = time.time()
        if now - self._runway_ts > 60:
            self._runway = bridge.runway(mon)
            self._runway_ts = now
        return self._runway

    def allow_ingest(self, key_hash: str, now: float) -> bool:
        """Per-key token-bucket rate limit for /v1/usage (fix #65). Returns True
        if a request is permitted and consumes a token."""
        ing = self.cfg.get("ingest", {})
        rate = float(ing.get("rate_per_min", 600) or 0)
        if rate <= 0:
            return True  # limiting disabled
        burst = float(ing.get("burst", rate) or rate)
        refill_per_sec = rate / 60.0
        with self._bucket_lock:
            tokens, last = self._buckets.get(key_hash, [burst, now])
            tokens = min(burst, tokens + (now - last) * refill_per_sec)
            if tokens < 1.0:
                self._buckets[key_hash] = [tokens, now]
                return False
            self._buckets[key_hash] = [tokens - 1.0, now]
            return True


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, ctx):
        super().__init__(addr, handler)
        self.ctx = ctx


class Handler(BaseHTTPRequestHandler):
    server_version = f"Plutus/{__version__}"

    # ---- helpers -----------------------------------------------------------
    @property
    def ctx(self) -> _Ctx:
        return self.server.ctx

    def _conn(self):
        return db.connect(self.ctx.db_path)

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Security headers on every response: block clickjacking + MIME-sniffing
        # and lock the resource origin. The dashboard is inline-only and loads no
        # external resources (favicon is a data: URI), so script/style keep
        # 'unsafe-inline'; moving to a nonce-based script-src is a follow-up.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "frame-ancestors 'none'; base-uri 'none'; object-src 'none'; "
                         "form-action 'self'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, obj, headers=None):
        self._send(code, json.dumps(obj, default=str), "application/json",
                   headers=headers)

    def _redirect(self, url):
        self.send_response(303)
        self.send_header("Location", url)
        # Explicit zero-length body: without it some proxies / in-app webviews
        # won't treat the 303 as complete and never follow the Location.
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self, max_bytes=MAX_BODY_BYTES) -> bytes:
        # Cache per request so the body can be read more than once (the CSRF gate
        # reads it to check the token, then the handler parses it). Reset in
        # do_POST so a keep-alive connection's next request starts fresh.
        if getattr(self, "_cached_body", None) is not None:
            return self._cached_body
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > max_bytes:
            raise _BodyTooLarge(f"request body {n} bytes exceeds limit {max_bytes}")
        self._cached_body = self.rfile.read(n) if n else b""
        return self._cached_body

    def _form(self) -> dict:
        raw = self._body().decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def log_message(self, fmt, *args):  # quieter, single-line
        sys.stderr.write("plutus %s — %s\n" % (self.address_string(), fmt % args))

    def _log_exc(self, method, path, exc) -> str:
        """Log an unhandled exception server-side with a short reference id and
        return that id. Clients get only the id — never ``str(exc)`` — so SQLite
        / internal detail can't leak through a 500 (Fix #37 item 2)."""
        ref = secrets.token_hex(4)
        sys.stderr.write(f"plutus: [{ref}] {method} {path} failed: {exc!r}\n")
        return ref

    # ---- auth --------------------------------------------------------------
    def _gate(self, conn, path) -> bool:
        """When auth is on, require a session for non-public paths.

        Sets ``self._user`` (row or None). Returns True if the request may
        proceed, False if a 401/redirect was already sent.
        """
        self._user = None
        if not self.ctx.auth_on:
            return True
        self._user = authmod.current_user(self, conn)
        if self._user or path in _PUBLIC_PATHS:
            return True
        # not signed in, protected path
        if path.startswith("/api/") or self.command == "POST":
            self._json(401, {"error": "authentication required"})
        else:
            self._redirect("/auth/login")
        return False

    def _scoped_orgs(self, conn):
        """Orgs visible to this request (all when auth is off)."""
        if self._user is not None:
            return db.list_orgs_for_email(conn, self._user["email"])
        return db.list_orgs(conn)

    def _authz_org(self, conn, requested, *, strict=False):
        """Resolve the org for this request, enforcing membership when signed in.

        Returns an org_id, or None meaning "no org available". Raises
        :class:`PermissionError` if a signed-in user asks for an org they are
        not a member of.

        With ``strict`` (used by state-changing POSTs — billing, API keys), a
        signed-in user who belongs to more than one org *must* name one: we raise
        :class:`_OrgRequired` instead of silently defaulting to the earliest org,
        so a blank ``org`` field can't bill/key the wrong one (Fix #37 item 4).
        Dashboard GETs stay lenient — defaulting to the first org is the expected
        landing behavior, with the org selector to switch.
        """
        if self._user is None:
            return requested or api.default_org_id(conn)
        orgs = db.list_orgs_for_email(conn, self._user["email"])
        ids = {o["id"] for o in orgs}
        if requested:
            if requested not in ids:
                raise PermissionError(requested)
            return requested
        if strict and len(orgs) > 1:
            raise _OrgRequired()
        return orgs[0]["id"] if orgs else None

    def _auth_login(self):
        try:
            url, state = authmod.login_url(self.ctx.cfg)
        except authmod.AuthError as e:
            return self._send(500, views.simple_page(
                "Sign in", "Sign-in is misconfigured", html.escape(str(e)), ok=False))
        # Bind this login attempt to the browser (login-CSRF guard): the callback
        # state must match this cookie.
        return self._send(200, views.login_page(url), headers={
            "Set-Cookie": authmod.set_state_cookie_header(state, self.ctx.cfg)})

    def _client_ip(self) -> str:
        """Best-effort client IP for rate limiting (fix #59). Honors the first
        X-Forwarded-For hop only when auth.trust_forwarded_for is set (i.e. the
        server runs behind a trusted reverse proxy); otherwise the socket peer."""
        if self.ctx.cfg.get("auth", {}).get("trust_forwarded_for"):
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()
        try:
            return self.client_address[0]
        except Exception:
            return ""

    def _auth_callback(self, conn, q):
        flat = {k: (v[0] if isinstance(v, list) else v) for k, v in q.items()}
        try:
            token = authmod.handle_callback(
                conn, self.ctx.cfg, flat, client_ip=self._client_ip(),
                cookie_state=authmod.read_cookie(self, authmod.STATE_COOKIE))
        except authmod.AuthError as e:
            return self._send(403, views.simple_page(
                "Sign in", "Could not sign you in", html.escape(str(e)), ok=False))
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", authmod.set_cookie_header(token, self.ctx.cfg))
        self.send_header("Set-Cookie", authmod.clear_state_cookie_header(self.ctx.cfg))
        self.end_headers()

    def _auth_logout(self, conn):
        db.delete_session(conn, authmod.read_cookie(self))
        self.send_response(303)
        self.send_header("Location", "/auth/login")
        self.send_header("Set-Cookie", authmod.clear_cookie_header(self.ctx.cfg))
        self.end_headers()

    def _same_origin(self) -> bool:
        """CSRF defense: the request's Origin/Referer host must match our own.

        Prefer the configured ``auth.base_url``; if it's unset, fall back to the
        request's own ``Host`` header so we still **fail closed** — Fix #32: the
        previous code returned ``True`` (allowing any cross-origin POST) whenever
        ``base_url`` was unconfigured. A request carrying neither Origin nor
        Referer is rejected.
        """
        base_url = (self.ctx.cfg.get("auth", {}).get("base_url") or "").rstrip("/")
        expected = (urlparse(base_url).netloc.lower() if base_url
                    else (self.headers.get("Host") or "").lower())
        if not expected:
            return False
        # Origin is preferred; Referer is the fallback. A present-but-mismatched
        # Origin blocks even if Referer would match.
        for header in ("Origin", "Referer"):
            val = self.headers.get(header, "")
            if val:
                return urlparse(val).netloc.lower() == expected
        return False

    def _csrf_token_ok(self) -> bool:
        """Fix #58: validate the per-session CSRF synchronizer token from the
        ``_csrf`` form field against the value derived from the session cookie.
        Defense-in-depth that lets through legit requests whose Origin/Referer was
        stripped, while a forgery (which can't know the session token) still fails."""
        sess = authmod.read_cookie(self)
        if not sess:
            return False
        expected = authmod.csrf_token(sess)
        submitted = self._form().get("_csrf", "")
        return bool(submitted) and secrets.compare_digest(submitted, expected)

    # ---- routing -----------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        conn = self._conn()
        try:
            if not self._gate(conn, path):
                return
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/healthz":
                return self._json(200, {"ok": True, "version": __version__,
                                        "demo": self.ctx.demo})
            if path == "/auth/login":
                return self._auth_login()
            if path == "/auth/callback":
                return self._auth_callback(conn, q)
            # /auth/logout moved to POST for CSRF protection
            if path == "/auth/logout":
                return self._json(405, {"error": "use POST to logout"})
            if path == "/pricing":
                return self._pricing(conn)
            if path == "/api/orgs":
                limit = _qs_int(q, "limit", None, lo=1, hi=1000)
                offset = _qs_int(q, "offset", 0, lo=0, hi=10_000_000) or 0
                return self._json(200, api.orgs_json(
                    conn, self._scoped_orgs(conn), limit=limit, offset=offset))
            if path == "/api/users":
                org_id = self._authz_org(conn, q.get("org", [None])[0])
                if not org_id:
                    return self._json(404, {"error": "no organizations"})
                return self._json(200, {
                    "org_id": org_id,
                    "users": [dict(u) for u in db.list_users(conn, org_id, True)],
                    "active_seats": db.active_seat_count(conn, org_id),
                })
            if path == "/api/summary":
                org_id = self._authz_org(conn, q.get("org", [None])[0])
                if not org_id:
                    return self._json(404, {"error": "no organizations"})
                return self._json(200, api.summary_json(conn, org_id))
            if path == "/api/audit":
                org_id = self._authz_org(conn, q.get("org", [None])[0])
                if not org_id:
                    return self._json(404, {"error": "no organizations"})
                return self._json(200, api.audit_json(
                    conn, org_id, hmac_key=cfgmod.chain_hmac_key(self.ctx.cfg),
                    external_ref=q.get("external_ref", [None])[0]))
            if path == "/api/ledger":
                org_id = self._authz_org(conn, q.get("org", [None])[0])
                if not org_id:
                    return self._json(404, {"error": "no organizations"})
                return self._json(200, api.ledger_json(
                    conn, org_id, limit=_qs_int(q, "limit", 50, lo=1, hi=500),
                    before=_qs_int(q, "before", None, lo=1, hi=2**63 - 1)))
            if path == "/api/events":
                org_id = self._authz_org(conn, q.get("org", [None])[0])
                if not org_id:
                    return self._json(404, {"error": "no organizations"})
                return self._json(200, api.events_json(
                    conn, org_id, limit=_qs_int(q, "limit", 50, lo=1, hi=500),
                    before=_qs_int(q, "before", None, lo=1, hi=2**63 - 1)))
            if path in ("/v1/usage/export.csv", "/v1/usage/export.json"):
                return self._usage_export(conn, path, q)
            if path == "/v1/checkpoints":
                return self._checkpoints_get(conn, q)
            if path.startswith("/v1/admin/"):
                return self._admin_get(conn, path, q)
            if path in ("/billing/success", "/billing/cancel"):
                ok = path.endswith("success")
                return self._send(200, views.simple_page(
                    "Billing", "Payment complete ✓" if ok else "Checkout canceled",
                    "Your credit balance updates as soon as Stripe confirms the payment "
                    "(via webhook). You can close this tab." if ok else
                    "No charge was made.", ok=ok))
            if path == "/" or path == "/index.html":
                # Public landing for logged-out visitors (auth on, no session);
                # the dashboard for signed-in users and local trusted mode.
                if self.ctx.auth_on and self._user is None:
                    from .. import savings as _sav
                    pct = _sav.rate_bps_from_config(self.ctx.cfg) / 100.0
                    return self._send(200, views.landing_page(
                        signed_in=False, savings_share_pct=pct))
                return self._dashboard(conn, q)
            return self._send(404, views.simple_page("Not found", "404",
                              f"No route for <span class='num'>{html.escape(path)}</span>.",
                              ok=False))
        except PermissionError:
            return self._json(403, {"error": "forbidden: not a member of that org"})
        except Exception as e:  # never 500 silently
            ref = self._log_exc("GET", path, e)
            return self._send(500, views.simple_page(
                "Error", "Something broke",
                f"An internal error occurred. Reference: "
                f"<span class='num'>{html.escape(ref)}</span>.", ok=False))
        finally:
            conn.close()

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        self._cached_body = None  # fresh per request (keep-alive safe)
        conn = self._conn()
        try:
            if not self._gate(conn, path):
                return

            # CSRF protection: cookie-authenticated state-changing POSTs must pass
            # a same-origin check OR carry a valid per-session synchronizer token
            # (#58 — the token lets through legit requests whose Origin/Referer a
            # privacy proxy stripped, while still blocking forgeries that have
            # neither). Exempt: /v1/usage (Bearer auth), /webhook/stripe (signed).
            needs_csrf_check = (
                self.ctx.auth_on and
                path not in {"/v1/usage", "/webhook/stripe",
                             "/v1/checkpoints"} and  # Bearer-auth (#121)
                self._user is not None  # Cookie-authenticated
            )
            if needs_csrf_check and not (self._same_origin() or self._csrf_token_ok()):
                return self._json(403, {"error": "cross-origin request blocked"})
            
            if path == "/auth/logout":
                return self._auth_logout(conn)
            if path == "/v1/usage":
                return self._ingest_usage(conn)
            if path == "/v1/checkpoints":
                return self._checkpoints_post(conn)
            if path == "/api/users":
                return self._users_create(conn)
            if path == "/api/users/deactivate":
                return self._users_deactivate(conn)
            if path == "/webhook/stripe":
                return self._webhook(conn)
            if path == "/billing/checkout/credit":
                return self._checkout_credit(conn)
            if path == "/billing/checkout/pro":
                return self._checkout_pro(conn)
            if path == "/billing/checkout/team":
                return self._checkout_team(conn)
            if path == "/billing/checkout/donate":
                return self._checkout_donate(conn)
            if path == "/billing/portal":
                return self._portal(conn)
            if path == "/keys/create":
                return self._keys_create(conn)
            if path == "/keys/revoke":
                return self._keys_revoke(conn)
            if path.startswith("/v1/admin/"):
                return self._admin_post(conn, path)
            return self._json(404, {"error": f"no route for {path}"})
        except _BodyTooLarge:
            return self._json(413, {"error": "request body too large"})
        except _OrgRequired:
            return self._json(400, {"error": "specify which organization "
                                    "(the 'org' field is required when you belong "
                                    "to more than one)"})
        except PermissionError:
            return self._json(403, {"error": "forbidden: not a member of that org"})
        except BillingError as e:
            # BillingError messages are operator-facing config guidance, not
            # internal detail — safe to show.
            return self._send(400, views.simple_page("Billing", "Billing not available",
                              str(e), ok=False))
        except sqlite3.OperationalError as e:
            # Transient write contention anywhere in the request (e.g. an auth or
            # idempotency-replay read that lost the lock race outside the ingest
            # transaction's own handler). Nothing committed, so it's retryable:
            # answer 503 + Retry-After, never a scary non-retryable 500. Surfaced
            # by the concurrency soak at 500-way contention (5/30k requests);
            # see docs/CONCURRENCY-PROOF-2026-07.md.
            self._log_exc("POST", path, e)
            return self._json(503, {"error": "temporarily busy, retry shortly"},
                              headers={"Retry-After": "1"})
        except Exception as e:
            ref = self._log_exc("POST", path, e)
            return self._json(500, {"error": "internal error", "ref": ref})
        finally:
            conn.close()

    # ---- views -------------------------------------------------------------
    def _dashboard(self, conn, q):
        org_id = self._authz_org(conn, q.get("org", [None])[0])
        if not org_id:
            return self._send(200, views.simple_page(
                "Plutus", "No organization yet",
                "Run <span class='num'>plutus init</span> to create one, or "
                "<span class='num'>plutus serve --demo</span> to explore with sample data.",
            ))
        from .. import metering
        summary = metering.org_summary(conn, org_id)
        # Savings billboard (all tiers) + tip-jar amount (Free): current-month
        # efficiency and the provable savings-share. Wrapped so a pricing/query
        # hiccup can never take the dashboard down.
        import time as _time
        from .. import efficiency as _eff, savings as _sav
        _lbl = _time.strftime("%Y-%m", _time.gmtime())
        try:
            summary["efficiency"] = _eff.org_efficiency(
                conn, org_id, period_label=_lbl).as_dict()
        except Exception:
            summary["efficiency"] = None
        try:
            _rate = _sav.rate_bps_from_config(self.ctx.cfg)
            summary["savings_share"] = _sav.savings_share_report(
                conn, org_id, _lbl, rate_bps=_rate).as_dict()
            summary["audit"] = api.audit_json(
                conn, org_id, hmac_key=cfgmod.chain_hmac_key(self.ctx.cfg))
        except Exception:
            summary["savings_share"] = None
        integrity = db.verify_chain(  # #108: tamper-evidence tile for this org
            conn, org_id=org_id, hmac_key=cfgmod.chain_hmac_key(self.ctx.cfg))
        # #121: latest retained anchor for the checkpoint tile (None = never
        # anchored — the tile nudges toward `plutus checkpoint`).
        ckpts = db.list_checkpoints(conn, org_id)
        page = views.render_dashboard(
            summary, orgs=self._scoped_orgs(conn), cfg=self.ctx.cfg,
            stripe_status=self.ctx.stripe.status(), demo=self.ctx.demo,
            runway=self.ctx.runway(authed=self._user is not None), user=self._user,
            api_keys=[dict(k) for k in db.list_api_keys(conn, org_id)],
            csrf=authmod.csrf_token(authmod.read_cookie(self)),
            integrity=integrity,
            checkpoint=(ckpts[-1] if ckpts else None),
        )
        return self._send(200, page)

    def _pricing(self, conn):
        signed_in = (not self.ctx.auth_on) or (self._user is not None)
        org_id = None
        if signed_in:
            try:
                org_id = self._authz_org(conn, None)
            except PermissionError:
                org_id = None
        return self._send(200, views.pricing_page(
            stripe_status=self.ctx.stripe.status(),
            org_id=org_id, user=self._user, signed_in=signed_in,
            csrf=authmod.csrf_token(authmod.read_cookie(self))))

    # ---- ingest API --------------------------------------------------------
    def _bearer_org(self, conn):
        """Resolve the org from an ``Authorization: Bearer plutus_sk_…`` header."""
        h = self.headers.get("Authorization", "")
        if h[:7].lower() != "bearer ":
            return None
        return db.api_key_org(conn, h[7:].strip())

    def _usage_export(self, conn, path, q):
        """GET /v1/usage/export.csv|json — org-scoped spend export (#66), Bearer-
        authenticated like /v1/usage. Optional ?since & ?until epoch bounds."""
        org_id = self._bearer_org(conn)
        if not org_id:
            return self._json(401, {"error": "invalid or missing API key"})
        since, until = _qs_float(q, "since"), _qs_float(q, "until")
        if path.endswith(".json"):
            return self._json(200, api.export_json(conn, org_id, since, until))
        csv_text = api.export_csv(conn, org_id, since, until)
        return self._send(200, csv_text, "text/csv; charset=utf-8",
                          {"Content-Disposition": 'attachment; filename="usage.csv"'})

    def _checkpoints_get(self, conn, q):
        """GET /v1/checkpoints — the org's retained anchors (#121). Bearer-
        authenticated for API callers; falls back to the signed-in session for
        the dashboard's download link. The customer stores these OUT OF BAND;
        the in-DB copies are the weaker fallback (an operator could rewrite
        them), which is why the response says so."""
        org_id = self._bearer_org(conn)
        if not org_id:
            try:  # browser path: dashboard download link, session-scoped
                org_id = self._authz_org(conn, q.get("org", [None])[0])
            except Exception:
                org_id = None
        if not org_id:
            return self._json(401, {"error": "invalid or missing API key "
                                             "(or sign in and retry)"})
        return self._json(200, {
            "org_id": org_id,
            "checkpoints": db.list_checkpoints(conn, org_id),
            "note": "retain these out of band (email/S3 object-lock/git) — "
                    "in-DB copies alone cannot prove operator honesty; verify "
                    "with `plutus verify-checkpoints --file <retained copy>`",
        })

    def _checkpoints_post(self, conn):
        """POST /v1/checkpoints — record a fresh tamper-evidence anchor for the
        caller's org, Bearer-authenticated (#121). Returns the checkpoint dict
        (the thing to retain). Idempotent per chain head."""
        org_id = self._bearer_org(conn)
        if not org_id:
            return self._json(401, {"error": "invalid or missing API key"})
        cp = db.checkpoint_chain(conn, org_id,
                                 hmac_key=cfgmod.chain_hmac_key(self.ctx.cfg))
        if cp is None:
            return self._json(200, {"recorded": False,
                                    "detail": "no chained events yet"})
        return self._json(200, {"recorded": True, "checkpoint": cp})

    def _usage_response(self, conn, org_id, out, n_blocked, n_over_balance, cfg):
        """Build the (code, body) for a recorded /v1/usage batch. Called inside the
        db.immediate block so the stored idempotent response commits atomically
        with the debits it describes."""
        from .. import metering
        st = metering.tier_status(conn, org_id)
        body = {
            "org_id": org_id,
            "tracked_tokens_mtd": st["tracked_tokens"],
            "tracked_limit": st["tracked_limit"],
            "tier": st["tier"],
        }
        n_recorded = len(out) - n_blocked - n_over_balance
        # #170: never let a workspace fold pass silently — when the tier cap
        # (Free = 1 workspace) forced any event into the org's earliest
        # workspace, say so at the top level in addition to the per-result flag.
        if any(r.get("workspace_folded") for r in out):
            body["workspace_note"] = (
                "tier workspace cap reached: event(s) tagged with a new "
                "workspace were attributed to the org's earliest workspace "
                "instead (see workspace_folded / workspace_id per result)")
        if len(out) == 1:
            body.update(out[0])
        else:
            body["results"] = out
            body["recorded"] = n_recorded
            # Fix #62: surface BOTH rejection reasons. `blocked` is the *total*
            # so a 200 is never mistaken for "all recorded"; the breakdown
            # distinguishes free-tier quota from the prepaid hard-stop, which a
            # reconciler / SDK needs to act on a partially-rejected batch.
            body["blocked"] = n_blocked + n_over_balance
            body["free_limit_blocked"] = n_blocked
            body["over_balance_blocked"] = n_over_balance
        # 402 whenever NOTHING landed, regardless of the mix of reasons (Fix #62).
        if n_recorded == 0:
            base = (cfg.get("auth", {}).get("base_url") or "").rstrip("/")
            if n_over_balance and not n_blocked:
                body["error"] = "prepaid credit exhausted"
            elif n_blocked and not n_over_balance:
                body["error"] = "free-tier token quota reached — upgrade to Pro"
            else:
                body["error"] = ("usage rejected: free-tier quota and prepaid "
                                 "credit both exhausted")
            body["upgrade_url"] = (base + "/pricing") if base else "/pricing"
            return 402, body
        return 200, body

    def _ingest_usage(self, conn):
        """POST /v1/usage — meter one event (or a JSON array of them) via API key.

        Returns the metered result(s). When an org on a limited tier is past its
        quota and hard-blocking is on, the event is rejected with 402.
        """
        org_id = self._bearer_org(conn)
        if not org_id:
            return self._json(401, {"error": "invalid or missing API key"})
        # Fix #65: per-key token-bucket rate limit. The bucket key is a hash of
        # the bearer token (the raw secret is never stored or logged).
        token = self.headers.get("Authorization", "")[7:].strip()
        key_hash = hashlib.sha256(token.encode()).hexdigest()
        if not self.ctx.allow_ingest(key_hash, time.time()):
            return self._json(429, {"error": "rate limit exceeded — slow down"})
        idem_key = (self.headers.get("Idempotency-Key") or "").strip() or None
        try:
            body = self._body()
        except _BodyTooLarge:
            raise
        try:
            payload = json.loads(body or b"{}")
        except Exception:
            return self._json(400, {"error": "body must be JSON"})
        events = payload if isinstance(payload, list) else [payload]
        if not events or len(events) > 1000:
            return self._json(400, {"error": "send 1–1000 events"})

        from .. import metering
        cfg = self.ctx.cfg
        block_free = bool(cfg.get("pricing", {}).get("block_over_free_limit"))
        block_balance = bool(cfg.get("pricing", {}).get("block_over_balance"))
        chain_key = cfgmod.chain_hmac_key(cfg)  # #108: keyed-MAC chain, if configured

        # Fix #27: validate ALL events before recording ANY
        for ev in events:
            if not isinstance(ev, dict) or not ev.get("provider"):
                return self._json(400, {"error": "each event needs a 'provider'"})
            # Validate int fields coerce AND are non-negative. Fix #80: a negative
            # token count would rewind tracked_tokens_mtd (bypassing the free-tier
            # quota) and corrupt every SUM(tokens) aggregate — the token-count
            # sibling of the #61 negative-cost_usd guard.
            try:
                if any(int(ev.get(f, 0) or 0) < 0 for f in (
                        "input_tokens", "output_tokens",
                        "cache_read_tokens", "cache_write_tokens",
                        "reasoning_tokens")):
                    return self._json(400, {"error": "token fields must be non-negative"})
            except (TypeError, ValueError):
                return self._json(400, {"error": "token fields must be integers"})
            # Fix #61: reject a negative (or non-numeric) cost_usd before it can
            # reach the debit hot path, where it would mint credit and bypass the
            # prepaid hard-stop. None means "estimate from tokens" and is allowed.
            cost = ev.get("cost_usd")
            if cost is not None:
                try:
                    if float(cost) < 0:
                        return self._json(400, {"error": "cost_usd must be non-negative"})
                except (TypeError, ValueError):
                    return self._json(400, {"error": "cost_usd must be a number"})
            # #7: optional savings-share counterfactual. Same guard as cost_usd —
            # a negative baseline could only inflate billable savings. None means
            # "no baseline recorded" and never contributes to savings.
            baseline = ev.get("baseline_cost_usd")
            if baseline is not None:
                try:
                    if float(baseline) < 0:
                        return self._json(400, {"error": "baseline_cost_usd must be non-negative"})
                except (TypeError, ValueError):
                    return self._json(400, {"error": "baseline_cost_usd must be a number"})
            # #7: alternatively name the baseline model; the server prices the same
            # tokens from its published table (a string, not a dollar amount).
            bmodel = ev.get("baseline_model")
            if bmodel is not None and not isinstance(bmodel, str):
                return self._json(400, {"error": "baseline_model must be a string"})
            # #134: token-reduction counterfactual counts. Same non-negative rule
            # as the actual token fields — a negative count could only inflate
            # the baseline and therefore billable savings.
            try:
                if any(ev.get(f) is not None and int(ev.get(f)) < 0 for f in (
                        "baseline_input_tokens", "baseline_output_tokens")):
                    return self._json(400, {"error": "baseline token fields must be non-negative"})
            except (TypeError, ValueError):
                return self._json(400, {"error": "baseline token fields must be integers"})
            # #8: efficiency-leakage counterfactual — same guard as the baseline.
            optimal = ev.get("optimal_cost_usd")
            if optimal is not None:
                try:
                    if float(optimal) < 0:
                        return self._json(400, {"error": "optimal_cost_usd must be non-negative"})
                except (TypeError, ValueError):
                    return self._json(400, {"error": "optimal_cost_usd must be a number"})
            omodel = ev.get("optimal_model")
            if omodel is not None and not isinstance(omodel, str):
                return self._json(400, {"error": "optimal_model must be a string"})
            # #20-arc shape A: per-task attribution ref (e.g. an Invarium task_id).
            xref = ev.get("external_ref")
            if xref is not None and not isinstance(xref, str):
                return self._json(400, {"error": "external_ref must be a string"})
            uid = ev.get("user_id")
            if uid is not None and not isinstance(uid, str):
                return self._json(400, {"error": "user_id must be a string"})

        # All valid — record the whole batch as one serialized transaction.
        # Fix #27/#30: db.immediate() takes the write lock up front (BEGIN
        # IMMEDIATE) so the per-event quota / prepaid hard-stop reads can't race
        # a concurrent writer between the read and the insert, and commits once
        # (no partial batch). record_usage(commit=False) defers to that commit.
        out, n_blocked, n_over_balance = [], 0, 0
        replay = False
        try:
            with db.immediate(conn):
                # Fix #65 + idempotency-reclaim double-debit fix: claim the
                # Idempotency-Key AND store the response in the SAME transaction as
                # the debits. Previously the claim+events committed here but the
                # response/status was stored in a later separate commit — so a
                # crash in between left committed events behind a NULL-status claim
                # that the 120s reclaim would delete, letting a retry re-record the
                # batch (double-debit). A truly-concurrent duplicate is serialized
                # too: BEGIN IMMEDIATE makes the 2nd claim wait, then it replays.
                if idem_key and not db.claim_idempotency_key(
                        conn, org_id, idem_key, commit=False):
                    replay = True
                else:
                    for ev in events:
                        res = metering.record_usage(
                            conn, org_id, provider=str(ev["provider"]),
                            model=ev.get("model"), task_type=ev.get("task_type", "general"),
                            workspace=ev.get("workspace"),
                            input_tokens=strict_int(ev.get("input_tokens", 0) or 0),
                            output_tokens=strict_int(ev.get("output_tokens", 0) or 0),
                            cache_read_tokens=strict_int(ev.get("cache_read_tokens", 0) or 0),
                            cache_write_tokens=(
                                strict_int(ev["cache_write_tokens"])
                                if ev.get("cache_write_tokens") is not None else None),
                            reasoning_tokens=strict_int(ev.get("reasoning_tokens", 0) or 0),
                            cost_usd=ev.get("cost_usd"),
                            baseline_cost_usd=ev.get("baseline_cost_usd"),
                            baseline_model=ev.get("baseline_model"),
                            baseline_input_tokens=(
                                strict_int(ev["baseline_input_tokens"])
                                if ev.get("baseline_input_tokens") is not None else None),
                            baseline_output_tokens=(
                                strict_int(ev["baseline_output_tokens"])
                                if ev.get("baseline_output_tokens") is not None else None),
                            optimal_cost_usd=ev.get("optimal_cost_usd"),
                            optimal_model=ev.get("optimal_model"),
                            external_ref=ev.get("external_ref"),
                            user_id=ev.get("user_id"),
                            source=ev.get("source", "api"),
                            pricing_overrides=cfg.get("pricing", {}).get("overrides"),
                            alert_cfg=cfg.get("alerts", {}),
                            block_over_limit=block_free,
                            block_over_balance=block_balance,
                            chain_hmac_key=chain_key,
                            commit=False,  # defer commit to db.immediate()
                        )
                        if not res.recorded:
                            if res.over_balance:
                                n_over_balance += 1
                            else:
                                n_blocked += 1
                        out.append({
                            "recorded": res.recorded,
                            "event_id": res.event_id or None,
                            "cost_usd": res.cost_usd,
                            "estimated": res.estimated,
                            "balance_after": res.balance_after,
                            "over_free_limit": res.over_free_limit,
                            "over_balance": res.over_balance,
                            "unpriced": res.unpriced,
                            "savings_usd": res.savings_usd,
                            "baseline_usd": res.baseline_usd,
                            "leaked_usd": res.leaked_usd,
                            "user_id": res.user_id,
                            # #170: attribute verbatim — plus the signal that a
                            # tier-capped workspace fold rewrote the client-sent
                            # workspace, so per-source breakdowns never silently
                            # collapse.
                            "workspace_id": res.workspace_id,
                            "workspace_folded": res.workspace_folded,
                        })
                    code, body = self._usage_response(
                        conn, org_id, out, n_blocked, n_over_balance, cfg)
                    if idem_key:
                        db.store_idempotency_response(
                            conn, org_id, idem_key, code, json.dumps(body), commit=False)
        except sqlite3.OperationalError as e:
            # Transient write contention (e.g. "database is locked" after the
            # busy_timeout). The batch did NOT commit — BEGIN IMMEDIATE rolled it
            # back, so the ledger is untouched and the client may safely retry.
            # This must be a retryable 503, not a 400 (which says "don't retry").
            # Surfaced by the concurrency soak under saturation; see
            # docs/CONCURRENCY-PROOF-2026-07.md.
            self._log_exc("POST", "/v1/usage", e)
            return self._json(503, {"error": "temporarily busy, retry shortly"},
                              headers={"Retry-After": "1"})
        except Exception:
            return self._json(400, {"error": "batch recording failed"})

        # Fix #65: a duplicate Idempotency-Key replays the stored response rather
        # than re-recording, so a client retry never double-charges.
        if replay:
            prev = db.idempotency_response(conn, org_id, idem_key)
            if prev and prev[0] is not None:
                status, resp = prev
                replayed = json.loads(resp)
                replayed["idempotent_replay"] = True
                return self._json(int(status), replayed)
            # Claimed but the original response isn't stored yet (in flight).
            return self._json(409, {"error":
                "a request with this Idempotency-Key is still in progress"})

        # #150: record ingest health. Source is the API key prefix (first 16
        # chars of the bearer token) so the operator can tie diagnostics to a
        # specific key without exposing the raw secret.
        source = f"key:{token[:16]}..."
        ok = code < 400
        error = None if ok else body.get("error", "ingest error")
        db.record_ingest_health(conn, org_id, source, ok=ok, error=error)

        return self._json(code, body)

    # ---- admin API (token-scoped; #66) -------------------------------------
    def _admin_auth(self):
        """Tri-state admin-token check. None = disabled (no token configured),
        False = present but wrong, True = authorized. Constant-time compare."""
        tok = (self.ctx.cfg.get("admin", {}).get("token") or "").strip()
        if not tok:
            return None
        h = self.headers.get("Authorization", "")
        if h[:7].lower() != "bearer ":
            return False
        return secrets.compare_digest(h[7:].strip(), tok)

    def _admin_gate(self):
        """Return True if the request is an authorized admin call, else send the
        appropriate response (404 when disabled, 401 when the token is wrong)."""
        st = self._admin_auth()
        if st is None:
            self._json(404, {"error": "admin API not enabled"})
            return False
        if not st:
            self._json(401, {"error": "invalid admin token"})
            return False
        return True

    def _admin_get(self, conn, path, q):
        if not self._admin_gate():
            return
        if path == "/v1/admin/orgs":
            return self._json(200, {"orgs": api.orgs_json(
                conn, limit=_qs_int(q, "limit", 100, lo=1, hi=1000),
                offset=_qs_int(q, "offset", 0, lo=0, hi=10_000_000) or 0)})
        if path == "/v1/admin/keys":
            org_id = (q.get("org") or [None])[0]
            if not org_id or not db.get_org(conn, org_id):
                return self._json(404, {"error": "unknown org"})
            keys = [{"id": k["id"], "name": k["name"], "prefix": k["prefix"],
                     "created_at": k["created_at"], "last_used_at": k["last_used_at"],
                     "revoked_at": k["revoked_at"]}
                    for k in db.list_api_keys(conn, org_id, include_revoked=True)]
            return self._json(200, {"org_id": org_id, "keys": keys})
        if path == "/v1/admin/verify":
            # #108: walk the usage-event tamper-evidence chain. 200 with the
            # per-org report; ``ok`` is false (but still HTTP 200) when a
            # divergence is found so a monitor can alert on the body.
            org_id = (q.get("org") or [None])[0]
            if org_id and not db.get_org(conn, org_id):
                return self._json(404, {"error": "unknown org"})
            report = db.verify_chain(
                conn, org_id=org_id, hmac_key=cfgmod.chain_hmac_key(self.ctx.cfg))
            return self._json(200, report)
        if path == "/v1/admin/ingest-health":
            # #150: ingestion health diagnostics across all orgs.
            org_id = (q.get("org") or [None])[0]
            if org_id and not db.get_org(conn, org_id):
                return self._json(404, {"error": "unknown org"})
            if org_id:
                return self._json(200, {
                    "org_id": org_id,
                    "sources": db.get_ingest_health(conn, org_id),
                })
            return self._json(200, {"sources": db.all_ingest_health(conn)})
        return self._json(404, {"error": f"no admin route for {path}"})

    def _admin_post(self, conn, path):
        if not self._admin_gate():
            return
        try:
            payload = json.loads(self._body() or b"{}")
        except Exception:
            return self._json(400, {"error": "body must be JSON"})
        if not isinstance(payload, dict):
            return self._json(400, {"error": "body must be a JSON object"})

        if path == "/v1/admin/orgs":
            name = (payload.get("name") or "").strip()
            if not name:
                return self._json(400, {"error": "name is required"})
            tier = (payload.get("tier") or "free").strip().lower()
            if tier not in ("free", "pro", "team", "enterprise"):
                return self._json(400, {"error": "tier must be free|pro|team|enterprise"})
            org = db.create_org(conn, name, tier=tier)
            return self._json(201, {"id": org["id"], "name": org["name"],
                                    "slug": org["slug"], "tier": org["tier"]})

        if path == "/v1/admin/credits":
            org_id = payload.get("org_id")
            if not org_id or not db.get_org(conn, org_id):
                return self._json(404, {"error": "unknown org"})
            kind = (payload.get("kind") or "grant").strip().lower()
            if kind not in ("grant", "adjust"):
                return self._json(400, {"error": "kind must be grant|adjust"})
            try:
                amount = float(payload.get("amount_usd"))
            except (TypeError, ValueError):
                return self._json(400, {"error": "amount_usd must be a number"})
            if not math.isfinite(amount) or amount == 0:
                return self._json(400, {"error": "amount_usd must be a non-zero finite number"})
            if kind == "grant" and amount < 0:
                return self._json(400, {"error": "a grant must be positive; use kind=adjust to debit"})
            row = db.add_ledger(conn, org_id, amount, kind,
                                reason=(payload.get("reason") or f"admin {kind}"))
            return self._json(201, {"org_id": org_id, "kind": kind,
                                    "amount_usd": amount,
                                    "balance_after": float(row["balance_after"])})

        if path in ("/v1/admin/keys", "/v1/admin/keys/rotate", "/v1/admin/keys/revoke"):
            return self._admin_keys_post(conn, path, payload)

        return self._json(404, {"error": f"no admin route for {path}"})

    def _admin_keys_post(self, conn, path, payload):
        """#150: admin key operations (create, rotate, revoke)."""
        org_id = payload.get("org_id")
        if not org_id or not db.get_org(conn, org_id):
            return self._json(404, {"error": "unknown org"})
        if path == "/v1/admin/keys/rotate":
            key_id = payload.get("key_id")
            if not key_id:
                return self._json(400, {"error": "key_id is required"})
            overlap = int(payload.get("overlap_seconds", 300))
            name = (payload.get("name") or "").strip() or None
            try:
                new_row, secret, old_row = db.rotate_api_key(
                    conn, org_id, key_id, overlap_seconds=overlap, name=name)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, {
                "new_key": {"id": new_row["id"], "prefix": new_row["prefix"]},
                "old_key": {"id": old_row["id"], "revoked_after_s": overlap},
                "secret": secret,
            })
        if path == "/v1/admin/keys/revoke":
            key_id = payload.get("key_id")
            if not key_id:
                return self._json(400, {"error": "key_id is required"})
            ok = db.revoke_api_key(conn, key_id, org_id)
            if not ok:
                return self._json(404, {"error": "key not found or already revoked"})
            return self._json(200, {"revoked": key_id})
        # Default: create a new key
        name = (payload.get("name") or "").strip() or None
        key_row, secret = db.create_api_key(conn, org_id, name=name)
        # The secret is returned ONCE and never recoverable afterward.
        return self._json(201, {"id": key_row["id"], "org_id": org_id,
                                "name": name, "secret": secret})

    # ---- API keys (session-gated; created from the dashboard) --------------
    def _users_create(self, conn):
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"), strict=True)
        email = (f.get("email") or "").strip()
        if not org_id or not email or "@" not in email:
            return self._json(400, {"error": "org and a valid email are required"})
        org = db.get_org(conn, org_id)
        limit = pricing.tier(org["tier"]).seats
        if limit is not None and db.active_seat_count(conn, org_id) >= limit:
            return self._json(409, {"error": "seat limit reached", "limit": limit})
        user = db.ensure_user(conn, org_id, email, (f.get("name") or "").strip() or None)
        if org["tier"] == "team" and self.ctx.stripe.available:
            self.ctx.stripe.sync_team_seats(conn, org_id)
        return self._json(201, {"user": dict(user),
                                "active_seats": db.active_seat_count(conn, org_id)})

    def _users_deactivate(self, conn):
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"), strict=True)
        user_id = (f.get("user_id") or "").strip()
        if not org_id or not user_id or not db.set_user_active(conn, user_id, org_id, False):
            return self._json(404, {"error": "user not found"})
        org = db.get_org(conn, org_id)
        if org["tier"] == "team" and self.ctx.stripe.available:
            self.ctx.stripe.sync_team_seats(conn, org_id)
        return self._json(200, {"user_id": user_id, "active": False,
                                "active_seats": db.active_seat_count(conn, org_id)})

    def _keys_create(self, conn):
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"), strict=True)
        if not org_id:
            return self._send(400, views.simple_page(
                "API keys", "No organization", "Create an org first.", ok=False))
        _, secret = db.create_api_key(conn, org_id, name=(f.get("name") or "").strip() or None)
        base = (self.ctx.cfg.get("auth", {}).get("base_url") or "").rstrip("/") \
            or f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"
        return self._send(200, views.api_key_created_page(secret, base))

    def _keys_revoke(self, conn):
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"), strict=True)
        if org_id and f.get("key_id"):
            db.revoke_api_key(conn, f.get("key_id"), org_id)
        return self._redirect("/")

    # ---- billing -----------------------------------------------------------
    def _subscriptions_enabled(self) -> bool:
        """#175: recurring-billing gate flag. Pro/Team subscription checkout
        stays off until the launch-readiness checks (#4/#164) pass; one-way
        money paths (credit top-ups, donations) are unaffected."""
        return bool((self.ctx.cfg.get("billing") or {}).get("subscriptions_enabled"))

    def _subscriptions_gated(self):
        """403 page returned while the #175 subscription gate is closed."""
        return self._send(403, views.simple_page(
            "Subscriptions", "Subscriptions open at launch",
            "Recurring Pro/Team billing is temporarily gated while the "
            "launch-readiness checks complete. Credit top-ups and donations "
            "are available now.", ok=False))

    def _checkout_credit(self, conn):
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"), strict=True)
        if not org_id:
            raise BillingError("no organization to bill")
        amount = _parse_credit_amount(f.get("amount"))
        sess = self.ctx.stripe.credit_checkout(conn, org_id, amount)
        return self._redirect(sess["url"])

    def _checkout_pro(self, conn):
        if not self._subscriptions_enabled():
            return self._subscriptions_gated()
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"), strict=True)
        if not org_id:
            raise BillingError("no organization to bill")
        sess = self.ctx.stripe.pro_checkout(conn, org_id)
        return self._redirect(sess["url"])

    def _checkout_team(self, conn):
        if not self._subscriptions_enabled():
            return self._subscriptions_gated()
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"), strict=True)
        if not org_id:
            raise BillingError("no organization to bill")
        sess = self.ctx.stripe.team_checkout(conn, org_id)
        return self._redirect(sess["url"])

    def _checkout_donate(self, conn):
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"), strict=True)
        if not org_id:
            raise BillingError("no organization to bill")
        amount = _parse_credit_amount(f.get("amount"))
        sess = self.ctx.stripe.donate_checkout(conn, org_id, amount)
        return self._send(200, views.checkout_handoff_page(sess["url"]))

    def _portal(self, conn):
        f = self._form()
        org_id = self._authz_org(conn, f.get("org"), strict=True)
        if not org_id:
            raise BillingError("no organization to bill")
        sess = self.ctx.stripe.portal(conn, org_id)
        return self._redirect(sess["url"])

    def _webhook(self, conn):
        payload = self._body()
        sig = self.headers.get("Stripe-Signature", "")
        try:
            event = self.ctx.stripe.construct_event(payload, sig)
        except Exception as e:  # signature/parse failure — log detail, don't echo it
            sys.stderr.write(f"plutus: webhook rejected: {e!r}\n")
            return self._json(400, {"error": "invalid webhook signature or payload"})
        result = handle_webhook_event(conn, event)
        # Log event id/type only — not the applied result (org_id/amount/balance).
        sys.stderr.write(
            f"plutus: stripe event id={event.get('id', '')} type={event.get('type', '')}\n")
        return self._json(200, {"received": True, "result": result})


def _guard_insecure_bind(host, auth_on, cfg):
    """Fail closed rather than expose an UNAUTHENTICATED dashboard on a non-loopback
    interface. The shipped Docker/compose default binds ``--host 0.0.0.0``, and auth
    is off-by-default and falls off on misconfig, so without this a plain deploy
    could publish an open billing console on the network. Bypass only with an
    explicit opt-in (localhost-proxy / trusted-network deployments)."""
    loopback = str(host) in ("127.0.0.1", "localhost", "::1", "")
    allow_insecure = bool(cfg.get("server", {}).get("allow_insecure")) or \
        os.environ.get("PLUTUS_ALLOW_INSECURE", "").strip().lower() in ("1", "true", "yes")
    if auth_on or loopback or allow_insecure:
        return
    raise SystemExit(
        f"plutus: refusing to serve on host {host!r} with authentication disabled; "
        "this would expose the billing dashboard and API to the network.\n"
        "  Do one of:\n"
        "   - enable auth (PLUTUS_AUTH_ENABLED=1 + Google client id/secret + an https base_url)\n"
        "   - bind 127.0.0.1 behind a TLS-terminating reverse proxy\n"
        "   - for a trusted network only, pass --allow-insecure (PLUTUS_ALLOW_INSECURE=1)")


def serve(host=None, port=None, db_path=None, demo=False, cfg=None,
          open_browser=False):
    """Start the dashboard/API server. Blocks until interrupted."""
    # Fix (#17): PyYAML is a hard dependency; fail fast rather than silently
    # degrading to the minimal config parser (which could reset a security config
    # such as allowed_emails to defaults when run from source without deps).
    import importlib.util
    if importlib.util.find_spec("yaml") is None:
        raise SystemExit("plutus: PyYAML is required to run the server "
                         "(pip install 'plutus-agent[all]').")
    cfg = cfg or cfgmod.load()
    host = host or cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(port or cfg.get("server", {}).get("port", 8420))
    db_path = db_path or str(cfgmod.db_path())

    # make sure schema exists
    c = db.connect(db_path)
    db.init_schema(c)
    c.close()

    ctx = _Ctx(cfg, db_path, demo=demo)
    _guard_insecure_bind(host, ctx.auth_on, cfg)
    httpd = _Server((host, port), Handler, ctx)
    url = f"http://{host}:{port}/"
    stripe_mode = ctx.stripe.status()["mode"]
    if ctx.auth_on:
        signup = " · open signup" if cfg.get("auth", {}).get("allow_signup") else " · allow-list"
        auth_mode = f"Google OIDC ({cfg['auth'].get('base_url') or 'base_url unset!'}){signup}"
    elif cfg.get("auth", {}).get("enabled"):
        auth_mode = "enabled but NOT configured → open (set Google client id/secret)"
    else:
        auth_mode = "open (no sign-in required)"
    sys.stderr.write(
        f"\n  ◆ Plutus v{__version__} — the billing layer for AI agents\n"
        f"    dashboard:  {url}\n"
        f"    stripe:     {stripe_mode}\n"
        f"    auth:       {auth_mode}\n"
        f"    database:   {db_path}{'  (demo data)' if demo else ''}\n"
        f"    webhook:    POST {url}webhook/stripe\n\n"
        f"  Ctrl-C to stop.\n\n"
    )
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n  plutus: stopped.\n")
    finally:
        httpd.server_close()
