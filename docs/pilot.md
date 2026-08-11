# 15-minute real-world ReproTrace pilot

ReproTrace is looking for evidence about usefulness, not testimonials. This
pilot asks whether an evidence bundle helps you understand and check one small
ML result better than your usual Git commit/SHA + manifest record, and whether
that benefit justifies the setup cost.

The pilot is designed for about 15 minutes. Use a short CPU workflow; do not
start a long training job, GPU run, or download-heavy experiment for this pilot.

## Who this is for

This is a good fit if you run a local, sequential Python or PyTorch-style ML
workflow and can use a public example or a sanitized slice of your own work. The
current scope supports declared file inputs and artifacts, argv-based commands,
and metrics extracted from CSV files or command logs with regular expressions.

It is not a fit for private-only material that cannot be safely inspected, or
for workflows that require distributed execution, orchestration, untrusted
pickle/PTH metric loading, or a full experiment matrix.

## Safety before you start

The feedback form creates a **public GitHub Issue**. Never upload or paste
secrets, credentials, tokens, private or personal data, proprietary code,
unreleased model details, confidential logs, or restricted datasets. Use public,
synthetic, or properly sanitized names and summaries. Review any bundle content
yourself before sharing it; the pilot does not require uploading the bundle.

ReproTrace redacts declared environment override values whose names contain
common secret markers, but that heuristic is not a substitute for your own
review and redaction.

## Prerequisites

- Python 3.10 or newer and Git;
- a local clone of ReproTrace;
- permission to run a short CPU command;
- optionally, one public or sanitized ML workflow whose command, inputs,
  artifacts, and CSV or log-regex metric you understand.

## 1. Install and run the canonical fixture (about 5 minutes)

From the ReproTrace repository root, use the commands for your platform.

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\reprotrace.exe --version
.\.venv\Scripts\reprotrace.exe run examples/tiny/reprotrace.yaml
```

POSIX shell:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/reprotrace --version
.venv/bin/reprotrace run examples/tiny/reprotrace.yaml
```

The final command prints a `bundle:` path. Copy that exact path and inspect the
directory. It should include the resolved manifest, source and environment
records, declared input and artifact records, command records, logs, raw metric
source bindings, derived metrics, `evidence.index.json`, `verification.json`,
and `report.md`.

Re-check or regenerate the presentation using the printed path:

```text
reprotrace verify <bundle-path>
reprotrace report <bundle-path>
```

If you did not activate the environment, replace `reprotrace` with
`.\.venv\Scripts\reprotrace.exe` on Windows or `.venv/bin/reprotrace` on POSIX.
Use `reprotrace verify <bundle-path> --json` to print the complete canonical
verification object.

## 2. Try one real, safe workflow (about 5 minutes)

Start from [`examples/tiny/reprotrace.yaml`](../examples/tiny/reprotrace.yaml)
and make a working copy for a small public or sanitized workflow. Keep the
current manifest interface and change only what describes your run:

- `project.name` and `project.root`;
- the `claim` locator and honest protocol (`strict` or `approximate`);
- declared `inputs`;
- `run.output_root`, `seed`, and each step's `argv`, optional `cwd`/`env`,
  timeout, and declared artifacts;
- CSV or log-regex metric definitions, including any expected value and
  tolerance you genuinely intend to evaluate.

Commands must remain argv arrays; do not turn them into shell command strings.
The supported placeholders are demonstrated by the checked-in fixtures,
including `{python}`, `{project_root}`, `{run_dir}`, and `{seed}`. Put a relative
`run.output_root` either outside the audited Git worktree or at a path ignored by
that worktree. ReproTrace rejects an unignored evidence output path inside the
captured worktree so recorder output cannot silently dirty the source it is
describing.

Before executing a real manifest, preflight it:

```text
reprotrace run --dry-run <path-to-reprotrace.yaml>
```

The dry-run records a resolved plan and checks required inputs but does not run
the declared steps. Read its summary and bundle before running the short CPU
workflow:

```text
reprotrace run <path-to-reprotrace.yaml>
```

Do not continue if the resolved command, project root, input paths, output root,
or redaction boundary is not what you expected.

For a controlled stale-evidence check, copy a disposable bundle, change one
indexed text artifact or raw metric source in the copy, and run `verify` on that
copy. Record whether the failure is understandable. This checks bundle-local
integrity; it does **not** test whether the original project has changed. To
compare two actual project states, create a new run after an intentional safe
source/config/input change and use:

```text
reprotrace diff <first-bundle-path> <second-bundle-path>
```

`diff` exits `1` when the bundles differ, which is an expected comparison result
rather than a verification failure.

## 3. Compare with Git commit/SHA + manifest (about 3 minutes)

For the same workflow, write down what your normal baseline provides: at
minimum, the Git commit/SHA and the manifest or config you would retain. Do not
improve the baseline after seeing the ReproTrace bundle. Compare how quickly a
future reviewer could answer each question from the two records:

- Was the worktree clean, and if not, which tracked changes and untracked paths
  existed?
- Which Python and installed package versions were observed?
- Which input paths and hashes, resolved argv, working directory, redacted
  overrides, return codes, and logs belong to this result?
- Which artifacts and raw metric sources produced the reported metric?
- Can the metric be re-extracted from bundle-local evidence, and does the
  declared tolerance decision still agree?
- Can two runs be compared without manually assembling all of those fields?

The baseline may already answer some or all of these well. That is a useful
pilot result; the purpose is to measure incremental value, not assume it.

## 4. Record the outcome and send feedback (about 2 minutes)

Capture concrete observations rather than a general impression:

- minutes spent installing, adapting the manifest, running, and interpreting;
- setup friction and any unclear message or field;
- stale-result or evidence-binding problems found, including what evidence made
  the problem visible;
- false positives: warnings or failures that did not represent a real problem;
- false negatives: a known binding or staleness problem ReproTrace missed;
- value added over Git commit/SHA + manifest, or why there was none;
- whether you would use the bundle again and the smallest improvement that would
  change your answer.

Submit the sanitized summary through the
[Real-world ReproTrace pilot form](https://github.com/YaoYang-123456/ReproTrace/issues/new?template=real-world-pilot.yml).
You do not need to attach code, data, logs, or an evidence bundle.

## What the result can and cannot establish

Depending on the reported assurance level and coverage, ReproTrace can establish
that captured bundle-local bytes match the indexed size and SHA-256 closure,
that schema-1 command and artifact declarations agree with the resolved
manifest, and that captured metric derivations were re-extracted and checked.
It can record a command outcome and evaluate a declared expected value and
tolerance. A `recorded_success` value is still a producer-supplied record.

ReproTrace does not establish producer or execution authenticity, a trusted
timestamp, signature or attestation, independent replay, scientific validity,
or paper reproduction. An internally coherent forged bundle can pass. It also
does not automatically check current source, data, or artifacts outside the
captured bundle, and a successful process exit is not evidence that a scientific
claim is true.
