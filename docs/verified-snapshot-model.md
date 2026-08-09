# Verified snapshot model

Stage 6.2 introduces a verifier-private logical snapshot so that later
verification, metric extraction, and reporting can consume the same bytes that
were checked against one captured `evidence.index.json`. Stage 6.2a defines the
ownership and lifecycle model. Stage 6.2b adds a separately reviewable
single-file acquisition primitive. Stage 6.2c composes those pieces into a
complete schema-1 logical snapshot builder. None of these stages changes
production verification behavior yet.

## Objects and ownership

`VerifiedEvidenceObject` represents one indexed evidence entry. Its canonical
bundle-relative path, sorted roles, expected fingerprint, observed fingerprint,
retention kind, acquisition state, and diagnostics are held separately from any
live origin path. Acquisition input is bytes-like data supplied by a later
acquisition layer; the object itself never reopens the bundle path.

The three retention kinds are:

- `memory`: exact bytes are converted to an immutable `bytes` payload when
  acquisition finishes;
- `spool`: bytes are retained in a verifier-owned
  `tempfile.SpooledTemporaryFile`; readers use the already-open file-like object
  and never a temporary filename;
- `integrity_only`: size and SHA-256 validation state is retained, but semantic
  contents are discarded and no reader is available.

Semantic readers are available only after an object is successfully acquired,
validated, and sealed. Each reader starts at byte zero. Spool readers have
independent logical positions over the same owned spool. The normal model API
does not permit replacing payload, expected fingerprint, storage kind, or state
after sealing.

`VerifiedBundleSnapshot` holds:

- display-only bundle-root metadata and a root-identity placeholder for a later
  handle-bound root identity;
- exact captured canonical `evidence.index.json` bytes and its parsed record;
- the candidate evidence root;
- indexed objects keyed by canonical bundle-relative path;
- defensive-copy parsed core-record cache entries;
- acquisition and sealing state;
- cleanup diagnostics.

Duplicate paths cannot overwrite an object. Snapshot acquisition completes only
when every indexed path is present and every object has a matching observed
size/SHA-256. Snapshot sealing additionally requires every object to be sealed.

`VerificationSession` is the explicit lifetime owner. It can be used as a
context manager and closes all retained spool resources deterministically.
Repeated close is safe. Cleanup failures are recorded as operational
diagnostics; they do not retroactively alter fingerprint validation or an
already-established logical evidence root. A closed spool object rejects both
new readers and reads through previously created views.

## Candidate root and established root

The evidence-root formula is unchanged:

```text
candidate_root = SHA256(canonical evidence.index.json bytes)
```

Parsing a canonical index only creates a candidate. The snapshot does not expose
that value as `established_evidence_root` until every index entry has been
successfully acquired and validated and the complete snapshot has been sealed.
A missing, failed, unvalidated, unacquired, or unsealed object prevents root
publication.

`evidence.index.json` remains special because it is not self-indexed. A later
acquisition stage will read its exact bytes once. Bootstrap `run.json` bytes must
also eventually be acquired once and then validated against that captured
index; Stage 6.2a only provides the model needed for that sequence.

## Handle-bound single-file acquisition

Stage 6.2b adds `acquire_bundle_evidence()` for one object that has already been
claimed by a `VerifiedBundleSnapshot` owned by a `VerificationSession`. This
ownership-before-acquisition order ensures that failed memory/spool acquisition
still falls under deterministic session cleanup.

`FileIdentity` is an immutable structured value containing a mechanism,
`st_dev`, `st_ino`/file index, and file type. Paths and timestamps are not
identity. Boolean values and non-positive file IDs are rejected. Platforms that
do not expose a usable identity produce an explicit unavailable identity, and
acquisition fails closed rather than falling back to pathname-only assurance.
`BundleRootIdentity` wraps the corresponding directory identity.

The single-file sequence is:

1. normalize the bundle-relative path using the existing canonical rules;
2. resolve the bundle root and compare its current structured directory identity
   with the session identity;
3. perform bundle containment, final-symlink, regular-file, and pre-open identity
   checks;
4. call `os.open` once with read-only, non-inheritable binary flags, adding
   `O_CLOEXEC` and `O_NOFOLLOW` where available;
5. `fstat` that descriptor, require a regular file and compare its structured
   identity with the pre-open identity;
6. recheck root identity, containment, final-component safety, and candidate
   identity before reading any semantic byte;
7. use bounded `os.read(descriptor, chunk_size)` calls and send each chunk once
   through `VerifiedEvidenceObject`, where the same bytes drive size, SHA-256,
   memory retention, spool retention, or integrity-only accounting;
8. finish fingerprint validation and close the descriptor in `finally`.

There is no path-based hash, `read_bytes()`, semantic reopen, automatic retry,
or spool-to-memory fallback. Identity uncertainty before streaming consumes zero
semantic bytes. Read, retention, descriptor, or fingerprint failures leave the
already-owned object in `FAILED`; partial contents cannot be sealed.

Pre/open/post identity comparison catches deterministic final-component swaps,
replacement by another in-bundle file, parent symlink/junction redirection, and
bundle-root replacement. POSIX uses `O_NOFOLLOW` when available as an additional
defense. Windows relies on the same pre/open/post `stat`/`fstat` identity and
containment checks when `O_NOFOLLOW` is unavailable. A real Windows junction
replacement test exercises this path. These checks do not claim defense against
arbitrary high-frequency ABA attacks or hostile multi-user filesystem control;
full component-by-component `openat` walking and native `CreateFileW` wrappers
remain outside the approved design.

## Schema-1 bootstrap and index-wide construction

Stage 6.2c adds the internal `open_schema_one_snapshot()` builder. It captures
the bundle-root identity once and reuses that expected identity for every
bootstrap and indexed acquisition. The builder returns an already-open
`VerificationSession`; the snapshot is complete and sealed, while its private
spool resources remain usable until the caller exits the session context.

`run.json` is the schema bootstrap and is therefore special. The builder
handle-captures its exact bytes once into immutable memory, parses only those
bytes to determine the schema, and stops without opening the index for schema
0. For schema 1 it next handle-captures `evidence.index.json` once. The index is
strict UTF-8 JSON, rejects non-finite numbers, must satisfy the existing index
normalization, and its captured bytes must exactly equal canonical index bytes.
Because the index is intentionally not self-indexed, it is retained as snapshot
metadata rather than an indexed evidence object.

After the canonical index exists, the already-captured `run.json` bytes are
bound to its declared entry, fingerprinted, sealed, and cached without reopening
the live path. A mismatch fails closed. Every other index entry is then acquired
exactly once in canonical path order through the Stage 6.2b handle-bound engine.
The classifier is deliberately narrow:

- the nine core semantic filenames use `memory`;
- a non-core entry carrying the existing `metric_source` role uses `spool`;
- every other entry uses `integrity_only`.

Core JSON and resolved YAML records are parsed only through retained snapshot
readers. The parsed-record cache contains `run.json`, `source.json`,
`environment.json`, `inputs.json`, `commands.json`, `artifacts.json`,
`metrics.json`, `manifest.resolved.yaml`, and `metric_sources.json`. Invalid
UTF-8, malformed JSON/YAML, non-finite JSON values, or an incorrect top-level
container abort construction before the snapshot can be completed or sealed.

Only after all indexed entries have matching fingerprints, every object is
sealed, and all required core records are cached does the builder complete and
seal the snapshot. The established root remains:

```text
SHA256(exact canonical captured evidence.index.json bytes)
```

This is a same-indexed-logical-byte snapshot, not a filesystem-atomic directory
snapshot. Once construction succeeds, cached core records and retained memory
or metric-source spool bytes are independent of the live bundle and its producer
location. Integrity-only payload bytes are intentionally unavailable to semantic
consumers. Root or file identity instability causes one fail-closed attempt;
there is no retry, restart, fallback, or switch to a replacement root.

## Snapshot-backed metric extraction

Stage 6.2d adds the internal `extract_metrics_from_snapshot()` path. It accepts
an already-open `VerificationSession`; it does not create or close a hidden
session. The snapshot must be active, complete, sealed, and have an established
root. The resolved manifest and `metric_sources.json` are obtained only from the
Stage 6.2c parsed-record cache, never from bundle paths or producer metadata.

The captured resolved manifest is validated with the existing manifest rules.
Its metric IDs must have exact missing/extra closure with the ordered metric
records in cached `metric_sources.json`; output order follows the manifest.
Within each metric, source order remains the explicit ordinal order recorded by
`metric_sources.json`, independent of index path sorting.

Each `evidence_path` is bound to an existing, open, sealed snapshot object with
the `metric_source` role and semantic retention. The size and SHA-256 declared
by `metric_sources.json` must equal the object's index-bound expected
fingerprint, and its handle-acquired observed fingerprint must still equal that
expected value. No live stat or hash is performed for this cross-record closure.

CSV and log-regex extraction share one reader-based parsing layer with the
legacy Path adapter. Snapshot readers are opened afresh and closed
deterministically for each source and metric, so multiple metrics may consume
the same retained spool without sharing reader position. CSV remains strict
UTF-8 with existing column, empty-field, numeric, finite-value, and selector
semantics. Regex remains UTF-8 with `errors="replace"`, existing match/group
order, and the same finite numeric conversion. Derived record fields and
`last`/`min`/`max` selectors are unchanged.

`origin_path` is compatibility/provenance display metadata only. Snapshot
extraction never opens, resolves, stats, hashes, or requires it, and it never
falls back to a live `evidence_path`. Consequently metric derivation continues
to work after live metric files or the original producer location are deleted,
replaced, redirected, made unreadable, or relocated, provided the owning
session remains open.

The legacy path-backed metric APIs remain available. Stage 6.2d introduced this
consumer independently; Stage 6.2e now uses it for schema-1 production
verification while leaving producer-time extraction path-backed.

## Production verification and report session

Stage 6.2e dispatches by attempting `open_schema_one_snapshot()` first. A
`SchemaOneSnapshotNotApplicable` result alone permits a second, legacy schema-0
read. A malformed, unstable, incomplete, or fingerprint-mismatched schema-1
snapshot fails closed and is never retried through the legacy verifier. Thus a
schema-1 `run.json` is captured once during bootstrap rather than pre-read for
dispatch and opened again.

`verify_snapshot_session()` accepts one active, complete, sealed session with an
established evidence root. It obtains all core semantic records only from the
snapshot parsed cache, applies the existing source, manifest, protocol, closure,
assurance, and result validations to those values, and projects `bundle:index`
and `bundle:file:*` checks from expected and handle-observed snapshot
fingerprints. Source patch/status checks bind their metadata to sealed objects
with the `source_evidence` role; they do not reopen integrity-only payloads.
Metric recomputation calls `extract_metrics_from_snapshot()` once when
derivation is applicable. Dry-run and zero-metric behavior is unchanged.

Report formatting is split into a pure `render_report()` function and an output
adapter. Schema-1 report records come from the same session cache that produced
the verification result. CLI `verify`, CLI `report`, and runner finalization use
one operation-local session for verification, report rendering, and derived
output writes. A standalone `generate_report()` invocation establishes a fresh
session; a verification mapping without its active session is not sufficient
schema-1 report authority. Legacy schema 0 remains path-backed and does not
require an index or session.

`verification.json` and `report.md` remain derived, regenerable, unindexed
outputs and do not participate in the evidence-root formula. For each serialized
write-intending invocation, C5.1a captures the named bundle-root identity and
guard-invalidates historical `verification.json` followed by historical
`report.md` before schema dispatch or snapshot establishment. The first file is
the primary canonical derived record; the report is dependent presentation from
the same refresh. A final symlink is unlinked without following its target;
directories, junctions/reparse-like objects, and unsupported special objects
fail closed.

The lifecycle identity is checked before and after mutation. For schema 1 it
must also exactly match the session-captured root identity before publication,
and the existing session-bound writers retain their per-write root checks.
Schema 0 uses the same mutation guard without gaining snapshot assurance.
`verify_bundle(write=False)` acquires no lifecycle mutation authority and leaves
both canonical files unchanged on success or failure.

After successful invalidation, a snapshot, verification, rendering, or
verification-publication failure leaves both outputs absent. Report publication
failure after successful verification publication leaves fresh verification and
no report; there is no rollback. A report surviving without current canonical
verification is historical/orphaned output, not a current report, and file
existence alone does not prove that the latest invocation succeeded. The writes
remain atomic sibling-temporary single-file writes, not a two-file transaction.
Concurrent write-intending operations on the same bundle are outside the pair
consistency guarantee and must be serialized externally.

## Assurance boundary

For schema 1, canonical production verification semantics and report content now
consume retained snapshot representations and do not reopen live evidence after
snapshot establishment. Stage 6.2e changes the internal production authority
and lifecycle, but adds no assurance level, schema field, evidence-root change,
command or metric-source schema change, or CLI flag.

The model claims one coherent logical byte snapshot described by one captured
index. It does not claim a filesystem-atomic whole-directory snapshot or that
all pathnames physically coexisted at one instant. Producer authenticity,
trusted execution, signing, attestation, independent replay, and scientific
reproduction remain not established.

The root check is not a filesystem transaction and does not claim protection
against arbitrary high-frequency ABA between the final identity comparison and
the atomic replace. Component-by-component ancestry locking, native Windows
share-mode hardening, hostile multi-user filesystem resistance, producer
authenticity, trusted execution, signing, attestation, replay, and scientific
reproduction remain outside this stage.

H1 production implementation was candidate-closed by Stage 6.2e and was finally
closed by the human-approved Stage 6.2f adversarial cross-platform acceptance.
C5.0 is accepted and merge authorization is granted.
