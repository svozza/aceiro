from aws_lambda_powertools.shared import connection
from aws_lambda_powertools.shared.constants import DEFAULT_TIMEOUT_SECONDS, MAX_RETRIES


def open_connection(host):
    return connection.connect(host, timeout=DEFAULT_TIMEOUT_SECONDS, retries=MAX_RETRIES)
