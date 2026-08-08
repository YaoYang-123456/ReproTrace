# Raw metric source evidence v1

Stage 3 captures the exact files consumed by each metric extractor so a new run's
derived `metrics.json` can be traced to bundle-local raw evidence. Stage 4 now
performs verifier-side re-extraction for run schema 1 and grants derivation
assurance only when the complete indexed closure and all derived fields agree.

## Record format

Every new run contains `metric_sources.json` schema 1:

```json
{
  "schema_version": 1,
  "metrics": [
    {
      "id": "mean_score",
      "declared_path": "{project_root}/output/metrics.csv",
      "sources": [
        {
          "ordinal": 0,
          "origin_path": "C:/producer/output/metrics.csv",
          "evidence_path": "raw/metrics/mean_score/abcdef012345-0000.source",
          "size_bytes": 123,
          "sha256": "<lowercase SHA-256 hex>"
        }
      ]
    }
  ]
}
```

Metric IDs are unique. Each source list preserves the existing sorted origin
match order, and `ordinal` must equal its zero-based list position. Extractors
consume this order directly; they never sort by `evidence_path`, hash, dictionary
order, or filesystem traversal order. This preserves multi-file row/match order
and `select: last` semantics.

`origin_path` is historical producer metadata only. It is retained in the
compatible derived `source_paths` field but is never opened during bundle-local
extraction. `evidence_path` is a canonical POSIX-style path inside the bundle and
is resolved with the common bundle-safe path resolver.

## Capture behavior

After commands and artifact capture, the runner resolves exactly the files that
the CSV or log-regex extractor would read:

- A source already inside the current bundle is referenced directly. Command
  logs and bundle-local artifacts are not copied again.
- An external source is read as bytes and atomically copied to
  `raw/metrics/<metric-id>/<hash-prefix>-<ordinal>.source`. No directory,
  checkpoint, dataset, unrelated output, or complete artifact tree is copied.
- Final size and SHA-256 metadata are calculated from the actual bundle-local
  file after capture, not copied from an origin fingerprint.

The runner writes `metric_sources.json` before extraction, verifies that each
bundle-local file still matches the captured size and hash, then extracts only
from those explicit paths. Missing matches, snapshot failures, changed captured
bytes, unreadable sources, and missing numeric values remain explicit evidence
collection errors; no empty or fabricated metric is produced.

Zero-metric runs and dry-runs write a valid empty record:

```json
{"schema_version": 1, "metrics": []}
```

## Extraction architecture and authority

Origin matching, source capture, and value extraction are separate operations.
The pure extraction function receives a manifest metric specification and an
explicitly ordered list of bundle-local paths. It needs no producer project root
and never consults `origin_path`, so Stage 4 can reuse the same CSV, log-regex,
selector, and numeric parsing implementation.

The resolved manifest remains authoritative for `expected`, `atol`, and `rtol`.
The runner records `actual`, `passed`, and related fields in `metrics.json` as
cached derived compatibility data and adds `source_evidence_paths` without
removing the historical `source_paths` field. `metric_sources.json` does not
carry expected values or tolerances.

## Assurance and race boundary

Raw evidence capture alone does not establish that the producer execution was
authentic. Stage 4 can establish internal bundle integrity and recompute metric
derivations, but it still does not establish producer identity, independent
execution replay, or scientific reproduction.

Stage 3 snapshots one observed byte sequence and derives the metric from that
same bundle-local sequence. It does not claim protection against a malicious
producer or all origin-side TOCTOU races, signing, attestation, replay, or trusted
execution.

C5 adversarial tests distinguish two useful failure layers. Changing cached
`metrics.json` while leaving raw sources intact passes byte-integrity checks but
fails exact derivation comparison. Changing raw sources and rebuilding their
metadata/index while leaving cached metrics intact also passes byte-integrity
checks but fails verifier re-extraction. Coherently changing both sides and all
producer-controlled declarations remains outside the threat model. The exact
cases are mapped in [the adversarial acceptance matrix](adversarial-acceptance.md).
