# Privacy — Perseus Ledger integration for Claude Code

This plugin is an optional self-hosted Perseus Ledger integration. It is
**separate from Anthropic** and is not endorsed, reviewed, or operated by
Anthropic. Your Ledger instance is your responsibility; Anthropic does not host,
monitor, or have access to it. Legacy `plutus*` identifiers remain in the
integration configuration during the transition.

## What is sent, and where

At the end of each Claude Code turn, the plugin sends **token usage metadata** to
the Plutus instance **you configure** (`remote` in `~/.claude/plutus_cc_config.json`
or `PLUTUS_REMOTE_URL`):

- the model name (e.g. `claude-opus-4-8`)
- token counts: input, output, cache-read
- the working-directory basename, used as a workspace label
- `provider: anthropic`, `source: claude-code`, `task_type: coding`

That is the complete list. The data is POSTed to your instance's `/v1/usage`
endpoint, authenticated with your ingest API key.

## What is NOT sent

- **No transcript content.** The text of your prompts and Claude's responses is
  never read for transmission and never sent. The hook parses the transcript only
  to sum token *counts* from the usage fields; message bodies are not transmitted.
- No file contents, no code, no environment variables, no credentials.

## Retention and use

Retention and access are governed entirely by **your** Plutus deployment and
your own privacy policy — not by this plugin and not by Anthropic. The plugin
does not send data anywhere except the instance you point it at. It does not
train on, resell, or share your data.

## Consent

Installing this plugin and configuring an instance URL constitutes your consent
to send the above metadata to that instance at session end. Remove the plugin
(`/plugin uninstall plutus-metering`) and delete the config file to stop.
