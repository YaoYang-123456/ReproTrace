# C5 adversarial acceptance matrix

This matrix names the producer-controlled evidence attacks that C5 must handle
and the explicit boundary it does not claim to cross. The tests use complete
schema-1 bundles produced through the normal runner unless a case specifically
targets legacy compatibility or a low-level path primitive.

## Matrix

| Case | Threat | Required observation | Regression coverage |
|---|---|---|---|
| A1 | A command record claims `completed` and return code `0`, but a required command log is absent | The recorded outcome may remain `recorded_success`, but closure fails, checks are incomplete, the verified root is withheld, assurance does not exceed `recorded`, and execution authenticity remains not established | `tests/test_assurance_verifier.py::test_a1_a2_forged_success_or_tampered_command_log_cannot_raise_assurance[missing]` |
| A2 | An indexed stdout/stderr log is deleted or modified without rebuilding evidence | Integrity fails, the verified root is withheld, and assurance does not exceed `recorded` | `tests/test_assurance_verifier.py::test_a1_a2_forged_success_or_tampered_command_log_cannot_raise_assurance` |
| A3 | `metrics.json.actual` is changed and the canonical index is validly rebuilt while raw evidence is unchanged | Bundle integrity can remain valid, but exact derivation comparison fails; assurance stops at `bundle_integrity_checked` and the result is `indeterminate` | `tests/test_assurance_verifier.py::test_v3_derived_metric_tamper_survives_index_but_fails_derivation` and `test_v11_derived_actual_uses_strict_not_scientific_tolerance` |
| A4 | Raw metric evidence and its embedded/index hashes are changed coherently, but cached derived metrics are left unchanged | Bundle integrity can remain valid, but verifier re-extraction disagrees with cached derivation; assurance stops at `bundle_integrity_checked` and the result is `indeterminate` | `tests/test_assurance_verifier.py::test_v4_raw_metric_tamper_fails_derivation_after_integrity_reconstruction` |
| A5 | A complete bundle is relocated and its producer/origin files disappear or change | Verification uses only bundle-local evidence; root, assurance, and result remain stable | `tests/test_assurance_verifier.py::test_v9_relocated_bundle_verifies_after_origin_deletion`, `test_v10_origin_metadata_is_not_used_for_schema_one_io`, and the Stage-6 real tiny relocation acceptance |
| A6 | `manifest.resolved.yaml` expected value or tolerance is changed and the index is rebuilt without changing cached derived metrics | Integrity can remain valid, but manifest/derived decision consistency fails; the old cached decision is not trusted | `tests/test_assurance_verifier.py::test_a6_resolved_protocol_tamper_with_valid_rehash_fails_derived_consistency` |
| A7 | An evidence path uses traversal, an absolute/drive/UNC form, or a symlink/junction escape | Resolution fails before the outside target is read | `tests/test_evidence_index.py::test_bundle_resolver_rejects_unsafe_paths`, `test_final_symlink_escape_is_rejected`, `test_parent_symlink_escape_is_rejected`, `test_parent_junction_escape_is_rejected`, plus the corresponding source-evidence tests in `tests/test_source_capture.py` |

The declaration-closure regressions in `tests/test_assurance_verifier.py`
independently cover missing, extra, duplicate, or modified input/artifact
records, extra index entries, and role mismatches. They prevent a rebuilt index
from hiding disagreement with authoritative manifest declarations.

## Expected limitation: coherent producer forgery

`test_coherent_producer_forgery_is_an_explicit_undetectable_boundary` rewrites
command records, logs, raw metric evidence, cached metrics, the resolved
manifest, metadata, and the evidence index into a new internally coherent
bundle. C5 may classify that bundle as `metric_derivations_recomputed`. This is
an expected threat-model boundary, not an expected test failure.

Even in that case, the canonical result continues to state:

```text
execution_authenticity = not_established
independent_replay = not_performed
scientific_reproduction = not_established
```

The evidence root identifies the internally consistent snapshot presented to
the verifier. It is not a signature, attestation, trusted timestamp, producer
identity, or proof that the recorded execution occurred.

## Acceptance invariants

- Expectation mismatch is orthogonal to verification: intact, exactly
  recomputed evidence may be `complete` with result `not_matched`.
- A valid zero-metric run stops at `bundle_integrity_checked` and is
  `not_evaluated`, not failed.
- A dry-run is a planning bundle with `execution_record_status=not_run`; it can
  establish indexed planning integrity but not metric derivation or execution.
- A recorded command failure does not by itself corrupt intact evidence.
- Legacy schema-0 bundles remain readable but never exceed `recorded` and never
  derive a new scientific result from historical compatibility fields.

## Stage 6.1 protocol-closure hardening

Additional audit regressions bind command argv/cwd/environment/timeout and log
identities to the resolved manifest protocol, reject non-enum command status and
boolean return codes, enforce the command/run state machine, and demonstrate
that a rehashed `commands.jsonl` archive is not a second semantic authority.

Artifact regressions rebuild a valid index around an out-of-pattern log file or
a duplicate canonical match and prove that wildcard manifest membership still
fails. Numeric regressions reject boolean expected values, non-finite or
negative tolerances, invalid timeouts, and non-finite recomputed metric values.

These deterministic closure tests do not address a file changing between
verifier hash/open/parse operations. Immutable snapshot identity and
verifier-time TOCTOU are explicitly deferred beyond Stage 6.1.
