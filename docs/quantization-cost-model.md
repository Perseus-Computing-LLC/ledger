# Quantization in the cost model

Serving a model at lower numeric precision — fp8, NVFP4, int8, int4, 1-bit —
lowers the per-token *inference* cost. As 4-bit serving becomes the default on
Blackwell/Rubin-class hardware, a cost model that can't see precision will
misprice a growing share of real spend. This is issue
[#128](https://github.com/Perseus-Computing-LLC/plutus/issues/128).

Plutus models precision as a **multiplier** on the resolved per-token cost:

```
cost_at_precision = base_cost × precision_multiplier
```

## The honest default: 1.0

Every recognized tier ships at a multiplier of **1.0 — no assumed savings**, and
any *unrecognized* precision string also resolves to 1.0. Quoting a quantization
tier on an uncalibrated deployment therefore changes nothing. This is deliberate:
Plutus's job is to *prove* savings, so it must never manufacture a discount from a
label. An uncalibrated install can under-report a real saving, but it can never
over-report one.

Vendor-published headline figures (e.g. "1.73× training speedup, ~2× memory")
are **not** encoded here. They describe different quantities under vendor-chosen
conditions and don't translate to a per-token serving-cost ratio for your
workload.

## Calibrating with measured numbers

Real multipliers come from *measured* quality/latency/cost artifacts. The source
of record is **perseus-vault#630**, which produces measured INT8 / 1-bit / NVFP4
ratios (INT8 is already the Vault's shipped default embedding path). Drop the
measured ratios into config — no code change:

```yaml
# ~/.plutus/config.yaml
pricing:
  quantization:
    nvfp4: 0.55   # measured: NVFP4 serving costs ~55% of the fp16/fp8 baseline
    int4: 0.50
```

Any tier you don't list stays at 1.0.

## API

```python
from plutus_agent import pricing

# Resolve a multiplier (honoring a measured-override map).
mult, known = pricing.resolve_precision_multiplier("nvfp4", {"nvfp4": 0.55})
# -> (0.55, True);  unknown/None -> (1.0, False)

# Price a call at a given precision.
cost = pricing.estimate_cost(
    "anthropic", "claude-opus-4-8", input_tokens, output_tokens,
    quantization="nvfp4",
    quantization_overrides={"nvfp4": 0.55},   # or pull from pricing.quantization
)
```

`QUANTIZATION_TIERS` is the recognized taxonomy; `PRECISION_MULTIPLIERS` holds
the (identity) defaults.

## Not yet done: quantization-aware routing

The issue's sketch also floats preferring a cheaper quantization in
`plutus_route.py` when the quality delta is acceptable. That is a **follow-up**,
intentionally not built here: it needs (1) per-model quantization metadata the
router doesn't yet carry and (2) the measured multipliers above. Wiring it before
those exist would mean routing on guessed ratios — exactly what the 1.0 default
is there to prevent.
