from aws_lambda_powertools.shared.constants import DEFAULT_TIMEOUT


def fetch(url, get):
    """Fetch url with the module-wide timeout."""
    return get(url, timeout=DEFAULT_TIMEOUT)
