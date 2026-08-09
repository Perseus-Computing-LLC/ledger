# NVFP4-vs-FP8 LLM-serving benchmark (#131)

Measures the **LLM-serving** cost/quality ratio of NVFP4 vs FP8 for a decoder
LLM, and produces the number that populates `pricing.quantization['nvfp4']` /
`PRECISION_MULTIPLIERS` (#128). This is the per-token *serving* lever — distinct
from the embedding-quantization question (perseus-vault#629, closed: no).

## Measured result — Llama-3.3-70B on 1× NVIDIA B200 (2026-07-15)

| | FP8 | NVFP4 |
|---|---|---|
| checkpoint | `RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic` (compressed-tensors) | `nvidia/Llama-3.3-70B-Instruct-FP4` (modelopt) |
| output throughput | 2,676 tok/s | **5,482 tok/s** |
| $/1M output tok @ $5.89/h | $0.61 | **$0.30** |
| tool-use selection (N=10, `tool_choice=auto`) | 10/10 | 10/10 |

**`nvfp4_multiplier = 0.49`** (NVFP4 ≈ 2.05× throughput → ~half the per-token cost)
· **tool-use retention = 1.0** (no quality loss observed). Adopted into
`ledger_agent/pricing.py` (`PRECISION_MULTIPLIERS['nvfp4']=0.49`) and the router
floor (`ledger_route.py`), since retention cleared the quality floor. Raw
artifacts in [`results/`](results/) (`results.json` + per-precision
`bench_*.json` / `quality_*.json`).

> **Caveats.** N=10 tool-use is a coarse quality probe (no degradation *observed*,
> not a guarantee across workloads). FP8 and NVFP4 use different quant recipes
> (compressed-tensors vs modelopt) because **nvidia's modelopt-FP8 checkpoint
> segfaulted in `flashinfer_bmm_fp8` during CUDA-graph capture** on this SGLang
> build (0.5.15) — a different FP8 recipe was substituted; both are standard
> production servings of the same base model. Re-run to refresh.

## How it was run (reproduce)

On a Blackwell pod (RunPod 1× B200, ~$5.89/h; needs CUDA ≥12.8 / torch ≥2.7):

```bash
pip install "sglang[all]"                       # 0.5.15 here (Blackwell FP4: sgl-project/sglang#26083)
# gotcha: the CUDA-13 pip libs aren't on the loader path — deep_gemm needs libnvrtc.so.13:
export LD_LIBRARY_PATH="$(python -c 'import nvidia,os,glob;print(":".join(sorted(glob.glob(os.path.dirname(nvidia.__file__)+"/*/lib"))))')"
export HF_HOME=/root/hf                          # authenticate HF (Llama-3.3-70B is gated)

# bench_one.sh serves ONE (pre-quantized) checkpoint, runs sglang.bench_serving,
# scores tool-use, tears down. Run once per precision (sequential fits <200GB disk):
NUM_PROMPTS=500 IN=1024 OUTLEN=512 ./bench_one.sh fp8   RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic compressed-tensors
NUM_PROMPTS=500 IN=1024 OUTLEN=512 ./bench_one.sh nvfp4 nvidia/Llama-3.3-70B-Instruct-FP4          modelopt_fp4
```

`bench_one.sh` records `output_throughput` per precision; the multiplier is
`throughput_fp8 / throughput_nvfp4`. `tooluse_eval.py` scores the pinned set
(`prompts_tooluse.jsonl`) against the live endpoint with **`tool_choice=auto`**
(forced `required` collapses some stacks to one tool — see the module comment);
`retention = toolselect_acc_nvfp4 / toolselect_acc_fp8`.

> Serve NVFP4 and FP8 with `--tool-call-parser llama3` (else `tool_calls` aren't
> parsed). `run_bench.sh` is the earlier online-quant sketch; the pre-quantized
> two-checkpoint path above is what produced the committed result.

### Why the multiplier is hardware-price-independent
Both precisions run on the same pod, so $/h cancels: the multiplier is purely
`throughput_fp8 / throughput_nvfp4`. The $/h only sets the absolute $/1M figure
(`= ($/h)/(3600·tok_s)·1e6`).

## Adopt criterion (matches the router's quality floor)
Only write `nvfp4 = throughput_fp8 / throughput_nvfp4` into
`ledger_agent/pricing.py` **if** tool-use retention ≥ the `quantization-aware`
policy's quality floor (`ledger_route.py`). Otherwise leave `nvfp4 = 1.0`
(identity no-op) and record the negative result here. **Never** fill the
multiplier from a vendor-published ratio — measured only.

## Output artifact
Commit the resulting `results.json` + `quality_*.json` alongside this README so
the multiplier in `pricing.py` is traceable to a signed measurement (the Ledger
verifiability contract).
