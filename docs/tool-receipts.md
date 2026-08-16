# Tool-execution receipts and epistemic verification

Ledger's `ledger_agent.tool_receipts` module is an additive bridge for
NabaOS-style claim verification ([Basu, *Tool Receipts, Not Zero-Knowledge
Proofs*, arXiv:2603.10060](https://arxiv.org/abs/2603.10060)). It does not change
Ledger's existing evidence-receipt schemas. A tool adapter can issue one
small signed receipt per call, add `receipt_to_evidence_hash(receipt)` to an
existing `evidence_hashes` list, and verify response claims at the end of a
session.

## Receipt schema

`build_tool_receipt` returns a dictionary with schema
`perseus-ledger-tool-receipt/v1`. The field order below is also the builder's
insertion order (canonical hashing remains key-sorted):

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | string | Versioned schema identifier. |
| `id` | string | Caller-supplied identifier or `uuid4().hex`. |
| `tool_name` | string | Runtime tool/adapter name. |
| `input_hash` | 64-hex string | SHA-256 of compact canonical JSON for `input_params`. |
| `output_hash` | 64-hex string | SHA-256 of `raw_output` encoded as UTF-8. |
| `result_count` | non-negative integer | Number of results returned by the tool. |
| `facts` | object | Deterministically extracted ground-truth facts. |
| `timestamp_ms` | non-negative integer | Runtime issue time, in milliseconds. |
| `duration_ms` | non-negative integer | Tool execution duration. |
| `key_id` | non-empty string | Declared key-registry identifier. |
| `signature` | 64-hex string | HMAC-SHA256 signature. |

Canonical JSON uses `json.dumps(value, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)`. The signature payload is **not JSON**; it is the UTF-8
encoding of:

```text
id|tool_name|input_hash|output_hash|result_count|canonical(facts)|timestamp_ms
```

The paper's illustrative payload ends at `timestamp_ms`. Ledger deliberately
extends it with `result_count` and `canonical(facts)`: these are the fields
used to decide count and fact mismatches, so leaving them unsigned would make
the cross-check meaningless. `duration_ms` is recorded but is not in the
paper's signed field list; the schema and signature still make it visible for
audit, while the core grounding fields remain explicitly committed.

A receipt contains commitments rather than raw input/output preimages. The
runtime or tool adapter that retains those preimages can independently
recompute both hashes; a standalone verifier validates their shape and the
HMAC binding without disclosing sensitive tool data.

The verifier resolves keys through Ledger's existing
`evidence_levels.resolve_key`: registries may contain raw bytes or labeled
entries with `{"key_material": bytes, ...}`. The signing key is never placed
in the receipt or passed to the LLM.

## Pramāṇa classification

Every factual claim is tagged with one of the six lowercase labels below.
`verify_claim` returns a stable verdict with `status`, `hallucination_type`,
`trust_level`, a reason, and the cited receipt ID.

| Pramāṇa | Source | Verification method | Trust when grounded |
| --- | --- | --- | --- |
| `pratyaksha` | Direct tool output | Verify the cited receipt; compare `expected_count` and `expected_facts`; reject an inferential marker mislabelled as direct output. | `fully_verified` |
| `anumana` | Inference from tool data | Verify that every declared premise exists as a key/value in receipt `facts`. | `mostly_verified` |
| `upamana` | Comparison or analogy | Verify that comparison subjects are present among receipt facts. | `partial` |
| `shabda` | External testimony/source | Find a verified fetch-type receipt whose `source_url` or `fetched_urls` contains `cited_source_url`. | `mostly_verified` |
| `abhava` | Knowledge from absence | Verify the cited receipt and require `result_count == 0`. | `mostly_verified` |
| `ungrounded` | No declared evidence | Cannot verify; emit an unverifiable verdict. | `ungrounded` |

A missing cited receipt for any label except `ungrounded` is
`fabricated_call`. A valid receipt with a wrong expected count is
`count_mismatch`; a wrong expected fact is `fact_mismatch`; an inference
labelled `pratyaksha` or an inference with absent premises is
`inference_as_fact`; a non-empty receipt used for `abhava` is `false_absence`;
and a source URL absent from all verified fetch receipts is
`source_fabrication`. These six names are stable lowercase error/metric
labels used by the benchmark.

## Six-stage protocol

1. **User request.** The user asks the agent to retrieve, search, or act.
2. **Tool execution.** The runtime—not the LLM—executes the tool and issues
   exactly one HMAC-signed receipt containing hashes, count, extracted facts,
   timing, and an ID.
3. **LLM call.** The runtime supplies the raw tool result and receipt
   identifier, but never the signing key.
4. **Self-tagged response.** The LLM emits each factual claim with a pramāṇa
   label and the receipt ID or source URL it believes supports the claim.
5. **Verification engine.** Ledger verifies signatures, cross-checks counts and
   facts, checks inference premises, validates fetched sources, and detects
   fabricated or omitted calls.
6. **Trust-annotated output.** The original response can be decorated with
   `render_claim` lines and a Markdown `render_verification_block` containing
   summary counts and omitted receipts.

`ToolReceiptLedger` is an insertion-ordered in-memory session registry. Its
`unreferenced(referenced_ids)` result is a completeness audit: a receipt that
exists but is never cited is an omitted-call suspect. A production runtime can
persist the receipts separately; this module intentionally does not replace
Ledger's durable event chain.

## Trust and threat model

The trust mapping is deliberately calibrated rather than binary:

- **Fully verified** means a direct claim agrees with a verified receipt's
  count and selected facts. It does not mean the tool itself returned true
  data.
- **Mostly verified** means the evidence supports an inference, an external
  fetch, or an absence claim, but the epistemic step is not direct observation.
- **Partial** means comparison subjects are grounded, while the analogy's
  semantic conclusion remains weaker.
- **Unreliable** is used for a cryptographically invalid receipt or a flagged
  mismatch/fabrication.
- **Ungrounded** is reserved for claims without verifiable evidence, including
  explicit opinion/unknown-comparison paths.

The threat model protects against fabricated calls, tampered receipt fields,
misstated counts/facts, fabricated source URLs, and some inference-as-fact
claims. It relies on three conditions:

1. The LLM never sees the HMAC signing key. Under HMAC unforgeability, it
   cannot manufacture a valid receipt for a call the runtime did not execute.
2. Receipt integrity is checked before epistemic comparison. Any change to
   `tool_name`, `input_hash`, `output_hash`, `result_count`, `facts`, or
   `timestamp_ms` invalidates the signature (the latter two are the deliberate
   extension to the paper's field list).
3. The runtime maintains completeness: one receipt is emitted for each tool
   call. The session ledger catches both a nonexistent cited ID and receipts
   that never appear in the response's references.

This does not protect against a compromised or incorrect tool, a stolen HMAC
key, semantic errors in a valid inference, or a model that systematically lies
in its self-tags. HMAC proves binding to the runtime's recorded data, not the
truth of an untrusted backend.

## Benchmark

`benchmark/nyaya_verify_bench.py` is a stdlib-only deterministic CLI. It uses
`random.Random(seed)` and generates exactly 1,800 JSON-serializable scenarios:

- `en`, `es`, `fr`, and `hi`;
- 50 each of `fabricated_call`, `count_mismatch`, `fact_mismatch`,
  `inference_as_fact`, `false_absence`, and `source_fabrication` per language;
- 150 clean controls per language.

The corpus uses deterministic `email_search` and `web_fetch` receipts. Clean
claims use correct pramāṇa labels; injected cases change only the targeted
reference, count, fact, label/premise relation, absence assertion, or URL. No
LLM or network call is involved. The harness reports per-type detection,
overall fabricated-tool-reference detection, clean false-positive rate, total
verification time divided by responses, and median per-claim time. It exits 2
if fabricated-reference detection is below 90% and 3 if median response
verification exceeds 20 ms.

Measured run in this worktree (`python benchmark/nyaya_verify_bench.py`):

```text
NyayaVerifyBench seed=251 scenarios=1800

Hallucination type                         Detected/total   Detection rate
-----------------------------------------  ---------------  --------------
fabricated_call                                200/200      100.00%
count_mismatch                                 200/200      100.00%
fact_mismatch                                  200/200      100.00%
inference_as_fact                              200/200      100.00%
false_absence                                  200/200      100.00%
source_fabrication                             200/200      100.00%

OVERALL fabricated-tool-reference detection rate: 100.00%
False-positive rate on clean claims: 0/600 (  0.00%)
Verification overhead: 0.0247 ms/response average; 0.0249 ms/response median; 0.0249 ms/claim median
```

This synthetic implementation is intentionally deterministic and explicit, so
its 100% rates are an engineering sanity check rather than a replacement for
the paper's model-generated evaluation. For comparison, arXiv:2603.10060
reports 94.2% fabricated-tool-reference detection, 87.6% count-mismatch
detection, and 91.3% false-absence detection, with less than 15 ms per
response. The local harness is below both the 20 ms gate and the paper's
reported latency target, but its clean controls do not measure LLM self-tagging
compliance or multilingual paraphrase difficulty.

## Why receipts instead of ZK proofs here?

Section 6.1 of arXiv:2603.10060 distinguishes the guarantees: ZK proofs show
that a model computation ran, while receipts show that an agent's claims are
grounded in tool execution evidence. For an interactive assistant, the latter
is the load-bearing question; a model can correctly execute a computation that
produces a hallucinated answer. The paper characterizes ZK proving as minutes
per query; using the paper's cited `zkLLM` comparison of roughly **180 s/query**
versus a receipt verification budget of **<20 ms** (and the paper's measured
`<15 ms` target), the latency ratio is several orders of magnitude. Receipts
also require only ordinary CPU/HMAC primitives and produce user-facing trust
levels, while ZK proving generally requires specialized hardware and yields a
computational-integrity result rather than semantic grounding. The approaches
are complementary for high-assurance systems: a deployment can use ZK for
model-execution integrity and receipts for claim grounding, but receipts are
the practical default for an interactive Ledger session.
