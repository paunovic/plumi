import os
from collections.abc import Callable
from functools import partial

import botocore.session
from botocore.exceptions import BotoCoreError, ClientError

from plumi.environment import Environment


class PreflightError(Exception):
    """raised when the credentials do not resolve for the target environment."""


def _caller_identity() -> str:
    client = botocore.session.get_session().create_client("sts")
    return client.get_caller_identity()["Account"]


def verify_credentials(
    environment: Environment,
    caller_identity: Callable[[], str] | None = None,
) -> None:
    if caller_identity is None:
        caller_identity = _caller_identity

    has_static_keys: bool = bool(os.environ.get("AWS_ACCESS_KEY_ID"))

    if has_static_keys and not environment.uses_envo:
        raise PreflightError(
            "raw AWS_ACCESS_KEY_ID in the environment without envo; "
            "run commands under envo: envo <env> plumi ...",
        )

    try:
        caller_identity()
    except (BotoCoreError, ClientError):
        raise PreflightError(
            "aws credentials did not resolve (sts get-caller-identity "
            "failed); re-authenticate (envo <env>, or aws sso login) and re-run",
        ) from None


def _describe_secrets_key(environment: Environment) -> None:
    client = botocore.session.get_session().create_client(
        "kms",
        region_name=environment.region or "us-east-1",
    )
    client.describe_key(KeyId="alias/pulumi-secrets")


def verify_secrets_provider(
    environment: Environment,
    describe_key: Callable[[], None] | None = None,
) -> None:
    # a missing secrets alias only surfaces at stack init with an
    # opaque kms error; fail here with the bootstrap pointer instead
    if describe_key is None:
        describe_key = partial(_describe_secrets_key, environment)

    try:
        describe_key()
    except (BotoCoreError, ClientError):
        raise PreflightError(
            "kms key alias/pulumi-secrets missing or unreachable "
            "(kms describe-key failed); run setup_aws_environment from "
            "the code repo to create it, then re-run",
        ) from None
