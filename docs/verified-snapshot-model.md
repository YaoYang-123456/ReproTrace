# Verified snapshot model

Stage 6.2 introduces a verifier-private logical snapshot so that later
verification, metric extraction, and reporting can consume the same bytes that
were checked against one captured `evidence.index.json`. Stage 6.2a defines the
ownership and lifecycle model only. It does not acquire live bundle files and
does not yet change verification behavior.

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
  handle-bound implementation;
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

## Assurance boundary

The eventual invariant is that canonical verification semantics and report
content consume retained snapshot representations, never reopened live bundle
paths. Stage 6.2a does not yet establish that invariant in the production
verifier. It adds no assurance level, schema field, evidence-root change, CLI
behavior, or report behavior.

The model claims one coherent logical byte snapshot described by one captured
index. It does not claim a filesystem-atomic whole-directory snapshot or that
all pathnames physically coexisted at one instant. Producer authenticity,
trusted execution, signing, attestation, independent replay, and scientific
reproduction remain not established.

Deferred to separately approved substages are live-file handle acquisition,
pre/post-open identity checks, symlink or junction race defenses, index-wide
snapshot construction, snapshot-backed metric extraction, verifier/report
integration, and bundle-root identity checks before writing derived outputs.
Consequently, the known verifier-time TOCTOU issue is **not fixed by Stage
6.2a**.
