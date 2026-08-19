from aws_lambda_powertools.shared.constants import DEFAULT_TIMEOUT


def serve(handler, listen):
    """Serve requests with the module-wide timeout."""
    return listen(handler, timeout=DEFAULT_TIMEOUT)
