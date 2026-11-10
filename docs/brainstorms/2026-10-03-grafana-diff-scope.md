---
date: 2026-10-03
topic: grafana-diff-scope
---

# Natural Grafana diff-scope comparison

The user approved returning to the real frozen source after isolated and
combined toys failed to reproduce the miss. Compare fresh full-PR and lock-diff
contexts under shipped `5391472`, with complete BASE/HEAD snapshots available
to both. Keep PR title/body and system prompt fixed.

Use one identical trusted consumer instruction in both arms: review the supplied
diff and its callers; that diff defines eligible scope. It sits outside the
untrusted PR fence. Scope is not smuggled into the author's body. Full receives
all five original diff blocks; lock receives only the unchanged bleve.go block
and corresponding changed-file list. Context and eligible scope change together;
this does not isolate token count from attention or scope.

Four fresh pairs, eight reviews, alternating first arm twice each. Same model,
allowances, policy, schema, tools, harness and sources. Two pair workers, fresh
staging at identical paths per pair. No extra instruction candidate, judge or
focused calls. No benchmark code execution.

Grade only the locking target: newly allowed lookup-during-build/older-last
publication lost update, explicit BASE prevention, proper changed anchor.
Generic duplicates, file-only collisions or residual-only delivery do not pass.
Track source requests, old caller-race attribution and other claims separately.
Do not claim full-review coverage from scoped output or treat historical misses
as fresh controls. Freeze all inputs and finish every sample with no retries,
edits, exclusions or automatic extra trials. Rito remains deferred.
