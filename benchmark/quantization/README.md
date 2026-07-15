# NVFP4-vs-FP8 LLM-serving benchmark (#131)

Measures the **LLM-serving** cost/quality ratio of NVFP4 vs FP8 for a decoder
LLM, and produces the number that populates `pricing.quantization['nvfp4']` /
`PRECISION_MULTIPLIERS` (#128). This is the per-token *serving* lever — distinct
from the embedding-quantization question (perseus-vault#629, closed: no).

**Target:** `meta-llama/Llama-3.3-70B-Instruct`, tool-use quality eval.
**Vehicle:** one NVIDIA **Blackwell** GPU (native FP4) — e.g. RunPod 1× B200
(192 GB), ≈ $6/h. Fits at both FP8 (~70 GB) and NVFP4 (~35 GB).

> ⚠️ Authored without a Blackwell GPU on hand — treat as a runbook + scaffold,
> not a green-tested harness. Verify the SGLang flags and `bench_serving` output
> field names against the installed version on first run (they drift across
> releases). Serve **the same base model** at both precisions so the ratio is a
> pure precision effect.

## Why the multiplier is hardware-price-independent
Both precisions run on the **same pod**, so $/h cancels:

```
nvfp4_multiplier = cost_nvfp4 / cost_fp8 = output_throughput_fp8 / output_throughput_nvfp4
```

The `$6/h` only feeds the absolute dashboard figure:

```
$ / 1M output tokens = ($/h) / (3600 · tok_per_s) · 1e6   (= 1666.7 / tok_per_s at $6/h)
```

## Run

```bash
# On the B200 pod:
export HF_TOKEN=...            # Llama-3.3-70B is gated — accept the license on HF first
pip install "sglang[all]"      # needs a build with Blackwell/FP4 support (sgl-project/sglang#26083)

cd benchmark/quantization
# 1) cheap smoke pass first (validates the pipeline on a small model, ~minutes)
MODEL=meta-llama/Llama-3.1-8B-Instruct ./run_bench.sh
# 2) the real run
MODEL=meta-llama/Llama-3.3-70B-Instruct ./run_bench.sh

# 3) quality retention on the tool-use set, per precision (server must be up):
#    launch each precision, then:
python tooluse_eval.py --base-url http://127.0.0.1:30000/v1 --model $MODEL --out quality_fp8.json
python tooluse_eval.py --base-url http://127.0.0.1:30000/v1 --model $MODEL --out quality_nvfp4.json
```

`run_bench.sh` serves each precision, runs `sglang.bench_serving`, records output
throughput, and writes `results.json` with the computed multiplier + $/1M-token
figures. `tooluse_eval.py` scores a pinned tool-use set (`prompts_tooluse.jsonl`)
against the live OpenAI-compatible endpoint; `retention = acc_nvfp4 / acc_fp8`.

## Adopt criterion (matches the router's quality floor)
Only write `nvfp4 = throughput_fp8 / throughput_nvfp4` into
`plutus_agent/pricing.py` **if** tool-use retention ≥ the `quantization-aware`
policy's quality floor (`plutus_route.py`). Otherwise leave `nvfp4 = 1.0`
(identity no-op) and record the negative result here. **Never** fill the
multiplier from a vendor-published ratio — measured only.

## Output artifact
Commit the resulting `results.json` + `quality_*.json` alongside this README so
the multiplier in `pricing.py` is traceable to a signed measurement (the Plutus
verifiability contract).
