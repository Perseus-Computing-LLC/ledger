# Perseus Ledger launch assets — 2026-07

Draft copy and a launch checklist. Nothing in this document posts externally.
Every public asset must be verified against the release before use.

## Demo recording plan

Goal: a 45–60 second loop that shows the evidence lifecycle.

1. Start a local Ledger demo with the stable compatibility command:
   `ledger demo`, then open `http://localhost:8420`.
2. Record an event through the SDK or `POST /v1/usage`.
3. Show the event’s actor/boundary/configuration/allocation projection and its
   place in the chain.
4. Run integrity verification and show a retained checkpoint or evidence receipt.
5. End with the distinction: Ledger records evidence; allocation and Stripe are
   optional adapters.

Capture terminal steps with asciinema and dashboard steps as a short silent GIF.
Do not claim a field in demo copy unless the recording shows it.

## README polish

- Lead with **Perseus Ledger**, not legacy identifiers.
- State the product question: what happened, under what authority and evidence,
  and can the history be verified?
- Describe `ledger-agent`, `ledger`, `ledger_agent`, `LEDGER_*`, and `/v1` as
  compatibility identifiers during migration.
- Keep runtime integrations optional. Do not make Hermes or another agent
  runtime part of Ledger’s product identity.
- Link the product site, integrity documentation, API, migration guidance, and
  demo media once recorded.

## Show HN draft

Title:
```text
Show HN: Perseus Ledger – self-hosted, verifiable event provenance for autonomous systems
```

Body:
```text
Autonomous systems need more than a dashboard aggregate. They need a defensible
answer to what happened, under what authority and evidence, and whether the
history can be verified later.

Perseus Ledger is a self-hosted, MIT-licensed event and provenance layer. It
records append-only, hash-chained activity with optional actor, workspace,
configuration, external evidence, authority-reference, and resource-allocation
metadata. It works offline with SQLite and integrates through HTTP or Python,
without requiring a particular agent framework.

The stable package and CLI remain `ledger-agent` and `ledger` during the
migration. Metering, reconciliation, prepaid credit, and Stripe are supported
optional adapters, not the product boundary.

Repo: https://github.com/Perseus-Computing-LLC/Ledger
Product site: https://perseus.observer/ledger/
```

## X thread draft

```text
1/ Introducing Perseus Ledger: self-hosted, verifiable event provenance for
autonomous systems. MIT licensed and runtime-neutral.

2/ The question is not only “what did the system cost?” It is: what happened,
under what authority and evidence, and can the history be verified later?

3/ Ledger records append-only, hash-chained events with optional actor,
workspace, configuration, evidence, authority, and allocation metadata.

4/ It works offline with SQLite, HTTP, and Python. It does not require a
particular agent framework or hosted service.

5/ Metering, reconciliation, prepaid credit, and Stripe are optional adapters.

6/ Existing users retain the stable compatibility package and CLI:
`pip install ledger-agent`; `ledger` remains supported during transition.

7/ https://github.com/Perseus-Computing-LLC/Ledger
   https://perseus.observer/ledger/
```

## Blog outline

Working title: **“An audit trail is a product boundary: building a verifiable
record for autonomous systems”**

1. The operational question: reconstruct what happened, not merely an aggregate.
2. Chain integrity, external checkpoints, and the limits of self-attestation.
3. Evidence and authority references without retaining raw prompts or secrets.
4. Runtime-neutral ingestion and local-first operation.
5. Allocation, metering, and settlement as optional adapters.
6. Compatibility transition from the `ledger*` install surface to Perseus Ledger.

## Submission-ready blurb

```text
Perseus Ledger is a self-hosted, runtime-neutral event and provenance layer for
autonomous systems. It records hash-chained activity, evidence links, authority
references, and optional resource allocation so teams can reconstruct what
happened and independently verify the resulting history. SQLite local-first,
HTTP and Python interfaces, MIT licensed. Existing installs use the compatible
`ledger-agent` package and `ledger` CLI during transition.
```

## Release and public-launch gates

- [ ] Canonical product site and legacy compatibility route both verified live.
- [ ] Repository, README, package metadata, images, and registry listings agree.
- [ ] Canonical Ledger aliases/migration guide ship before any compatibility
      deprecation announcement.
- [ ] External security review and outstanding security scope are resolved or
      accurately disclosed.
- [ ] Demo records only fields Ledger actually persists and verifies.
- [ ] Human approves and performs outward posts, marketplace listings, and any
      Stripe-account action.

## Explicit human/outward gates

- Posting to Show HN, X, LinkedIn, dev.to, directories, or marketplaces.
- Commissioning or accepting an external security review.
- Creating or altering Stripe products, prices, or marketplace submissions.

These are drafts only; they create no external commitment.
