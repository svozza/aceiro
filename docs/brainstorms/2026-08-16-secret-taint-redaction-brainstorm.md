---
date: 2026-08-16
topic: secret-taint-redaction
---

# Secret Taint Redaction

## What We're Building

Detect known-format and high-entropy secret candidates in contributor-controlled
content before model invocation. Replace candidates with stable placeholders that
retain type and approximate length, keep plaintext only in trusted process memory,
and reject any generated artifact that reproduces an original value.

## Why This Approach

`detect-secrets` is Python-native, Apache-2.0, exposes an embedding API, and
supports entropy and custom detector plugins. It provides detection; smtithy
retains ownership of enforcement and never persists plaintext candidates.

## Key Decisions

- Detection: use `detect-secrets` built-ins plus a smtithy generic quoted/assigned
  high-entropy detector.
- Model input: replace candidates with stable placeholders such as
  `<SECRET_1:type=high_entropy,length=20>`.
- Output gate: reject exact candidate values across every rendered artifact
  representation before posting.
- Persistence: hashes and metadata may be logged; plaintext candidate values may
  not be written to transcripts, baselines, or artifacts.
- False positives: allowlist common hashes, UUIDs, test fixtures, lockfiles, and
  explicitly configured public identifiers.
- Existing regex policy: retain it as an independent backstop.

## Open Questions

- Initial entropy thresholds and allowlist tuning will be calibrated through
  deterministic fixtures and later real-repository observation.

## Next Steps

- Implement detection and placeholder substitution in trusted context preparation.
- Carry the in-memory taint set to artifact verification.
- Add deterministic tests covering detection, redaction, exact-value rejection,
  allowlists, and non-persistence.
