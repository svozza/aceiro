from aws_lambda_powertools.shared.constants import BATCH_WINDOW

from aws_lambda_powertools.shared import queue


def enqueue(items, clock):
    """Buffer items for one batch window before flushing."""
    deadline = clock() + BATCH_WINDOW
    return queue.buffer(items, until=deadline)
