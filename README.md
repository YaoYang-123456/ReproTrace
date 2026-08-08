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

The command prints a canonical verification summary and the evidence directory,
for example:

```text
verification: COMPLETE
checks: PASS
assurance: metric_derivations_recomputed
recorded execution: recorded_success
declared result: matched
execution authenticity: NOT ESTABLISHED
independent replay: NOT PERFORMED
scientific reproduction: NOT ESTABLISHED
evidence root: <sha256>
bundle: <path>
```

The directory contains the resolved manifest, source and environment snapshots,
input and artifact hashes, command records, raw logs, extracted metrics, a
canonical evidence index, verification result, and a Markdown report. For a
Git worktree, `source.patch` preserves the exact binary-capable diff bytes and
`source.status` preserves the NUL-delimited porcelain status; `source.json`
records their formats, sizes, SHA-256 hashes, and replay coverage.

Normal runs also write `metric_sources.json`. Metric sources already inside the
bundle, such as command logs and run artifacts, are referenced directly;
external files actually consumed by an extractor are snapshotted under
`raw/metrics/`. Derived metrics are then extracted from those bundle-local bytes,
while origin paths remain historical metadata only.

For schema-1 bundles, `commands.json` is the sole semantic command record and is
bound field-by-field to the producer-finalized protocol in
`manifest.resolved.yaml`. `commands.jsonl` is an indexed convenience/archive
export; the verifier protects its bytes but does not treat it as a second command
authority. Command log identities are fixed as `logs/<step>.stdout.log` and
`logs/<step>.stderr.log`.

Run the remaining commands with that directory:

```bash
reprotrace verify .reprotrace/runs/<run-id>
reprotrace report .reprotrace/runs/<run-id>
reprotrace diff .reprotrace/runs/<run-a> .reprotrace/runs/<run-b>
```

`verify --json` emits the complete canonical verification object and retains
deprecated `status`, `passed`, and `preflight_passed` fields only for compatible
callers. The `report` command always refreshes verification before regenerating
`report.md`, so evidence changed after the original run cannot retain a stale
successful report.

Verification and result evaluation are separate. A scientifically valid
expectation miss can therefore show `verification: COMPLETE`, `checks: PASS`,
and `declared result: not_matched` at the same time. The bundle and metric
derivations were checked successfully; the declared target was not reached.
The command exits `1` for that requested run outcome without reclassifying the
verification as incomplete.

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
The isolation check reuses the worktree root captured before bundle creation and
fails closed if Git cannot determine whether an in-worktree output is ignored.
Git output is captured as bytes rather than decoded with the host locale, so
UTF-8, legacy-encoded text, and binary changes are recorded consistently across
platforms. The patch covers tracked changes relative to `HEAD`; untracked paths
appear in `source.status`, but their contents are intentionally not copied.

## Exit codes

- `0`: canonical verification completed, the recorded command outcome is
  successful, and the declared result is `matched` or not applicable; a
  successful dry-run preflight also exits `0`;
- `1`: canonical checks are incomplete, evidence/derivation disagrees, a
  recorded command failed, the declared result is `not_matched` or
  `indeterminate`, or dry-run preflight failed;
- `2`: the manifest or evidence bundle is invalid.

Legacy schema-0 bundles preserve their previous compatibility exit policy, but
their canonical presentation remains `assurance: recorded` and
`declared result: not_evaluated`.

## Evidence over automation

ReproTrace records observed facts and exposes disagreement. It does not silently
choose between conflicting environments, infer private dataset access, or turn
an approximate protocol into a strict paper reproduction.

See [the v0 design note](docs/design-v0.md) for the current scope and decisions.
The canonical C5 verification vocabulary and its explicit limitations are
defined in [the assurance contract](docs/assurance-contract-v1.md). The
bundle-safe path rules and canonical index format are defined in
[the evidence index specification](docs/evidence-index-v1.md). New schema-1 runs
emit a dependency-closure index and can reach bundle-integrity assurance after
all indexed bytes and authoritative references are checked.
The raw metric source schema and its Stage 3 assurance boundary are documented
in [the metric source specification](docs/metric-sources-v1.md). The formal
[C5 adversarial acceptance matrix](docs/adversarial-acceptance.md) maps the
supported tamper, relocation, and path-escape cases to repeatable tests and
states the coherent-producer-forgery boundary.

`metric_derivations_recomputed` means recorded metric derivations were
re-extracted from indexed evidence and agreed exactly. It does **not** mean the
experiment was independently replayed or the paper was scientifically
reproduced.

Manifest expected values and tolerances, command timeouts, and extracted metric
values use a finite numeric domain: booleans, NaN, and infinity are rejected;
tolerances are non-negative and present timeouts are positive. C5 verification
still does not provide an immutable verifier-time filesystem snapshot. A file
can change between separate verifier operations; that TOCTOU boundary is not
solved by the evidence index.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
```

ReproTrace is licensed under the MIT License.
