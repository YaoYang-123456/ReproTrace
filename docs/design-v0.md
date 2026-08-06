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

## v0 boundaries

Supported now:

- one local machine;
- sequential argv-based subprocess steps;
- declared file inputs and artifacts;
- CSV and log-regex metrics;
- one seed per evidence bundle;
- source, environment, command, log, hash, metric, and report capture.

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
