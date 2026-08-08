# ReproTrace v0 design

## Goal

ReproTrace records a claim-to-evidence chain for a local research experiment:

```text
source -> environment -> inputs -> commands -> logs/artifacts -> raw metric sources -> metrics -> decision
```

The core remains project-neutral. PEFT-ViT, VPT, SSF, and FreqFit informed the
schema, but no project name is hard-coded into the implementation.

## Command contract

- `run`: resolve a manifest, capture pre-execution evidence, execute sequential
  steps, collect artifacts and metrics, then verify the bundle.
- `verify`: for run schema 1, validate the bundle-local indexed dependency
  closure, recompute metrics from indexed raw evidence, and evaluate declared
  tolerances from the resolved manifest. Legacy schema 0 remains readable
  without dereferencing recorded input/artifact origin paths. Human output
  presents verification completeness, assurance, recorded execution, and the
  declared result as separate dimensions. JSON output retains compatibility
  fields but canonical fields are authoritative.
- `diff`: compare two bundles across source, environment, inputs, commands,
  artifacts, and metrics.
- `report`: refresh canonical verification and regenerate a readable Markdown
  report from that fresh verifier result. `report.md` remains outside the
  evidence index to avoid self-reference.

`run --dry-run` resolves commands and checks source and input prerequisites
without launching the experiment. A successful preflight exits `0`, but its
evidence status remains `planned`; it is not a reproduced result.

For non-dry schema-1 commands, exit `0` requires complete canonical checks, a
recorded successful command outcome, and a `matched` or non-applicable declared
result. Exit `1` covers integrity/derivation failure, recorded command failure,
`not_matched`, and `indeterminate`. Invalid configuration or evidence exits `2`.
Expectation mismatch does not change verification completeness or assurance,
even though the command exits `1` because the requested declared result was not
met. Schema-0 bundles retain their compatibility exit policy while presenting
only `recorded` assurance and `not_evaluated` result status.

Relative evidence output roots are resolved from the manifest directory. Before
creating a bundle, ReproTrace captures the source state and rejects an unignored
output path inside the audited Git worktree. This keeps recorder output separate
from the source state it is meant to describe.

## Schema-1 verification pipeline

New normal and dry runs write `run.json` schema 1 and a canonical
`evidence.index.json`. The index is built only after command logs are closed and
all producer records are finalized. It contains the exact verifier dependency
closure: core records, referenced source evidence, attempted-command logs,
bundle-local inputs/artifacts, and raw metric sources. Self-derived index,
verification, and report files are excluded.

The verifier never opens schema-1 origin metadata. Bundle evidence is resolved
through the shared safe-path resolver and must be a regular indexed file with
matching size and SHA-256. External inputs and artifacts remain metadata-only.
Metric values are recomputed from the ordered `metric_sources.json` evidence
paths, compared strictly with cached derived values, and then compared with the
resolved manifest's expected value and scientific tolerances. Expectation
mismatch changes `result_status`, not verification completeness or assurance.

Reports consume this same verifier result rather than re-extracting metrics.
Their metric table separates recorded and recomputed values, and their check
tables separate canonical structure/integrity/derivation checks from recorded
outcome and expectation compatibility checks. Coverage explicitly distinguishes
bundle-local evidence from external metadata-only records. No presentation
claims execution authenticity, independent replay, or scientific reproduction.

## Adversarial verification boundary

C5 regression coverage deliberately separates byte integrity, derivation
consistency, and producer authenticity. It detects missing or unindexed command
logs, indexed-byte tampering, disagreement between raw metric evidence and
cached derived metrics, resolved-protocol changes that invalidate cached
decisions, declaration-closure changes, and bundle-path escape attempts. It also
proves that schema-1 verification remains bundle-local after relocation and
producer-origin disappearance.

The verifier cannot detect a malicious producer that coherently rewrites every
record, raw source, resolved declaration, and the canonical index. Such a bundle
may be internally consistent and reach the highest C5 assurance level. The
canonical `not_established` fields remain mandatory because the root is not a
signature or attestation and no independent replay occurred. The exact A1–A7
mapping is maintained in [the adversarial acceptance matrix](adversarial-acceptance.md).

## Source evidence format

Git subprocess output is captured as bytes without locale-dependent decoding.
For repositories with a valid `HEAD`, each new bundle contains two source files:

- `source.status`: exact `git status --porcelain=v1 -z` bytes, including raw,
  NUL-delimited tracked and untracked path status;
- `source.patch`: exact deterministic `git diff --binary --full-index` bytes for
  staged and unstaged tracked changes relative to `HEAD`.

`source.json` schema 1 records the Git version, fixed generation arguments,
the manifest project root and resolved Git worktree root, file sizes, SHA-256
hashes, and explicit coverage metadata. All Git state commands run from that
worktree root so a manifest rooted in a subdirectory still captures repository-
wide tracked and untracked status. Output isolation reuses that captured root;
it does not rediscover the repository after the snapshot, and an indeterminate
`check-ignore` result aborts before run-directory creation. The source files are
captured before bundle creation, then written atomically before `source.json`.
Verification validates their bundle-relative paths, regular-file type, size,
and hash before treating them as intact evidence. Verify, diff, and report share
one source-record parser, reject malformed or unknown schemas consistently, and
continue to read legacy records without requiring the additional files.

Replay coverage is intentionally partial: the patch can reconstruct tracked
changes from the recorded base commit, while untracked names are present only in
the status evidence. Untracked and ignored contents and dirty submodule worktree
contents are not copied. Reports must not describe a bundle as a complete source
archive.

The fixed diff arguments neutralize context, inter-hunk context, algorithm,
rename, color, prefix, external-diff, textconv, blank-empty suppression, file
ordering, and submodule-display preferences. `diff.orderFile` is overridden with
the empty `/dev/null` order file understood by Git on Windows, Linux, and macOS;
the cross-platform test matrix verifies the resulting bytes. Repository
attributes and Git settings that define the effective worktree—such as EOL
normalization, file modes, symlink behavior, case handling, and text/binary
classification—remain in effect intentionally. Git version and argv are recorded
because equivalent logical changes are not guaranteed to serialize identically
across different Git versions or effective worktree configurations.

## v0 boundaries

Supported now:

- one local machine;
- sequential argv-based subprocess steps;
- declared file inputs and artifacts;
- CSV and log-regex metrics;
- ordered bundle-local raw metric source capture;
- one seed per evidence bundle;
- source, environment, command, log, hash, metric, and report capture;
- locale-independent raw Git status and binary-capable patch evidence.

Deferred:

- Slurm and cross-node DDP;
- automatic environment repair;
- gated datasets or weights;
- arbitrary PDF-to-command inference;
- automatic paper-wide experiment matrices;
- untrusted pickle/PTH metric deserialization in the generic core;
- cross-run statistical aggregation.

## Safety

Commands are argv arrays and run without `shell=True`. Environment variables
whose names contain token, key, secret, or password are excluded from recorded
evidence. ReproTrace does not copy large artifacts by default; it records their
resolved paths, sizes, and SHA-256 hashes. The only new Stage 3 copies are the
specific text files actually matched and consumed by metric extractors; existing
bundle logs and artifacts are referenced without duplication.
