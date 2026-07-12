"""Invarium → Plutus bridge: accuracy-gated savings metering.

`Invarium <https://github.com/invarium-ai/invarium>`_ ("pytest for AI agents")
produces one ``AgentResult`` per task/question — carrying ``cost``, ``latency``,
and a free-form ``metadata`` dict — plus a pass/fail verdict from its behavioral
assertions (``used_tools_in_order``, step budgets, "didn't-claim-success-without-
the-tool", …). Plutus meters spend per call and books *savings* only when a
counterfactual ``baseline_cost_usd`` is supplied.

This bridge makes the savings figure **accuracy-gated**: the counterfactual
baseline is forwarded to Plutus *only when the task passed its Invarium
contract*. A cheaper-but-wrong run therefore books **$0** savings.

Why this needs no schema change: Plutus already treats a withheld (NULL) baseline
as "never contributes to billable savings" (the conservative default in
``metering.record_usage`` / ``savings.period_savings``). Accuracy-gating is simply
the caller declining to pass a baseline for an unverified task. See
``docs/invarium-integration.md`` for the full design (per-task attribution and the
meter-accuracy regression suite).

The result object is duck-typed — anything exposing ``cost: float | None`` and
``metadata: dict`` works — so ``plutus_agent`` takes **no** dependency on invarium.
"""
from __future__ import annotations

from typing import Any, Optional

from .. import metering

# Metadata keys the upstream harness (e.g. Perseus) injects onto the AgentResult.
BASELINE_KEY = "baseline_cost_usd"      # explicit counterfactual USD for this task
BASELINE_MODEL_KEY = "baseline_model"   # or a model name Plutus prices itself
TASK_ID_KEY = "task_id"                 # per-question id (see external_ref in the design doc)


def meter_agent_result(
    conn,
    org_id: str,
    result: Any,
    *,
    verified: bool,
    provider: str,
    model: Optional[str] = None,
    task_type: str = "general",
    workspace: Optional[str] = None,
    source: str = "invarium",
    **record_kwargs: Any,
) -> metering.MeterResult:
    """Meter one Invarium ``AgentResult``, gating the savings baseline on ``verified``.

    ``verified`` is the outcome of the task's Invarium behavioral contract — i.e.
    ``expect(result)....verify()`` passed *and* any cost/latency assertion held.
    When it is ``False`` the counterfactual baseline is withheld, so the event
    contributes $0 to billable savings even if its actual cost undercut the
    baseline. When ``True`` the baseline (an explicit USD figure via
    ``metadata['baseline_cost_usd']`` or a model name via
    ``metadata['baseline_model']``) is passed through to ``record_usage``.

    Returns the :class:`plutus_agent.metering.MeterResult` unchanged so callers
    can read ``event_id``, ``cost_usd``, ``baseline_usd`` and ``savings_usd``.
    """
    cost = getattr(result, "cost", None)
    if cost is None:
        raise ValueError(
            "AgentResult.cost is None — the Invarium adapter must populate a real "
            "per-task cost before metering (the whole point is per-task accuracy)."
        )

    metadata = getattr(result, "metadata", None) or {}
    baseline_cost_usd = metadata.get(BASELINE_KEY) if verified else None
    baseline_model = metadata.get(BASELINE_MODEL_KEY) if verified else None
    # Attribution is recorded for EVERY event regardless of the verdict — only the
    # savings baseline is gated. So a failed task still shows up under its task_id
    # (metered spend), it just books $0 savings. record_kwargs may override.
    record_kwargs.setdefault("external_ref", metadata.get(TASK_ID_KEY))

    return metering.record_usage(
        conn,
        org_id,
        provider=provider,
        model=model,
        task_type=task_type,
        workspace=workspace,
        cost_usd=float(cost),
        baseline_cost_usd=baseline_cost_usd,
        baseline_model=baseline_model,
        source=source,
        **record_kwargs,
    )
