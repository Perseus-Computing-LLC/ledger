# Deterministic OSCAL evidence projection

Ledger's `ledger_agent.oscal` module projects a bounded, hash-only input
contract into OSCAL **Assessment Results** and **POA&M** JSON/YAML. The
projection is evidence for an assessor or system owner; it is not an ATO, AO
approval, RMF completion, CMMC decision, FIPS validation, or compliance
certification.

## Supported OSCAL version and subset

The implementation targets **OSCAL 1.2.3** and validates its synthetic
fixtures against the official NIST JSON schemas:

- [Assessment Results schema](https://github.com/usnistgov/OSCAL/releases/download/v1.2.3/oscal_assessment-results_schema.json)
- [POA&M schema](https://github.com/usnistgov/OSCAL/releases/download/v1.2.3/oscal_poam_schema.json)
- [NIST Assessment Results reference](https://pages.nist.gov/OSCAL-Reference/models/v1.2.3/assessment-results/json-reference/)

The supported model subset contains document metadata, Assessment Plan/SSP
imports, reviewed control selections, observations, findings, findings-to-
observation links, POA&M items, and a POA&M system identifier. Ledger-specific
coverage and export hashes remain outside the OSCAL model envelope so a
consumer can distinguish standard OSCAL content from the projection contract.

## Input contract

`build_ledger_event()` accepts only these fields:

- `event_id` and `control_id` — opaque event reference and OSCAL control token;
- `observed_at` — timezone-qualified ISO-8601 timestamp;
- `evidence[]` — opaque `ref`, SHA-256 `digest`, and an explicit evidence
  state;
- `observation_state` — `observed`, `missing`, `unknown`, `stale`,
  `superseded`, or `unreported`;
- `finding_state` — `satisfied`, `not-satisfied`, `unknown`, or `not-assessed`;
- `risk_state` — `none`, `low`, `moderate`, `high`, `critical`, or `unknown`;
- `remediation_status` — `complete`, `open`, `in-progress`, `accepted-risk`,
  or `unknown`;
- `human_review` and `attestation_state` — explicit approval/attestation
  states.

`project_oscal()` additionally requires the system identifier, SSP reference,
Assessment Plan reference, expected control IDs, and an explicit assessment
window. Events outside the expected control mapping are rejected instead of
being silently discarded.

Raw prompts, memory bodies, provider payloads, credentials, private policy
text, and other sensitive content are forbidden. References are bounded opaque
labels without whitespace; only those references and source digests are
retained.

`project_oscal()` validates both generated envelopes against the packaged official
NIST OSCAL 1.2.3 JSON schemas before returning. The schemas are shipped with
the package, while the test suite also keeps source fixtures for independent
regression checks. The local validator normalizes only NIST's `\\p{L}`/`\\p{N}`
pattern notation to equivalent ASCII classes because Python's standard regex
engine cannot compile those Unicode-property escapes; generated identifiers
are restricted to ASCII OSCAL tokens.

A control is clean only when Ledger has a non-empty set of observed evidence,
all evidence items are observed, the finding is satisfied, risk is `none`,
remediation is complete, human review is approved, and the event is attested.
Every other posture is represented as `not-satisfied` and remains visible in
both the coverage report and the OSCAL finding/POA&M description. In
particular, missing, unknown, stale, superseded, and unreported evidence can
never become a clean result.

The returned coverage report has deterministic lists for expected, observed,
clean, and unreported controls, plus per-state control lists. The two
`export_hashes` values are SHA-256 hashes of the sorted-key, compact JSON
representation of the emitted Assessment Results and POA&M documents.

## Example

```python
from ledger_agent import oscal

projection = oscal.project_oscal(
    [
        oscal.build_ledger_event(
            event_id="evt-ac-2",
            control_id="ac-2",
            observed_at="2026-08-19T12:00:00Z",
            evidence=[{
                "ref": "ledger:evidence/evt-ac-2",
                "digest": "<64-char SHA-256>",
                "state": "observed",
            }],
            observation_state="observed",
            finding_state="satisfied",
            risk_state="none",
            remediation_status="complete",
            human_review="approved",
            attestation_state="attested",
        )
    ],
    system_id="system:example",
    ssp_ref="https://example.test/ssp.json",
    assessment_plan_ref="https://example.test/assessment-plan.json",
    expected_control_ids=["ac-2", "ac-3"],
    assessment_start="2026-08-19T00:00:00Z",
    assessment_end="2026-08-19T23:59:59Z",
)
```
