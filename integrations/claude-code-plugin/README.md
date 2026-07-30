# Perseus Ledger integration for Claude Code

This optional integration records Claude Code token-usage metadata in your
self-hosted [Perseus Ledger](https://perseus.observer/ledger/) instance at
session end. Ledger remains runtime-neutral: this is one integration, not a
requirement for using Ledger.

The stable plugin/package/configuration identifiers retain their `plutus*` names
during the transition. The integration records metadata only—never transcript
content. See [PRIVACY.md](PRIVACY.md).

**It sends token metadata only** — model, token counts, workspace, cost estimate.
**Never transcript content.** See [PRIVACY.md](PRIVACY.md).

## What you need

- A running Perseus Ledger instance (self-host with `pip install plutus-agent && plutus serve`, or run the Docker image). Its legacy-compatible `/v1/usage` endpoint records the event.
- An ingest API key for the organization you want Claude Code usage attributed to (`plutus keys create`, or the dashboard).
- `python` (3.8+) on your PATH — the hook is stdlib-only, nothing to install.

## Install

```
/plugin install plutus-metering@claude-community
```

Then create a config file at `~/.claude/plutus_cc_config.json`:

```json
{
  "remote": "https://plutus.your-host.example",
  "api_key": "plutus_sk_..."
}
```

(Or set the env vars `PLUTUS_REMOTE_URL` and `PLUTUS_CC_API_KEY` instead — env
wins over the file.)

That's it. Keep working. At the end of each turn the hook records new usage in
your Ledger dashboard. The `plutus efficiency` command remains available as a
legacy-compatible allocation analysis command.

## How it works

Claude Code fires a `Stop` hook at the end of every turn and hands it the
session's `transcript_path`. The hook reads the *new* assistant messages since it
last ran (watermarked per transcript, so a turn is never double-counted), sums
their token usage (input / output / cache-read), and POSTs the batch to your
Ledger `/v1/usage` endpoint with an idempotency key. It is fail-safe: any error
is non-fatal and never interrupts Claude Code.

- Model attribution: `provider=anthropic`, `model` taken from each message.
- Workspace: the basename of the turn's working directory, so spend is attributed
  per project automatically.
- No baseline/savings is sent — Claude Code runs the flagship, so there's no
  routing saving to claim; this is pure usage tracking. Your *subscription-vs-API*
  efficiency is computed on the Plutus side.

## Notes

- `python` must resolve to Python 3 on your PATH. On systems where only `python3`
  exists, symlink `python` or set an alias for the hook environment.
- The hook only meters usage; it never reads or transmits the content of your
  messages or Claude's responses.
- Uninstall: `/plugin uninstall plutus-metering` and delete
  `~/.claude/plutus_cc_config.json`.

## Links

- Plutus: https://github.com/Perseus-Computing-LLC/plutus
- Self-host / API: see the Plutus README and `docs/api.md`
