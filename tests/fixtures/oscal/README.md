# OSCAL v1.2.3 schema fixtures

These are the official NIST OSCAL JSON schemas used by `test_oscal_projection.py`
to validate generated Assessment Results and POA&M documents.

Source release: https://github.com/usnistgov/OSCAL/releases/tag/v1.2.3

Files:

- `oscal_assessment-results_schema.json`
- `oscal_poam_schema.json`

The projection emits the supported OSCAL 1.2.3 JSON subset and keeps Ledger-
specific coverage and claim-ceiling data outside the OSCAL model envelope.
