"""Tell the single-stage reviewer about its existing clock; do not extend it."""

import time

from claude_agent_sdk import HookContext, HookInput, HookJSONOutput, HookMatcher

from artifact import Transcript


class ReviewBudget:
    def __init__(self, seconds: float, state: dict, transcript: Transcript):
        self.seconds = seconds
        self.deadline = time.monotonic() + seconds
        self.state = state
        self.transcript = transcript
        self.stage = 0

    def introduction(self) -> str:
        return (
            f"\n\nReview time allowance: about {int(self.seconds)} seconds for this attempt, "
            "including thinking and tool calls. Reserve time to finish the review and "
            "call submit_review. Budget notices after source calls "
            "will update the remaining time. Do not promote unresolved concerns merely "
            "because time is short."
        )

    def hooks(self):
        matcher = HookMatcher(matcher="Read|Grep|Glob", hooks=[self.after_source], timeout=2)
        return {"PostToolUse": [matcher], "PostToolUseFailure": [matcher]}

    async def after_source(
        self, data: HookInput, tool_use_id: str | None, context: HookContext,
    ) -> HookJSONOutput:
        event = data["hook_event_name"]
        if event not in ("PostToolUse", "PostToolUseFailure"):
            return {}
        if data.get("tool_name") not in ("Read", "Grep", "Glob"):
            return {}
        if self.state["accepted"] is not None or self.state["abort_reason"]:
            return {}
        remaining = max(0, self.deadline - time.monotonic())
        stage = sum(remaining <= self.seconds * fraction for fraction in (0.5, 0.25, 0.1))
        if stage <= self.stage:
            return {}
        self.stage = stage
        instructions = (
            "Close the current line of inquiry before investigating a different concern. "
            "Keep any source-supported findings in the final review.",
            "Prioritize finishing the review with the evidence already collected. Limit "
            "further reads to facts needed to resolve current findings. Keep unresolved "
            "concerns in residual_risk.",
            "Call submit_review now with the findings you still stand behind and explicit "
            "remaining uncertainty and coverage limits. Do not turn unresolved concerns "
            "into findings to fill the review.",
        )
        note = f"Review budget: about {int(remaining)} seconds remain. {instructions[stage - 1]}"
        self.transcript.log("budget_feedback", stage=stage, remaining_seconds=int(remaining))
        if event == "PostToolUse":
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}
        return {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure", "additionalContext": note}}
