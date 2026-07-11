#!/usr/bin/env python3
"""Produce a REAL Hermes state.db from a multi-model agent workload.

Runs genuine Ollama inference (two locally-served models on the A10), doing real
mid-session model switches, and records each call through Hermes' OWN accounting
code (`hermes_state.SessionDB.update_token_counts` -> `_record_model_usage`,
schema v17). So `sessions` keeps each session's INITIAL (model, provider) with the
cumulative cost, while `session_model_usage` splits per live model -- exactly the
data #97 consumes.

Token counts and costs come from real inference (Ollama's prompt_eval_count /
eval_count). The two model backends are labelled as distinct billing_providers to
mirror a multi-provider route; that labelling is the only assigned element, and
it is disclosed in the exhibit writeup.
"""
import os
import sys
import json
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/hermes_kit"))
from hermes_state import SessionDB  # real Hermes v17 accounting

OLLAMA = "http://127.0.0.1:11434/api/chat"

# (model, billing_provider label, illustrative $/token). Tokens are REAL.
MODELS = [
    ("qwen2.5:1.5b", "ollama:qwen", 0.20e-6),
    ("llama3.2:1b",  "ollama:llama", 0.50e-6),
]

PROMPTS = [
    "In one sentence, what is a binary search tree?",
    "Give a short Python example of inserting into it.",
    "Now explain the average-case time complexity briefly.",
    "Summarize when NOT to use one, in one sentence.",
    "Name one self-balancing variant and why it helps.",
    "Write a one-line analogy a child would understand.",
]


def chat(model, prompt):
    body = json.dumps({"model": model, "stream": False,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode())
    return int(d.get("prompt_eval_count", 0) or 0), int(d.get("eval_count", 0) or 0)


def main():
    n_sessions = int(os.environ.get("N_SESSIONS", "12"))
    db_path = Path(os.path.expanduser("~/attrib_state.db"))
    if db_path.exists():
        db_path.unlink()
    st = SessionDB(db_path=db_path)
    print(f"schema_version={st._conn.execute('PRAGMA user_version').fetchone()[0]}")

    for s in range(n_sessions):
        sid = f"sess-{s:03d}"
        (m0, p0, price0) = MODELS[0]
        (m1, p1, price1) = MODELS[1]
        st.create_session(sid, "lambda-attrib-proof", model=m0)
        # First half of the session runs on model0 / provider0 ...
        for prompt in PROMPTS[:3]:
            it, ot = chat(m0, prompt)
            st.update_token_counts(sid, input_tokens=it, output_tokens=ot, model=m0,
                                   billing_provider=p0, api_call_count=1,
                                   estimated_cost_usd=(it + ot) * price0)
        # ... then the user switches models mid-session -> model1 / provider1.
        for prompt in PROMPTS[3:6]:
            it, ot = chat(m1, prompt)
            st.update_token_counts(sid, input_tokens=it, output_tokens=ot, model=m1,
                                   billing_provider=p1, api_call_count=1,
                                   estimated_cost_usd=(it + ot) * price1)
        print(f"{sid}: done (switched {m0} -> {m1})")

    # quick integrity read
    cur = st._conn.execute("SELECT COUNT(*) FROM sessions")
    ns = cur.fetchone()[0]
    cur = st._conn.execute("SELECT COUNT(*) FROM session_model_usage")
    nm = cur.fetchone()[0]
    print(f"sessions={ns} session_model_usage_rows={nm} db={db_path}")


if __name__ == "__main__":
    main()
