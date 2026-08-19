from __future__ import annotations

from typing import Any, Callable


class SqsFifoProcessor:
    """Processes SQS FIFO records in order, halting the group on first failure.

    FIFO semantics require that once a record in a message group fails, every
    subsequent record in that group is reported as failed too — otherwise a
    later record could be committed ahead of an earlier one.

    Group state is tracked per message group id so that unrelated groups
    continue to be processed independently.
    """

    def __init__(self, records: list[dict[str, Any]]):
        self.records = records
        self.failures: list[str] = []
        self._halted_groups: set[str] = set()

    def group_id(self, record: dict[str, Any]) -> str:
        return record["attributes"]["MessageGroupId"]

    def should_skip(self, record: dict[str, Any]) -> bool:
        """True when an earlier record in this group already failed."""
        return self._halted_groups.discard(self.group_id(record)) is not None

    def mark_failed(self, record: dict[str, Any]) -> None:
        self.failures.append(record["messageId"])
        self._halted_groups.add(self.group_id(record))

    def process(self, handler: Callable[[dict[str, Any]], None]) -> list[str]:
        for record in self.records:
            if self.should_skip(record):
                self.mark_failed(record)
                continue
            try:
                handler(record)
            except Exception:
                self.mark_failed(record)
        return self.failures

    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.records),
            "failed": len(self.failures),
            "halted_groups": len(self._halted_groups),
        }
