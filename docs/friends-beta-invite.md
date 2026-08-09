# Friends beta — invite + onboarding

The warm-beta playbook: peers running their own agentic / multi-provider AI
stacks try Ledger. They see a big efficiency number (routing + local models +
subscriptions make it dramatic), they have the income to pay a fair savings-share
later, and nothing can surprise-bill them during the trial.

**Why it's risk-free (say this if they ask):** Ledger doesn't bill their LLM
spend — that's their own provider bill, which they already pay. Ledger only
*measures* it. The only money Ledger ever charges is the $20/mo floor and
savings-share, and **neither auto-charges** (`bill-savings` is a manual, per-org,
dry-run-by-default action; the prepaid hard-stop is off by default). A trial org
has zero surprise-bill risk. "Forgiving the debt" = just don't run bill-savings.

---

## The invite (copy/paste, edit the voice to yours)

> Hey — I built a thing I want you to try. You run [your stack] like I do:
> routing across providers, some local models, Claude Code on a subscription.
> You have no idea what that's actually worth vs. just paying the API for
> everything — I didn't either.
>
> Ledger meters it and shows you: **"$X of API-equivalent value for $Y actual —
> N× efficiency."** On my own stack it came out to **22×**. And the number is
> verifiable — it's on a tamper-evident chain you can recompute yourself, not a
> marketing slide.
>
> It's free for you to try, it just *measures* — it doesn't touch your provider
> bills or charge you anything. Takes 2 minutes: I send you a key, you drop in a
> Claude Code plugin, keep working, and you'll see your number by end of
> session. Want in?

---

## 2-minute onboarding (what you do per friend)

1. **Provision their org + key** on `ledger.perseus.observer`:
   ```
   ledger org create "<Friend Name>" --tier enterprise --email <their-email>
   # create an ingest key for the org; hand them the key + the dashboard link
   ```
   (Enterprise tier = no limits during the beta. Their Google email on the org
   lets them sign in and see their own dashboard once self-serve signup is live;
   until then, send them a weekly efficiency readout.)

2. **They install the Claude Code plugin** (once it's on the community
   marketplace):
   ```
   /plugin install ledger-metering@claude-community
   ```
   …then create `~/.claude/ledger_cc_config.json`:
   ```json
   { "remote": "https://ledger.perseus.observer", "api_key": "ledger_sk_<theirs>" }
   ```
   **Interim (before the plugin is published):** send them
   `integrations/claude-code-plugin/scripts/ledger-meter.py` plus the config
   above, and a one-line Stop hook in `~/.claude/settings.json`:
   ```json
   { "hooks": { "Stop": [ { "hooks": [
     { "type": "command", "command": "python \"~/.claude/ledger-meter.py\"" }
   ] } ] } }
   ```

3. **They keep working.** At session end the hook meters their usage.

4. **Show them the number:** `ledger efficiency --org "<Friend Name>"` →
   flagship-equivalent value vs. actual, and the multiple. That's the aha.

5. **When they're hooked:** turn on savings-share for their org
   (`bill-savings`) — or leave it off and just keep them on the free meter.
   Their call.

---

## What to watch / say

- **The number is the pitch.** Lead with *their* efficiency multiple, not the
  plumbing.
- **Verifiability is the moat.** "Recompute it yourself" beats "trust me."
- **Metadata only.** The Claude Code hook sends token counts, never transcript
  content (see the plugin's PRIVACY.md) — say so; it removes the obvious
  objection.
- **Don't oversell savings-share yet.** For most friends the win is the
  visibility; the paid share comes later, only where it's provable.
