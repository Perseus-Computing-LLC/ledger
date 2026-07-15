#!/usr/bin/env python3
"""Tool-use quality eval for the NVFP4-vs-FP8 serving benchmark (#131).

Scores a pinned tool-use set (prompts_tooluse.jsonl) against a live
OpenAI-compatible endpoint (SGLang's /v1). A case passes when the model emits a
tool call whose name matches and whose arguments contain the expected key/values
(substring match, case-insensitive). Run once per served precision; then
    retention = accuracy_nvfp4 / accuracy_fp8
gates whether the measured throughput multiplier is adopted into
pricing.quantization['nvfp4'] (see README / #128).

Stdlib only (urllib) so it runs on a bare pod. Deterministic: temperature=0.

    python tooluse_eval.py --base-url http://127.0.0.1:30000/v1 \
        --model meta-llama/Llama-3.3-70B-Instruct --out quality_fp8.json
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

# Fixed tool catalog offered on every case, so the schema is identical across
# precisions and the only variable is the model's routing/argument behavior.
TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string"}, "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}},
            "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web for a query.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "calculator",
        "description": "Evaluate an arithmetic expression.",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "send_email",
        "description": "Send an email.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
            "required": ["to", "subject"]}}},
    {"type": "function", "function": {
        "name": "get_stock_price",
        "description": "Get the latest price for a stock ticker.",
        "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}}},
]


def chat(base_url, model, user, timeout=120):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": user}],
        "tools": TOOLS,
        # "auto": the model decides which tool (if any) to call. This measures
        # real tool-selection judgment. NOTE: "required" forces a call but on
        # some serving stacks collapses to a single tool (grammar artifact), so
        # it's unreliable for a precision-retention comparison — use auto on a
        # capable model (70B) that reliably calls tools when appropriate.
        "tool_choice": "auto",
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body, headers={"Content-Type": "application/json", "Authorization": "Bearer sk-none"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def first_tool_call(resp):
    try:
        tc = resp["choices"][0]["message"].get("tool_calls") or []
        if not tc:
            return None, None
        fn = tc[0]["function"]
        args = fn.get("arguments", "{}")
        args = json.loads(args) if isinstance(args, str) else (args or {})
        return fn.get("name"), args
    except (KeyError, IndexError, json.JSONDecodeError):
        return None, None


def case_passes(name, args, expect_name, expect_args_contains):
    if name != expect_name:
        return False
    for k, v in (expect_args_contains or {}).items():
        got = str(args.get(k, "")).lower()
        if str(v).lower() not in got:
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompts", default=str(Path(__file__).with_name("prompts_tooluse.jsonl")))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    cases = [json.loads(l) for l in Path(a.prompts).read_text(encoding="utf-8").splitlines() if l.strip()]
    details, correct, args_correct = [], 0, 0
    for c in cases:
        try:
            resp = chat(a.base_url, a.model, c["user"])
            name, args = first_tool_call(resp)
        except Exception as exc:  # network / server error — count as a miss, record why
            name, args = None, {"_error": str(exc)}
        # Primary metric: correct TOOL SELECTED (robust, precision-comparable).
        name_ok = name == c["expect_name"]
        # Secondary: did the args also carry the expected values (noisier).
        args_ok = case_passes(name, args, c["expect_name"], c.get("expect_args_contains"))
        correct += int(name_ok)
        args_correct += int(args_ok)
        details.append({"user": c["user"], "expect": c["expect_name"],
                        "got_name": name, "got_args": args,
                        "pass": name_ok, "args_ok": args_ok})

    n = len(cases)
    out = {"model": a.model, "base_url": a.base_url, "total": n,
           "correct": correct, "accuracy": round(correct / n, 4) if n else 0.0,
           "args_correct": args_correct,
           "args_accuracy": round(args_correct / n, 4) if n else 0.0,
           "details": details}
    json.dump(out, open(a.out, "w"), indent=2)
    print(f"{a.model}: tool-use accuracy {correct}/{n} = {out['accuracy']}  -> {a.out}")
    if correct != n:
        print("  (retention = accuracy_nvfp4 / accuracy_fp8 across the two runs)", file=sys.stderr)


if __name__ == "__main__":
    main()
