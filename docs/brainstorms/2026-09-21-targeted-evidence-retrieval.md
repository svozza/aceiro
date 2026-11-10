# Targeted evidence retrieval

The reporting phase completes reliably at low effort, but saved reports still
guess dependency behavior and miss defects when a necessary contract is absent.
Further wording changes did not establish a quality gain.

Add an opt-in lookup tool to the actual reporter. It may resolve two specific
gaps using repository source/documentation or dependency archives pinned by
the reviewed lockfile. The existing reporting deadline, output cap, model and
submission verifier remain unchanged. Stop accepting lookups while at least
90 seconds remain for final reporting; lookups do not restart investigation.

Use literal search with bounded context, explicit omissions, file hashes and
revision/package provenance. Read dependency archives as data only, after hash
verification. Initially support Python `uv.lock` and Rust `Cargo.lock`, which
cover the two existing large fixtures. Repository lookups are language-neutral.
Network requests are limited to fixed public artifact hosts; contributor URLs
cannot redirect the process or supply authentication. No model shell access,
package installation, archive extraction or arbitrary browsing is added.

A general browser or unrestricted source-tool restart would lose the bounded
reporting behavior. Preloading every dependency would grow the context again.
On-demand, narrowly scoped evidence is the intervention to measure.

Implement and test path, archive, network, redaction and budget boundaries.
Then replay the existing large Python/Rust evidence through the committed
runtime, comparing with saved controls. Grade finding truth, repair correctness,
actual retrieval use, completion and time separately. Do not add grading notes
or known findings to model inputs. Production remains opt-in pending results.
