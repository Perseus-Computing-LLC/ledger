# Quantization in the cost model

Serving a model at lower numeric precision — fp8, NVFP4, int8, int4, 1-bit —
lowers the per-token *inference* cost. As 4-bit serving becomes the default on
Blackwell/Rubin-class hardware, a cost model that can't see precision will
misprice a growing share of real spend. This is issue
[#128](https://github.com/Perseus-Computing-LLC/plutus/issues/128).

Plutus models precision as a **multiplier** on the resolved per-token cost for
router/estimator decisions:

```
cost_at_precision = base_cost × precision_multiplier
```

These multipliers are **not ledger inputs**. `record_usage` and `/v1/usage` do
not accept a quantization tier, so billed usage remains based on the provider's
reported tokens and exact cost. A multiplier must not be presented as a
customer's measured spend or savings until an end-to-end serving-cost benchmark
supports it.

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
ratios (INT8 is already the Vault's shipped default embedding path).

| Tier  | Multiplier | Source |
|-------|-----------|--------|
| int8  | 1.00      | Vault shipped default (baseline) |
| 1bit  | 0.05      | **Assumed**, based on 32× memory reduction; Vault#630 measures quality, not serving cost |
| fp16  | 1.20      | perseus-vault#630 row 2: equivalent quality to INT8 (0.989 cosine), larger model (86MB vs 23MB) |
| fp8   | 1.00      | Uncalibrated |
| nvfp4 | 0.49      | Plutus#131: measured B200 throughput ratio (FP8/NVFP4) |
| int4  | 1.00      | Uncalibrated |

**1-bit benchmark details (2026-07-15):** 24-memory paraphrase-heavy recall
dataset, all-MiniLM-L6-v2 (384-dim quantized ONNX), pure sign-quantized Hamming
ranking vs Vault dense (cosine). Measured on CPU.

| Metric    | 1-bit pure | Full dense | Ratio |
|-----------|-----------|------------|-------|
| recall@1  | 83.3%     | 91.7%      | 0.908 |
| recall@3  | 91.7%     | 95.8%      | 0.957 |
| recall@5  | 100.0%    | 100.0%     | 1.000 |
| MRR       | 0.894     | 0.948      | 0.943 |

Memory: 48 bytes/signature vs 1,536 bytes/f32 embedding = 32× reduction.

**FP32 vs INT8 benchmark details (2026-07-15):** 2,000-entity synthetic corpus,
batch-embedded with both models. Direct embedding quality comparison.

| Metric | Value |
|--------|-------|
| Mean cosine (same text) | 0.989 |
| Top-1 NN agreement | 71% |
| Top-100 NN agreement | 85% |
| Mean Spearman's ρ | 0.982 |
| Model size | INT8: 23MB, FP32: 86MB (3.7×) |

INT8 and FP32/FP16 embeddings are practically indistinguishable for retrieval.
INT8 is both the quality and efficiency winner. fp16 multiplier is set to 1.2
to reflect the larger model footprint.

Drop measured ratios into config without a code change:

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
router/estimator multipliers. These values do not alter ledger debits.

## Quantization-aware routing

`plutus_route.py` includes a `quantization-aware` policy (#128 step 3) that
applies precision multipliers to model costs and re-ranks providers by effective
cost (`base_cost × multiplier`). A configurable quality floor
(`quality_min_retention`, default 0.90) filters out quantization tiers whose
measured quality retention falls below the threshold.

```bash
# Route with quantization-aware cost optimization
python plutus_route.py --policy quantization-aware --dry-run

# Stack with other policies:
python plutus_route.py --policy "quantization-aware,cost-prefer-cheapest" --dry-run
```

Configuration in `plutus.budgets.json`:

```json
{
  "routing": {
    "policy": "quantization-aware",
    "quality_min_retention": 0.90
  }
}
```

Per-model quantization availability is in `MODEL_QUANTIZATION_TIERS` (currently
fp16/fp8 for all models). As providers deploy NVFP4 on Blackwell/Rubin hardware,
add tiers to this table and populate the corresponding precision multipliers.

All policy decisions are logged to `plutus.routing.jsonl` with the effective
cost computation visible in the policy notes.
