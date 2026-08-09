"""Hermes Agent → Ledger.

The original ``ledger.py`` monitor reads Hermes' ``state.db`` after the fact.
This shows the *push* path: meter each Hermes session into Ledger as it
completes, so spend shows up live on the dashboard and depletes prepaid credit.

Drop this into a Hermes post-session hook, or batch-import an existing
``state.db`` (the loop at the bottom).
"""
import os

from ledger_agent import Meter
from ledger_agent.hermes import read_spend_events
from ledger_agent.integrations import track_hermes_session

meter = Meter(org="Hermes", tier="pro")


def on_session_complete(session: dict):
    """Call this from a Hermes post-session hook with the session row/dict."""
    res = track_hermes_session(meter, session, workspace=session.get("workspace", "hermes"))
    if res.alerts:
        for a in res.alerts:
            print(f"[ledger] ALERT {a['kind']}: {a['message']}")
    return res


def backfill_from_state_db(state_db: str):
    """One-time import of historical Hermes sessions into Ledger.

    Reads via :func:`ledger_agent.hermes.read_spend_events`, so mid-session
    model switches are attributed to the provider that actually served each
    call (schema v17 ``session_model_usage``), with a graceful fallback to the
    aggregate ``sessions`` row for pre-v17 data. One meter event is recorded per
    ``(session, model, provider)`` instead of one lump per session.
    """
    events = read_spend_events(state_db)
    for ev in events:
        track_hermes_session(meter, ev)
    n_sessions = len({ev["session_id"] for ev in events})
    print(f"imported {n_sessions} sessions ({len(events)} model-events) "
          f"→ balance ${meter.balance():.2f}")


if __name__ == "__main__":
    state_db = os.environ.get(
        "LEDGER_STATE_DB",
        "/opt/data/webui/minions-hermes-config/state.db")
    if os.path.exists(state_db):
        backfill_from_state_db(state_db)
    else:
        # demo a single synthetic session
        on_session_complete({
            "billing_provider": "anthropic", "model": "claude-opus-4-8",
            "task_type": "code_review", "actual_cost_usd": 0.142,
            "input_tokens": 9100, "output_tokens": 2300,
        })
        print(f"balance ${meter.balance():.4f}")
    meter.close()
