"""Quickstart — meter a few calls and read the balance. Runs fully offline.

    python examples/quickstart.py

Then `ledger serve` (or `ledger demo`) to see it on the dashboard.
"""
from ledger_agent import Meter

# Open (or create) an org-scoped meter. Uses ~/.ledger/ledger.db by default;
# point elsewhere with db_path=... to keep this example self-contained.
ledger = Meter(org="Quickstart Co", tier="pro", db_path="./quickstart.db")

# Give the org some prepaid credit (what a Stripe top-up would do).
ledger.topup(25.00, reason="example seed")

# Meter some agent calls. Cost is estimated from tokens unless you pass cost_usd.
ledger.track(provider="anthropic", model="claude-opus-4-8",
             task_type="code_review", workspace="ci",
             input_tokens=8200, output_tokens=2400, reasoning_tokens=900)

ledger.track(provider="google", model="gemini-2.5-flash",
             task_type="summarize", workspace="docs",
             input_tokens=12000, output_tokens=600)

r = ledger.track(provider="deepseek", model="deepseek-v4-flash",
                 task_type="classify", workspace="ci",
                 input_tokens=900, output_tokens=40)

print(f"last call cost: ${r.cost_usd:.6f} ({'estimated' if r.estimated else 'exact'})")
print(f"credit balance: ${ledger.balance():.4f}")

s = ledger.summary()
print("\nspend by provider:")
for p in s["by_provider"]:
    print(f"  {p['key']:<12} ${p['cost']:.4f}  ({p['events']} calls)")

ledger.close()
print("\nNow run:  ledger serve   (point LEDGER_DB at ./quickstart.db to view this org)")
