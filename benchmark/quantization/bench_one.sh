#!/usr/bin/env bash
# One precision cycle: serve a (pre-quantized) checkpoint, bench throughput,
# score tool-use, tear down. Emits $OUT/bench_<label>.json + quality_<label>.json.
set -uo pipefail
LABEL="$1"; MODEL="$2"; QUANT="$3"; PORT="${4:-30000}"
export HF_HOME=/root/hf
# pip-installed CUDA-13 libs (from the torch reinstall) live under
# site-packages/nvidia/*/lib and aren't on the default loader path — deep_gemm
# needs libnvrtc.so.13 from there. Put them on LD_LIBRARY_PATH.
export LD_LIBRARY_PATH="$(python -c 'import nvidia,os,glob;print(":".join(sorted(glob.glob(os.path.dirname(nvidia.__file__)+"/*/lib"))))' 2>/dev/null):${LD_LIBRARY_PATH:-}"
OUT=/root/results; mkdir -p "$OUT"
NUM_PROMPTS="${NUM_PROMPTS:-200}"; IN="${IN:-1024}"; OUTLEN="${OUTLEN:-512}"
READY_MAX="${READY_MAX:-360}"   # 15s * 360 = 90 min ceiling incl. download+load

echo "[serve] $LABEL model=$MODEL quant=$QUANT $(date -u +%H:%M:%S)"
pkill -f sglang.launch_server 2>/dev/null; sleep 3
python -m sglang.launch_server --model "$MODEL" --quantization "$QUANT" \
  --host 127.0.0.1 --port "$PORT" --tp 1 --mem-fraction-static 0.85 \
  --tool-call-parser "${TOOL_PARSER:-llama3}" \
  > "$OUT/serve_$LABEL.log" 2>&1 &
SPID=$!
ready=0
for i in $(seq 1 "$READY_MAX"); do
  if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ready=1; break; fi
  if ! kill -0 "$SPID" 2>/dev/null; then echo "[serve] process exited early"; break; fi
  sleep 15
done
if [ "$ready" != 1 ]; then
  echo "[FAIL] $LABEL server not ready"; tail -40 "$OUT/serve_$LABEL.log"; kill "$SPID" 2>/dev/null; exit 1
fi
echo "[bench] $LABEL $(date -u +%H:%M:%S)"
rm -f "$OUT/bench_$LABEL.json"   # bench_serving APPENDS JSONL — keep one record
python -m sglang.bench_serving --backend sglang --host 127.0.0.1 --port "$PORT" \
  --model "$MODEL" --dataset-name random --num-prompts "$NUM_PROMPTS" \
  --random-input "$IN" --random-output "$OUTLEN" --random-range-ratio 1.0 \
  --output-file "$OUT/bench_$LABEL.json" > "$OUT/bench_$LABEL.txt" 2>&1 \
  || { echo "[warn] bench_serving rc=$?"; tail -20 "$OUT/bench_$LABEL.txt"; }
echo "[quality] $LABEL"
python /root/harness/benchmark/quantization/tooluse_eval.py \
  --base-url "http://127.0.0.1:$PORT/v1" --model "$MODEL" \
  --out "$OUT/quality_$LABEL.json" >> "$OUT/bench_$LABEL.txt" 2>&1 \
  || echo "[warn] tooluse_eval rc=$?"
kill "$SPID" 2>/dev/null; sleep 5; pkill -f sglang.launch_server 2>/dev/null
echo "[done] $LABEL $(date -u +%H:%M:%S)"
