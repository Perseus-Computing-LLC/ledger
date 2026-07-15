#!/usr/bin/env bash
# NVFP4-vs-FP8 serving throughput benchmark for a decoder LLM (#131).
# Serves the SAME model at each precision on one GPU, runs sglang.bench_serving,
# and computes nvfp4_multiplier = output_throughput_fp8 / output_throughput_nvfp4
# (hardware-price-independent — same pod, $/h cancels).
#
# UNTESTED on Blackwell — verify SGLang flags + bench_serving output field names
# on first run. Requires: a B200 (or other native-FP4 Blackwell GPU), sglang with
# FP4 support, HF_TOKEN for gated models, jq, curl.
set -euo pipefail

MODEL="${MODEL:-meta-llama/Llama-3.3-70B-Instruct}"
PRECISIONS="${PRECISIONS:-fp8 nvfp4}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-30000}"
NUM_PROMPTS="${NUM_PROMPTS:-500}"
INPUT_LEN="${INPUT_LEN:-1024}"
OUTPUT_LEN="${OUTPUT_LEN:-512}"
COST_PER_HOUR="${COST_PER_HOUR:-6.0}"   # RunPod B200 ~$6/h; only affects $/1M, not the multiplier
OUTDIR="${OUTDIR:-./results}"
READY_TIMEOUT="${READY_TIMEOUT:-1800}"  # 70B weight load can be slow
mkdir -p "$OUTDIR"

# SGLang's --quantization value differs per precision; nvfp4 uses online quant.
sglang_quant_flag() {
  case "$1" in
    fp8)   echo "fp8" ;;
    nvfp4) echo "nvfp4_online" ;;   # fall back to a ModelOpt NVFP4 checkpoint if unsupported
    *)     echo "$1" ;;
  esac
}

wait_ready() {
  local deadline=$(( SECONDS + READY_TIMEOUT ))
  until curl -sf "http://${HOST}:${PORT}/health" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then echo "server did not become ready in ${READY_TIMEOUT}s" >&2; return 1; fi
    sleep 5
  done
}

declare -A TPUT
for prec in $PRECISIONS; do
  q="$(sglang_quant_flag "$prec")"
  echo "=== serving $MODEL @ $prec (--quantization $q) ==="
  python -m sglang.launch_server --model "$MODEL" --quantization "$q" \
    --host "$HOST" --port "$PORT" --tp 1 >"$OUTDIR/server_${prec}.log" 2>&1 &
  SERVER_PID=$!
  trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
  wait_ready

  echo "=== bench_serving @ $prec ==="
  python -m sglang.bench_serving --backend sglang --host "$HOST" --port "$PORT" \
    --model "$MODEL" --dataset-name random --num-prompts "$NUM_PROMPTS" \
    --random-input "$INPUT_LEN" --random-output "$OUTPUT_LEN" --request-rate inf \
    --output-file "$OUTDIR/bench_${prec}.json" 2>&1 | tee "$OUTDIR/bench_${prec}.txt"

  # bench_serving output field is typically "output_throughput" (tok/s). Verify.
  t="$(jq -r '.output_throughput // .output_token_throughput // empty' "$OUTDIR/bench_${prec}.json" 2>/dev/null || true)"
  if [[ -z "$t" ]]; then
    t="$(grep -oE 'Output token throughput[^0-9]*[0-9.]+' "$OUTDIR/bench_${prec}.txt" | grep -oE '[0-9.]+' | tail -1 || true)"
  fi
  [[ -z "$t" ]] && { echo "could not parse output throughput for $prec — check $OUTDIR/bench_${prec}.*" >&2; exit 1; }
  TPUT[$prec]="$t"
  echo "$prec output_throughput = $t tok/s"

  kill $SERVER_PID 2>/dev/null || true; wait $SERVER_PID 2>/dev/null || true
  trap - EXIT
done

python - "$COST_PER_HOUR" "${TPUT[fp8]:-}" "${TPUT[nvfp4]:-}" "$MODEL" "$OUTDIR" <<'PY'
import json, sys
cost_h, fp8, nvfp4, model, outdir = sys.argv[1:6]
cost_h = float(cost_h); fp8 = float(fp8); nvfp4 = float(nvfp4)
def per_million(tps): return round((cost_h/3600.0)/tps*1e6, 4)
mult = round(fp8/nvfp4, 4)   # cost_nvfp4/cost_fp8 = tput_fp8/tput_nvfp4
out = {
  "model": model,
  "cost_per_hour_usd": cost_h,
  "fp8":   {"output_tok_s": fp8,   "usd_per_1M_out": per_million(fp8)},
  "nvfp4": {"output_tok_s": nvfp4, "usd_per_1M_out": per_million(nvfp4)},
  "nvfp4_multiplier": mult,
  "note": "multiplier = tput_fp8/tput_nvfp4 (hardware-price-independent). Adopt into pricing.quantization['nvfp4'] ONLY if tool-use retention >= router quality floor (see tooluse_eval.py).",
}
p = f"{outdir}/results.json"
json.dump(out, open(p, "w"), indent=2)
print(json.dumps(out, indent=2)); print("wrote", p)
PY
