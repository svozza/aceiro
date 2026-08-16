# The result envelope classifies the session; a teardown exception does not

Found clearing PR #10's red evals (2026-08-16): a `fence_forgery_cross_tag`
run died with `harness error: Exception: Claude Code returned an error
result: success` — a bare `Exception` that escaped `drive_session` past the
whole retry ladder. Traced end to end, the sequence is deliberate SDK
behaviour meeting a gap in ours:

1. The CLI's API call failed. The CLI emitted a result envelope with
   `is_error: true` and `subtype: "success"` — which is not an inconsistent
   envelope but its **documented shape for a failing API call**: the SDK's
   `ResultMessage.api_error_status` ("HTTP status code … of the failing API
   call") is populated precisely and only for that combination.
2. The CLI then exited non-zero **on purpose**, for shell-script consumers.
3. The SDK's message reader replaced the resulting `ProcessError` with the
   error-result text (`_internal/query.py:385-388`) and `receive_messages`
   re-raised it as a bare `Exception` — after the envelope had already been
   delivered, in order, to our stream consumer.
4. `drive_session` caught `ProcessError` and `ClaudeSDKError`; a bare
   `Exception` matched neither. It flew past the `api_error` retry arm and
   past the `verified is not None` delivery check — so a session that had
   already got an artifact through the verifier could lose the run to
   process-teardown noise. Production lane, not just evals. Observed rate:
   1 in ~40 scenario-runs.

## Decision

**The envelope is the classifier.** `_run_session` had already captured the
`ResultMessage` when the exception destroyed it; the exception says nothing
the envelope doesn't (the SDK's own comment: the trailing `ProcessError`
"carries no information beyond exit code 1"). So `_run_session` swallows the
exception and returns the envelope — only when both hold:

- a `ResultMessage` was captured, and
- `is_sdk_stream_error(exc)`: the exception is **exactly** `type(exc) is
  Exception` and its text starts with `"Claude Code returned an error
  result: "` (`SDK_STREAM_ERROR_PREFIX`, beside `is_permanent_api_error`).

Exact type, not `isinstance`: every exception of ours is a subclass, and a
crashed harness must never read as anything else — the same hazard the
retired prover's three-way exit contract guarded (ADR-0016). "Catch
`Exception`" was rejected because it turns our own `TypeError`s into retries.

**Retryable means the api_error class, identified by the envelope.** The
retry arm matches `terminal_reason == "api_error"` **or** the documented
shape `is_error and subtype == "success"` — the OR because `terminal_reason`
is nullable (older CLIs, envelopes that bypassed the query loop). From there
the existing rules stand unchanged: deliver first if an artifact is already
verified, back off and retry unless a permanence marker matches, and the
breaker's abort outranks everything (ADR-0015 owns the budget side; a
wall-clock timeout still does not retry).

**Any other error envelope fails honestly, without retry.** Before this fix
those envelopes crashed the process; recovered, they would have fallen
through to "agent completed without calling submit_review" — a false reason
for a session that died. A guard names them by their own `subtype` and
`errors`, delivers first if an artifact is verified, and does not retry: an
unrecognized error class gets the `ClaudeSDKError` treatment. `error_max_turns`
keeps its own earlier arm.

**`api_error_status` is logged, not decided on.** The permanence markers were
each placed off a real incident; a status→permanence rule would be policy on
zero observed data points, on a provider (Bedrock) whose status mapping we
have not verified, where a wrong "permanent" call kills a retryable run — the
expensive direction of the asymmetry the markers are tuned around. The
`api_error` transcript record now carries the status, so the accumulated
records are the dataset such a rule would have to earn itself from.

## What is deliberately given up

1. **Recovery without an envelope.** In-order delivery means the prefixed
   text without a captured `ResultMessage` can only be an SDK fault (a parse
   failure on the envelope is a `MessageParseError`, a `ClaudeSDKError`, and
   already fails clean). It stays a loud crash.
2. **Text coupling to the SDK.** The predicate matches the SDK's message
   prefix. If an SDK upgrade changes the text, the failure mode is today's
   behaviour — a visible crash — not a silent misclassification.

## Known adjacency, out of scope

The SDK reader wraps *all* its fatal errors as bare `Exception`, so a plain
CLI crash with no envelope also arrives unprefixed at `receive_messages` —
meaning the `except ProcessError` arm in `drive_session` now mostly fires on
connect-time failures, and a mid-stream crash without an envelope escapes as
a bare exception rather than the arm's clean "exited N without a result
envelope" failure. That was equally true before this change; it is named
here so nobody reads the `ProcessError` arm as covering it.
