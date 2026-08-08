# C5.0 Stage 6.2f final adversarial acceptance

## Gate status

- Authoritative production base: `d189e470f522ea4b4fd14a95777f3a98be3e3ef1`.
- Acceptance test commit: `d7a4cd4edafa0d2702e1d2707af10f53d0da9315`.
- First attempt: local **PASS**, CI **FAIL** before test execution because the
  acceptance module depended on an implicit `tests` namespace-package import.
- Portability-repair rerun: local **PASS** on Windows 3.13.9, including direct
  `pytest`, `python -m pytest`, and cp1252 full-suite runs.
- Cross-platform rerun gate: **pending** new GitHub Actions and human review.
- H1 final closure: pending the cross-platform gate.
- Merge authorization: withheld. This stage did not merge or create a pull request.
- Production source freeze: no file under `src/reprotrace/` changed.

This is an acceptance result for one logical indexed-byte snapshot. It is not an
authenticity proof, signature, attestation, trusted-execution claim, independent
replay, or scientific-reproduction claim.

## Threat model and authority

For schema-1 verification, one operation is required to remain bound to one
captured `run.json`, one exact canonical `evidence.index.json` byte sequence, one
index-defined set of logical evidence bytes, one set of handle-acquired
fingerprints/retained bytes, and one active `VerificationSession`.

The accepted boundary permits filesystem changes after snapshot establishment.
Semantic verification and report rendering must continue from retained snapshot
state. Before each unindexed derived write, the named bundle root must still have
the structured identity captured by the session. The evidence root remains
`SHA256(exact captured canonical evidence.index.json bytes)`.

The model does not promise an atomic directory transaction, hostile multi-user
filesystem protection, arbitrary high-frequency ABA protection, native Windows
`CreateFileW` ancestry/share locking, producer authenticity, signing, replay, or
scientific reproduction.

## H1 snapshot-identity result

H1 passed the local adversarial gate:

- Replacing the live metric source A with B after sealing left the recomputed
  value at A (`3.0`), not B (`88.0`), with the state-A evidence root.
- Replacing live `metrics.json` and the canonical index with state B did not
  affect verification or report semantics and did not cause an index reread.
- Mutating live `commands.json`, `source.json`, and `artifacts.json` did not
  replace their cached state-A authority.
- Deleting/replacing `source.patch` and `source.status` after acquisition left
  source checks bound to the acquired objects.
- Mutation between verification and report rendering produced an internally
  consistent state-A report and the same evidence root.
- Post-snapshot instrumentation rejected any attempted use of path-backed JSON,
  source, index, resolver, hasher, or `Path` content-read APIs; production verify
  and report still completed.
- `run.json` and `evidence.index.json` were each opened exactly once; every
  indexed evidence path was acquired exactly once; `run.json` was not reacquired
  and the index was not self-indexed.
- Verification and report rendering remained usable after all indexed live files
  and the live index were removed while the bundle root itself remained stable.
- A large metric source exceeded the production 8 MiB spool threshold, remained
  derivable after live deletion, supplied independent readers starting at offset
  zero, and became inaccessible after session close. Failure after acquisition
  also closed the retained spool.

On POSIX CI, a real post-snapshot final symlink to outside state B is mandatory.
This local Windows account lacks ordinary symlink privilege; Windows instead ran
the structured post-open identity mismatch and real parent-junction regressions.

## Root identity and derived outputs

- Real root rename/replacement before `verification.json` write failed closed;
  replacement root B received neither derived output.
- Real root replacement after verification write but before report write left the
  completed verification in parked root A, failed the report write, and wrote no
  `report.md` into replacement B.
- Simulated structured root-identity unavailability failed closed.
- With an unchanged root, verification and report writes completed atomically.
- Repeated verify/report/report-regeneration left exact index bytes and the
  evidence root unchanged. `evidence.index.json`, `verification.json`, and
  `report.md` were absent from index entries.

## H2 command-protocol result

H2 passed the local gate with byte-self-consistent, reindexed mutations:

- `requested_argv`, resolved `argv`, `cwd`, `environment_overrides`,
  `timeout_seconds`, `step_id`, and stdout/stderr evidence paths could not be
  changed while retaining command protocol closure or integrity assurance.
- Actual command logs could not be deleted and rebound to unrelated indexed
  metric/artifact evidence.
- `return_code=false` was rejected as an invalid boolean alias for zero.
- Impossible completed/failed/timeout/launch-error state combinations failed
  canonical protocol semantics.
- A consistently reindexed `commands.jsonl` mutation did not change semantic
  command authority; `commands.json` remained authoritative.

## H3 artifact-membership result

H3 passed the local gate:

- A wildcard artifact declaration could not be rebound to an unrelated indexed
  log while retaining `artifact:bundle-scope-closure`.
- Canonical POSIX segment behavior was exercised for `*`, `?`, `[]`, and a full
  `**` segment. Ordinary segment wildcards did not cross `/`.
- Duplicate, extra, and missing artifact declarations failed canonical closure.
- A declared wildcard with zero matches remained legal where current manifest
  semantics permit it.

## Numeric and parser hardening

The final suite retained fail-closed behavior for boolean `expected`, `atol`, and
`rtol`; NaN and positive/negative Infinity; negative tolerances; non-finite CSV
values; and non-finite regex values. Strict JSON evidence was not relaxed.

## Canonical index, relocation, and compatibility

- The established root exactly matched SHA-256 of captured canonical index bytes.
- A noncanonical serialization, a missing `run.json` entry, a later evidence
  fingerprint mismatch, and root replacement during acquisition all failed
  before an evidence root could be established; there was no restart.
- Existing Stage 6.2b regressions retained pre-open final replacement and real
  Windows parent-junction coverage.
- A copied bundle verified at a new location after producer/original deletion.
  Missing origin metadata paths were not used for schema-1 I/O.
- A schema-1 dry run remained `complete / bundle_integrity_checked / not_run /
  not_evaluated`; zero declared metrics remained legal without invented
  derivation.
- Schema-0 verify/report remained path-backed compatibility behavior with
  `recorded / not_evaluated` and no fake evidence root.
- A malformed schema-1 snapshot did not enter the legacy path.
- Stable schema/result/coverage/compatibility fields and canonical plus legacy
  check IDs, order, and results matched runner finalization and standalone
  production verification after excluding only `verified_at`.

The extra local probe `reprotrace verify <dry-run-bundle>` regenerated complete
evidence but returned CLI code 1 because the existing `verify` subcommand applies
the non-dry execution exit policy. `run --dry-run` and `report` both returned 0.
Stage 6.2f did not change this pre-existing CLI behavior because no acceptance
condition requires standalone dry-run verification to use preflight exit policy.

## Real tiny CPU acceptance

Executed bundle:
`.reprotrace/runs/20260808T163948Z-dd02ff`

Runner finalization, standalone verify, report, and report regeneration all
recorded:

- verification: `complete`
- assurance: `metric_derivations_recomputed`
- execution: `recorded_success`
- result: `matched`
- index SHA-256 and evidence root:
  `69246ca34cf0009e66553c242cfb92a54a818c2f0aa2395541b93f8abfde0460`

The metric was recomputed from retained snapshot evidence and derived outputs
remained unindexed.

Dry-run bundle:
`.reprotrace/runs/20260808T164019Z-fde2b9`

Runner finalization and report regeneration recorded:

- verification: `complete`
- assurance: `bundle_integrity_checked`
- execution: `not_run`
- result: `not_evaluated`
- index SHA-256 and evidence root:
  `68026940a2ce90a98ea5117555aa294d3b452f68af8260132b7695fe326cf318`

No GPU, PEFT-ViT, training, network, data download, or model download was used.

## Original independent audit fixture

The original package was found and inspected read-only:

- `reprotrace_c5_final_audit_fixtures.py`
- `reprotrace_c5_final_audit_fixture_results.json`

It could not execute directly on this Windows checkout without alteration because
it hardcodes `/mnt/data/ReproTrace-audit/src`, creates and writes under
`/mnt/data`, and its H1 races patch the pre-Stage-6.2e path-backed
`validate_evidence_index`/`resolve_bundle_file` flow. The package was not edited
or redirected to an invented compatibility path.

Equivalent current regressions are:

| Original fixture property | Current regression |
| --- | --- |
| command protocol mutation / bool zero alias | final H2 protocol and invalid-state parameter sets; Stage 6.1 assurance verifier |
| command-log role rebound | final unrelated-indexed-log test; Stage 6.1 role closure |
| wildcard artifact rebound | final H3 wildcard test; Stage 6.1 wildcard closure |
| input coverage rebound | Stage 4.1 manifest declaration closure tests |
| `commands.jsonl` opacity | final archive-authority test; Stage 6.1 regression |
| invalid command status | final invalid-state test; Stage 6.1 schema regression |
| hash-A / derive-B race | final production A-to-B test and Stage 6.2e integration tests |
| outside-symlink race | Stage 6.2b handle-bound POSIX/Windows identity tests and final POSIX post-snapshot test |
| bool/non-finite numeric values | final numeric matrix and Stage 6.1 manifest/metric tests |

The package's path-normalizer probes were also executed read-only against the
current import. Their current POSIX-logical values remain unchanged; they are not
an H1/H2/H3 gate and do not establish immunity to every native Windows reserved
name or alias.

## Local validation counts

| Suite | Result |
| --- | --- |
| Stage 6.2f final adversarial suite | 52 passed, 1 skipped |
| Stage 6.2e production integration | 37 passed |
| Stage 6.2d snapshot metrics | 37 passed, 1 skipped |
| Stage 6.2c snapshot builder | 28 passed |
| Stage 6.2b acquisition | 17 passed, 4 skipped |
| Stage 6.2a snapshot model | 16 passed |
| Stage 6.1 assurance/protocol | 93 passed |
| CLI/reporting | 16 passed |
| runner/end-to-end | 11 passed |
| full direct `pytest` suite | 390 passed, 10 skipped |
| full `python -m pytest` suite | 390 passed, 10 skipped |
| full `python -X utf8=0` (`cp1252`) | 390 passed, 10 skipped |
| `git diff --check` | passed |
| `compileall src/reprotrace tests` | passed |

The first full-suite invocation incorrectly placed `--basetemp` under this Git
worktree and therefore invalidated the non-Git-directory fixture by giving it a
Git ancestor. No code changed in response. Both authoritative full runs used a
new external non-Git basetemp and passed.

## Platform matrix and skips

Local Windows results:

- real parent-junction acquisition, evidence-index, and source-evidence cases:
  passed;
- real root replacement before both derived writes: passed;
- structured post-open identity mismatch: passed;
- ordinary file/directory symlink creation: unavailable (`WinError 1314`);
- renaming an already opened regular file: unavailable under Windows `os.open`
  sharing semantics; structured identity mismatch supplies deterministic
  equivalent rejection.

The ten local full-suite skips were seven ordinary-symlink-unavailable cases
(the final suite, acquisition pre-open final component, evidence-index final and
parent components, snapshot metric redirect, and source-evidence final and
parent components), two Windows opened-file rename-sharing cases, and one
POSIX-parent-symlink case whose Windows junction equivalent ran. Real Windows
junction and root-replacement tests were not skipped.

Ubuntu 3.10/3.12 and macOS 3.12 must execute the applicable real POSIX final and
parent symlink, rename/replacement, root replacement, and large-spool cases in
GitHub Actions. A skipped mutation will not be described as tested.

## CI and final recommendation

The authoritative Stage 6.2e base CI is green on Ubuntu 3.10, Ubuntu 3.12,
Windows 3.12, and macOS 3.12. The first Stage 6.2f acceptance commit
`d7a4cd4edafa0d2702e1d2707af10f53d0da9315` passed locally but failed in
[GitHub Actions run 31268142340](https://github.com/YaoYang-123456/ReproTrace/actions/runs/31268142340).
Ubuntu 3.12 reported `ModuleNotFoundError: No module named 'tests'` while
collecting the final acceptance module; macOS 3.12 also failed during pytest
collection, and fail-fast cancelled Windows 3.12 and Ubuntu 3.10. No adversarial
case executed in that failed cross-platform gate.

The test-portability repair adds only an empty `tests/__init__.py`, making the
existing cross-test helper imports explicit. The final adversarial test file is
byte-identical to the failed-attempt commit: no test body, fixture mutation,
assertion, skip condition, or adversarial sequence changed. Setuptools package
discovery remains rooted at `src`; from outside the repository the editable
installation exposes `reprotrace` but no `tests` package.

Before repair, direct `pytest` reproduced the collection error locally while
`python -m pytest` and a repository-root Python import happened to resolve the
implicit namespace. After repair, both final-suite entry forms returned
`52 passed, 1 skipped`, a clean subprocess import succeeded without `PYTHONPATH`,
and all three complete suites returned `390 passed, 10 skipped`. A new
cross-platform Actions run is still pending the repair commit and push.

Local recommendation: retain `H1 final closure pending cross-platform gate` and
withhold merge authorization until the repaired Stage 6.2f GitHub Actions matrix
is green and a human confirms the test-only repair commit.
