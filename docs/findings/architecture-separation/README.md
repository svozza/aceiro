# Architecture-separation result sources

Cross-arm reports are generated from native result records owned by each arm
repository. This directory does not copy those records.

`sources.lock.json` pins:

- the canonical result schema;
- the exact result commit for each arm repository;
- the result index path and its SHA-256.

Report generation must:

1. fetch each repository at the pinned commit;
2. verify the result-index hash;
3. validate every referenced record against the pinned schema;
4. check internal count invariants;
5. derive experiment-centric comparisons from per-cell dimensions;
6. keep scored, excluded, and structural-N/A outcomes separate.

Updating an arm result requires a new arm commit and a reviewed lock update.
Never edit a native arm record from this directory.

See ADR 0020, "Arm repositories own native experiment results".
