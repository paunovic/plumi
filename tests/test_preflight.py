import logging

import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from plumi import preflight
from plumi.environment import resolve_environment
from plumi.preflight import PreflightError, verify_credentials


class FakeCaller:
    def __init__(self, account_id: str | None) -> None:
        self.account_id = account_id

    def __call__(self) -> str:
        if self.account_id is None:
            raise NoCredentialsError()
        return self.account_id


def test_refuses_raw_static_keys_without_envo(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)

    environment = resolve_environment(environ={"AWS_PROFILE": "qa"})

    with pytest.raises(PreflightError, match="envo"):
        verify_credentials(environment, caller_identity=FakeCaller("123456789012"))


def test_accepts_federated_session_credentials_without_envo(monkeypatch, caplog):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-token")

    environment = resolve_environment(environ={"AWS_PROFILE": "qa"})

    with caplog.at_level(logging.INFO, logger="plumi.preflight"):
        verify_credentials(environment, caller_identity=FakeCaller("123456789012"))

    assert "federated session credentials" in caplog.text
    assert "envo" in caplog.text


def test_envo_holds_precedence_over_federated_credentials(monkeypatch, caplog):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAEXAMPLE")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-token")

    environment = resolve_environment(environ={"ENVO_ENVIRONMENT": "qa"})

    with caplog.at_level(logging.INFO, logger="plumi.preflight"):
        verify_credentials(environment, caller_identity=FakeCaller("123456789012"))

    assert "federated session credentials" not in caplog.text


def test_accepts_materialized_keys_under_envo(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAEXAMPLE")

    environment = resolve_environment(environ={"ENVO_ENVIRONMENT": "qa"})

    verify_credentials(environment, caller_identity=FakeCaller("123456789012"))


def test_expired_credentials_error_names_envo(monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

    environment = resolve_environment(environ={"ENVO_ENVIRONMENT": "qa"})

    with pytest.raises(PreflightError, match="envo"):
        verify_credentials(environment, caller_identity=FakeCaller(None))


def test_secrets_provider_probe_passes_when_the_alias_resolves(tmp_path):
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    preflight.verify_secrets_provider(environment, describe_key=lambda: None)


def test_secrets_provider_probe_fails_naming_the_alias_and_bootstrap(tmp_path):
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    def missing_alias() -> None:
        raise ClientError(
            {"Error": {"Code": "NotFoundException", "Message": "alias not found"}},
            "DescribeKey",
        )

    with pytest.raises(PreflightError) as excinfo:
        preflight.verify_secrets_provider(environment, describe_key=missing_alias)

    assert "alias/pulumi-secrets" in str(excinfo.value)
    assert "setup_aws_environment" in str(excinfo.value)


def test_describe_secrets_key_targets_the_alias(monkeypatch, tmp_path):
    class FakeKmsClient:
        def describe_key(self, KeyId) -> dict:
            assert KeyId == "alias/pulumi-secrets"
            return {"KeyMetadata": {}}

    class FakeSession:
        def create_client(self, service_name, region_name=None):
            assert service_name == "kms"
            assert region_name == "us-east-1"
            return FakeKmsClient()

    monkeypatch.setattr(
        preflight.botocore.session,
        "get_session",
        lambda: FakeSession(),
    )

    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    preflight._describe_secrets_key(environment)
