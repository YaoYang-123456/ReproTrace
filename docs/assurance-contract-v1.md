# Verification assurance contract v1

This document defines the canonical verification vocabulary introduced in C5.
It is deliberately narrower than execution attestation or scientific
reproduction.

## Assurance hierarchy

The ordered levels are:

```text
recorded
    ↓
bundle_integrity_checked
    ↓
metric_derivations_recomputed
```

`recorded` means the required bundle records are present, parseable, structurally
valid, and use supported schemas. It does not establish that their bytes are an
untampered snapshot or that any recorded process actually ran.

`bundle_integrity_checked` additionally means every byte used by the verifier is
inside the bundle's indexed evidence closure and matches its recorded size and
SHA-256. The evidence root identifies that snapshot; it is not a signature,
producer identity, trusted timestamp, or authenticity proof.

`metric_derivations_recomputed` additionally means at least one declared metric
was independently re-extracted from indexed bundle-local raw evidence and its
stored derivation and declared tolerance decision were checked against the
resolved manifest. A valid zero-metric experiment stops at
`bundle_integrity_checked` without failing verification.

Stage 1 defines this hierarchy but conservatively assigns existing schema-0
bundles only `recorded`. Stage 2 adds canonical evidence-index primitives but
does not yet connect production bundles to a complete indexed closure, so those
bundles remain `recorded`. Raw metric derivation is Stage 3/4.

## Orthogonal canonical fields

Verification schema 1 contains these canonical fields:

| Field | Values | Meaning |
|---|---|---|
| `verification_status` | `complete`, `incomplete`, `invalid` | Whether the applicable verification contract completed |
| `assurance_level` | hierarchy above | Which evidence guarantees were actually established |
| `execution_record_status` | `not_run`, `recorded_success`, `recorded_failure`, `unknown` | What producer-supplied command records claim |
| `result_status` | `matched`, `not_matched`, `indeterminate`, `not_evaluated` | Outcome of the declared result evaluation |
| `checks_passed` | boolean | Whether the canonical contract checks applicable at this stage passed |
| `coverage` | object | Machine-readable limits of bundle-local evidence coverage |
| `not_established` | object | Claims explicitly outside the verification guarantee |

These dimensions are intentionally independent. A future schema-1 bundle may
have complete metric derivation assurance while its declared result is
`not_matched`. That is an experimental outcome, not an evidence-integrity
failure.

`contract_checks` contains only checks that determine canonical
`verification_status` and `checks_passed`. The deprecated `checks` list retains
the previous mixed source, input, command, artifact, and metric-expectation
checks for compatibility. A failed recorded command or an unmatched metric may
therefore make legacy `status`/`passed` fail without making canonical
verification incomplete.

Invalid bundle/schema/config input raises a configuration error and does not
produce a misleading verification record. `invalid` remains part of the
canonical status vocabulary for API/CLI presentation in the later exposure
stage.

## Execution record semantics

Execution record values always include the word `recorded` where appropriate:

- `not_run`: dry-run; no execution is claimed;
- `recorded_success`: command records claim successful completion;
- `recorded_failure`: command records claim failure, timeout, or launch error;
- `unknown`: no sufficient command record is available.

No value establishes that a real process ran. Every verification record carries:

```json
{
  "not_established": {
    "execution_authenticity": "not_established",
    "independent_replay": "not_performed",
    "scientific_reproduction": "not_established"
  }
}
```

## Coverage skeleton

Stage 1 emits machine-readable coverage without claiming evidence closure:

```json
{
  "coverage": {
    "inputs": {"bundle_local": 0, "external_metadata_only": 2},
    "artifacts": {"bundle_local": 0, "external_metadata_only": 1},
    "metric_sources": {"captured": 0, "recorded": 0, "total": 1},
    "source": {"replay": "partial"}
  }
}
```

`metric_sources.total` is the authoritative declared metric count from
`manifest.resolved.yaml`; `recorded` is the number of derived records currently
present in `metrics.json`. Stage 3 captures raw metric source evidence for new
normal runs, but the Stage 1 verification skeleton continues to report
`captured: 0` until Stage 4 actually validates those records and their bundle
closure. These counts do not upgrade assurance.

## Deprecated compatibility fields

The fields `status`, `passed`, and `preflight_passed` remain temporarily so old
callers and tests continue to function. They are deprecated compatibility fields
and are duplicated under `compatibility` as `legacy_status` and
`legacy_passed`. They must not determine assurance or result semantics.
The legacy `checks` list likewise remains separate from canonical
`contract_checks` and cannot determine `verification_status` or
`checks_passed`.

During Stage 1 the existing CLI and report still consume these fields. Their
canonical presentation and exit-code migration is explicitly deferred to Stage
5; the compatibility fields are not a source for new verification logic.

Legacy bundle schema 0 never exceeds `recorded`, and its canonical
`result_status` is `not_evaluated`, even if a historical compatibility field says
`passed=true`.

## Expected limitation

An attacker who rewrites the manifest, logs, raw metric sources, derived records,
and evidence index into a new internally consistent bundle may still satisfy the
highest C5 assurance level. Detecting that producer-level forgery requires trust,
signing, attestation, or an independent replay system and is outside this
contract.

Therefore C5 assurance never establishes execution authenticity, independent
replay, or scientific reproduction.
