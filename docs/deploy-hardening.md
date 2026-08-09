# Deploy hardening

Ledger handles live billing, so a real deployment must be locked down. The engine
internals are hardened (auth, tenant isolation, CSRF, atomic ledger); the items
below are the **operator's** responsibility. This checklist came out of the
2026-07-05 security review.

> **Fail-closed default:** the server **refuses to bind a non-loopback host
> (`0.0.0.0`, a LAN IP, …) with authentication disabled** and exits. That stops a
> plain `docker compose up` / `serve --host 0.0.0.0` from publishing an open
> billing console. Override only for a trusted network with `--allow-insecure`
> (`LEDGER_ALLOW_INSECURE=1`); the shipped **demo** image sets this because its
> data is disposable.

## Minimum safe production config

1. **Enable auth, fully configured.** Set `LEDGER_AUTH_ENABLED=1`,
   `LEDGER_GOOGLE_CLIENT_ID`, `LEDGER_GOOGLE_CLIENT_SECRET`, and
   `LEDGER_BASE_URL=https://<host>` (the `https://` is what turns on the `Secure`
   session cookie). Confirm the startup banner shows `auth: Google OIDC (...)`,
   **not** "open" or "enabled but NOT configured".
2. **Signup off, allow-list on.** Leave `allow_signup` false (default); set
   `LEDGER_ALLOWED_DOMAIN` (your corp domain) or `LEDGER_ALLOWED_EMAILS`. Never
   set `allow_unsigned_tokens`.
3. **Secrets via environment, never on disk.** Put `STRIPE_SECRET_KEY`,
   `STRIPE_WEBHOOK_SECRET`, `LEDGER_GOOGLE_CLIENT_SECRET`, `LEDGER_SMTP_PASSWORD`,
   `LEDGER_ADMIN_TOKEN` in the environment only, so they never land in
   `config.yaml` or its backups. (Config + `config.yaml.bak-*` are git/docker
   ignored, but env is cleaner.)
4. **TLS in front.** Run behind a TLS-terminating reverse proxy (Caddy/nginx);
   keep the app on `127.0.0.1` or an internal interface. Set
   `auth.trust_forwarded_for=true` only when actually behind that proxy.
5. **Admin API.** Set `LEDGER_ADMIN_TOKEN` to a long random value only if you need
   `/v1/admin`; otherwise leave it empty (the endpoint stays 404).
6. **SMTP.** Use port 465 (implicit TLS) or an authenticated STARTTLS relay; set
   `alerts.require_tls: true` to refuse sending over an unencrypted link.
7. **Reproducible artifact.** Deploy a hash-locked image/wheel; don't
   `pip install --upgrade` from `curl | bash` on the production host. Dependency
   upper bounds are pinned in `pyproject.toml`.
8. **Container.** The image already runs as a non-root user; mount `/data` as a
   persistent, access-restricted volume.
9. **Keep the prepaid hard-stop on.** Leave `pricing.block_over_balance` true so
   prepaid orgs can't overspend.

## Quick check

```bash
# Production: this MUST refuse to start (proves the fail-closed guard is active)
ledger serve --host 0.0.0.0            # -> exits with a "refusing to serve" error

# Correct production invocation:
LEDGER_AUTH_ENABLED=1 LEDGER_GOOGLE_CLIENT_ID=... LEDGER_GOOGLE_CLIENT_SECRET=... \
LEDGER_BASE_URL=https://ledger.internal ledger serve --host 127.0.0.1
```
