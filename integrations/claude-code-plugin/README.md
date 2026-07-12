# Plutus metering for Claude Code

See what your Claude Code usage is really worth. This plugin meters each turn's
token usage to your self-hosted [Plutus](https://perseus.observer) ledger at
session end, so your Claude Code spend shows up on the same dashboard as the rest
of your AI stack — with the **efficiency view**: API-equivalent value vs. what you
actually paid (Claude Code on a subscription is typically several× cheaper than
the raw API — this shows you by how much).

**It sends token metadata only** — model, token counts, workspace, cost estimate.
**Never transcript content.** See [PRIVACY.md](PRIVACY.md).

## What you need

- A running Plutus instance (self-host in ~10 min: `pip install plutus-agent && plutus serve`, or run the Docker image). Its `/v1/usage` endpoint is where usage is metered.
- An ingest API key for the org you want Claude Code usage attributed to (`plutus keys create`, or the dashboard).
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

That's it. Keep working. At the end of each turn the hook meters the new usage;
open your Plutus dashboard to watch it land, and run `plutus efficiency` to see
your value-vs-cost multiple.

## How it works

Claude Code fires a `Stop` hook at the end of every turn and hands it the
session's `transcript_path`. The hook reads the *new* assistant messages since it
last ran (watermarked per transcript, so a turn is never double-counted), sums
their token usage (input / output / cache-read), and POSTs the batch to your
Plutus `/v1/usage` with an idempotency key. It is fail-safe: any error is
non-fatal and never interrupts Claude Code.

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
