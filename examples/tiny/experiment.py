"""Deterministic CPU-only experiment used to exercise ReproTrace."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()

    source = Path(args.input)
    values = [float(line) for line in source.read_text(encoding="utf-8").splitlines() if line]
    random.seed(args.seed)
    score = sum(values) / len(values)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["seed", "score", "count"])
        writer.writeheader()
        writer.writerow({"seed": args.seed, "score": f"{score:.6f}", "count": len(values)})

    metadata = Path(args.metadata)
    metadata.write_text(
        json.dumps(
            {
                "seed": args.seed,
                "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "score": score,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"score={score:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
