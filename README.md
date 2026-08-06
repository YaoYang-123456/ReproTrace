# ReproTrace

ReproTrace is an evidence-first execution recorder for research reproduction.
It does not claim that a successful process exit reproduces a paper. Instead,
it connects the exact source, environment, inputs, commands, logs, artifacts,
metrics, and tolerance decision in one inspectable evidence bundle.

The current `0.1.0` slice intentionally targets a small problem:

- local, sequential Python/PyTorch-style experiments;
- CPU-first validation with a deterministic tiny fixture;
- CSV and regular-expression metric extraction;
- `run`, `verify`, `diff`, and `report` commands;
- a PEFT-ViT manifest that can be preflighted before a GPU run.

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'

reprotrace run examples/tiny/reprotrace.yaml
```

The command prints the evidence directory. It contains the resolved manifest,
source and environment snapshots, input and artifact hashes, command records,
raw logs, extracted metrics, verification result, and a Markdown report.

Run the remaining commands with that directory:

```bash
reprotrace verify .reprotrace/runs/<run-id>
reprotrace report .reprotrace/runs/<run-id>
reprotrace diff .reprotrace/runs/<run-a> .reprotrace/runs/<run-b>
```

To inspect a real PEFT-ViT command without launching training:

```bash
reprotrace run --dry-run \
  --project-root /path/to/peft-vit \
  examples/peft-vit/reprotrace.yaml
```

The reusable PEFT-ViT manifest is pinned to the audited upstream commit. The
checkout path is supplied explicitly so the example remains outside that
repository. ReproTrace checks required inputs during this preflight.

Relative `run.output_root` paths are resolved from the manifest directory, not
from `project.root`. ReproTrace rejects an unignored evidence output path inside
the audited Git worktree so its own bundle cannot make the recorded source dirty.

## Exit codes

- `0`: verification/preflight passed, or a report completed;
- `1`: recorded evidence differs or a reproduction/preflight check failed;
- `2`: the manifest or evidence bundle is invalid.

## Evidence over automation

ReproTrace records observed facts and exposes disagreement. It does not silently
choose between conflicting environments, infer private dataset access, or turn
an approximate protocol into a strict paper reproduction.

See [the v0 design note](docs/design-v0.md) for the current scope and decisions.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
```

ReproTrace is licensed under the MIT License.
