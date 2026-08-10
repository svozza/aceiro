from aws_lambda_powertools.shared.constants import BATCH_WINDOW

from aws_lambda_powertools.shared import queue


def drain(sink, clock):
    """Drain whatever the producer buffered in the last window."""
    since = clock() - BATCH_WINDOW
    return queue.drain_since(sink, since)
