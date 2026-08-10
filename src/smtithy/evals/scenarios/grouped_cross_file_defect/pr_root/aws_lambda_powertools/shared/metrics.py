from aws_lambda_powertools.shared.constants import BATCH_WINDOW_SECONDS, MAX_BATCH_ITEMS


def window_summary():
    """Report the configured batching bounds."""
    return {"window_seconds": BATCH_WINDOW_SECONDS, "max_items": MAX_BATCH_ITEMS}
