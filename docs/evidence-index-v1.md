# Canonical evidence index v1

Stage 2 defines bundle-safe path and evidence-index primitives. Stage 4 connects
them to new run schema 1: the runner indexes the verifier's authoritative
dependency closure, and the verifier checks both every indexed file and exact
closure membership before granting bundle-integrity assurance.

## Bundle-local paths

Every evidence key is a normalized POSIX-style path relative to the resolved
bundle root. The resolver rejects empty paths, POSIX and Windows absolute paths,
drive and UNC paths, parent traversal, final symlinks, non-regular files, and any
path whose resolved target escapes through a parent symlink or junction. Paths
are checked before evidence bytes are read or hashed.

Origin paths and host-specific absolute paths are metadata, not evidence keys.

## Index schema

`evidence.index.json` uses schema 1:

```json
{
  "schema_version": 1,
  "entries": [
    {
      "path": "logs/example.stdout.log",
      "roles": ["command_log"],
      "size_bytes": 123,
      "sha256": "<lowercase SHA-256 hex>"
    }
  ]
}
```

Entries are sorted by normalized path and duplicate normalized paths are
rejected. Roles are de-duplicated and sorted. Only the documented root and entry
fields are accepted. These self-derived files are excluded to avoid cycles:

- `evidence.index.json`
- `verification.json`
- `report.md`

## Canonical bytes and evidence root

Canonical bytes are UTF-8 JSON with sorted object keys, separators `,` and `:`
without extra whitespace, sorted entries, sorted roles, and no trailing newline.
They contain no timestamp, host path, or other unstable field.

The evidence root is defined as:

```text
evidence_root_sha256 = SHA-256(canonical evidence.index.json bytes)
```

It is an evidence snapshot identifier only. It is not a signature, authenticity
proof, trusted digest, attestation, producer identity, or trusted timestamp. If
an attacker can replace both evidence and the index, an externally trusted root
is required to detect that replacement; trusted roots are outside Stage 2.

## Production closure

For run schema 1, the closure contains core records, referenced source status and
patch files, attempted-command stdout/stderr logs, bundle-local inputs and
artifacts, and ordered raw metric source evidence. Duplicate references merge
their roles; for example, one metrics CSV can be both `artifact` and
`metric_source`. Files not referenced by an authoritative record are not added
merely because they exist in the directory.

The verifier derives the same expected closure independently from bundle
records, requires exact path and role agreement, validates every indexed byte,
and independently requires those input and artifact records to correspond
exactly to the declarations in `manifest.resolved.yaml`. This second closure
prevents deletion or insertion of a record from being hidden by rebuilding the
index. Bundle artifacts declared with `{run_dir}` cannot be reclassified as
external to evade index membership. The verifier emits `evidence_root_sha256`
only when the complete integrity contract passes. The root remains a snapshot
identifier, never an authenticity proof. Legacy run schema 0 is not migrated or
upgraded.
