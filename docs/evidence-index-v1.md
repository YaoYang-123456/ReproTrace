# Canonical evidence index v1

Stage 2 defines bundle-safe path and evidence-index primitives. It does not yet
connect the existing runner or verifier to a complete indexed evidence closure,
so current production bundles remain at assurance level `recorded`.

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

## Stage boundary

The Stage 2 API can build, atomically write, read, and validate an index. It does
not make the current runner emit an index, does not migrate legacy bundles, and
does not raise assurance to `bundle_integrity_checked`. That upgrade requires a
future production verification path to prove that its complete required evidence
closure and every verifier dependency are indexed and verified.

Raw metric source snapshots and metric re-extraction are Stage 3 concerns and
are intentionally absent here.
