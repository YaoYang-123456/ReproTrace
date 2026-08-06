# ReproTrace v0 design

## Goal

ReproTrace records a claim-to-evidence chain for a local research experiment:

```text
source -> environment -> inputs -> commands -> logs -> artifacts -> metrics -> decision
```

The core remains project-neutral. PEFT-ViT, VPT, SSF, and FreqFit informed the
schema, but no project name is hard-coded into the implementation.

## Command contract

- `run`: resolve a manifest, capture pre-execution evidence, execute sequential
  steps, collect artifacts and metrics, then verify the bundle.
- `verify`: re-check recorded steps, current input/artifact hashes, and metric
  tolerances. Exit `0` means pass, `1` means mismatch, and `2` means invalid
  evidence or configuration.
- `diff`: compare two bundles across source, environment, inputs, commands,
  artifacts, and metrics.
- `report`: regenerate a readable Markdown report from machine-readable files.

`run --dry-run` resolves commands and checks source and input prerequisites
without launching the experiment. A successful preflight exits `0`, but its
evidence status remains `planned`; it is not a reproduced result.

Relative evidence output roots are resolved from the manifest directory. Before
creating a bundle, ReproTrace captures the source state and rejects an unignored
output path inside the audited Git worktree. This keeps recorder output separate
from the source state it is meant to describe.

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
resolved paths, sizes, and SHA-256 hashes.
