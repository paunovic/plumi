import sys

import pytest
from botocore.exceptions import NoCredentialsError

from plumi import app as app_module
from plumi import preflight
from plumi.app import Plumi
from plumi.environment import Environment, resolve_environment
from plumi.tools import plumi as plumi_tool

NO_STACK_ERROR = (
    "error: no stack selected; please use `pulumi stack select` "
    "or `pulumi stack init` to choose one"
)


class FakeResult:
    def __init__(
        self,
        returncode: int,
        stdout: str | None = "",
        stderr: str | None = "",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, command, env=None, check=False, capture_output=False, text=False):
        self.calls.append({"command": command})
        return self.results.pop(0)


def local_environment() -> Environment:
    return resolve_environment(environ={})


def write_plumi_pyproject(directory) -> None:
    (directory / "pyproject.toml").write_text(
        "[organization]\n"
        'name = "acme"\n'
        'domain = "acme.com"\n',
    )


def test_run_returns_login_failure_exit_code(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.chdir(tmp_path)
    write_plumi_pyproject(tmp_path)
    login_exit_code = 3
    fake_run = FakeRun([FakeResult(login_exit_code)])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["stack", "ls"])

    assert return_code == login_exit_code
    assert fake_run.calls[0]["command"] == ["pulumi", "login", "--local"]
    captured = capsys.readouterr()
    assert "failed to login" in captured.err


def test_run_reports_missing_organization_table(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)

    return_code = Plumi().run(args=["stack", "ls"])

    assert return_code == 1
    captured = capsys.readouterr()
    assert "[organization]" in captured.err


def test_run_pulumi_replays_original_when_stack_ls_fails(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    fake_run = FakeRun([
        FakeResult(1, stdout="", stderr=NO_STACK_ERROR),
        FakeResult(1, stdout="", stderr="error: stack ls failed"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()

    return_code = plumi.run_pulumi(["up"], local_environment())

    assert return_code == 1
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "up"],
        ["pulumi", "stack", "ls", "--json"],
    ]
    # the unremediable ls failure falls back to replaying the original error
    captured = capsys.readouterr()
    assert "no stack selected" in captured.err
    # the bucket hint never fires on the remediation path's failures
    assert "state bucket" not in captured.err


def test_run_pulumi_replays_original_when_stack_select_fails(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    fake_run = FakeRun([
        FakeResult(1, stdout="", stderr=NO_STACK_ERROR),
        FakeResult(0, stdout='[{"name": "dev"}]', stderr=""),
        FakeResult(1, stdout="", stderr="error: select failed"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()

    return_code = plumi.run_pulumi(["up"], local_environment())

    assert return_code == 1
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "up"],
        ["pulumi", "stack", "ls", "--json"],
        ["pulumi", "stack", "select", "dev"],
    ]
    captured = capsys.readouterr()
    assert "select failed" in captured.err


def test_run_pulumi_replays_original_when_stack_init_fails(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    fake_run = FakeRun([
        FakeResult(1, stdout="", stderr=NO_STACK_ERROR),
        FakeResult(0, stdout="[]", stderr=""),
        FakeResult(1, stdout="", stderr="error: init failed"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()

    return_code = plumi.run_pulumi(["up"], local_environment())

    assert return_code == 1
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "up"],
        ["pulumi", "stack", "ls", "--json"],
        ["pulumi", "stack", "init", "localhost"],
    ]
    captured = capsys.readouterr()
    assert "init failed" in captured.err


def test_tool_main_returns_plumi_run_exit_code(monkeypatch):
    run_exit_code = 4

    class FakePlumi:
        def run(self, args=None) -> int:
            return run_exit_code

    monkeypatch.setattr(plumi_tool, "Plumi", FakePlumi)

    assert plumi_tool.main() == run_exit_code


def test_caller_identity_reads_account_from_sts(monkeypatch):
    class FakeStsClient:
        def get_caller_identity(self) -> dict:
            return {"Account": "123456789012"}

    class FakeSession:
        def create_client(self, service_name, region_name=None):
            assert service_name == "sts"
            return FakeStsClient()

    monkeypatch.setattr(
        preflight.botocore.session,
        "get_session",
        lambda: FakeSession(),
    )

    assert preflight._caller_identity() == "123456789012"


def test_verify_credentials_defaults_to_the_sts_caller(monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setattr(preflight, "_caller_identity", lambda: "123456789012")

    environment = resolve_environment(environ={"ENVO_ENVIRONMENT": "qa"})

    # the default caller resolves; nothing raises
    preflight.verify_credentials(environment)


def test_verify_credentials_wraps_a_failing_sts_call(monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

    def failing_caller() -> str:
        raise NoCredentialsError()

    monkeypatch.setattr(preflight, "_caller_identity", failing_caller)

    environment = resolve_environment(environ={"ENVO_ENVIRONMENT": "qa"})

    with pytest.raises(preflight.PreflightError, match="sts get-caller-identity"):
        preflight.verify_credentials(environment)


def test_with_stack_injects_derived_environment(tmp_path):
    plumi = Plumi()
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    args = plumi.with_stack(["preview"], environment)

    assert args == ["preview", "--stack", "qa"]


def test_with_stack_appends_after_subcommand_args(tmp_path):
    plumi = Plumi()
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    args = plumi.with_stack(["up", "--yes"], environment)

    assert args == ["up", "--yes", "--stack", "qa"]


def test_with_stack_covers_the_config_tree(tmp_path):
    plumi = Plumi()
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    args = plumi.with_stack(["config", "set", "key", "value"], environment)

    assert args == ["config", "set", "key", "value", "--stack", "qa"]


def test_with_stack_covers_read_only_stack_subcommands(tmp_path):
    plumi = Plumi()
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    args = plumi.with_stack(["stack", "ls"], environment)

    assert args == ["stack", "ls", "--stack", "qa"]


def test_with_stack_respects_explicit_long_stack_flag(tmp_path):
    plumi = Plumi()
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    args = plumi.with_stack(["up", "--stack", "custom"], environment)

    assert args == ["up", "--stack", "custom"]


def test_with_stack_respects_explicit_short_stack_flags(tmp_path):
    plumi = Plumi()
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    assert plumi.with_stack(["up", "-s", "custom"], environment) == [
        "up", "-s", "custom",
    ]
    assert plumi.with_stack(["preview", "-S", "custom"], environment) == [
        "preview", "-S", "custom",
    ]


def test_with_stack_respects_equals_form_stack_flag(tmp_path):
    plumi = Plumi()
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    args = plumi.with_stack(["up", "--stack=custom"], environment)

    assert args == ["up", "--stack=custom"]


def test_with_stack_skips_subcommands_without_a_stack_flag(tmp_path):
    plumi = Plumi()
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    for args in [
        ["login", "--cloud-url", "s3://bucket"],
        ["logout"],
        ["whoami"],
        ["new", "python"],
        ["plugin", "ls"],
        ["version"],
        ["logs"],
        ["console"],
    ]:
        assert plumi.with_stack(args, environment) == args


def test_with_stack_skips_stack_subcommands_naming_a_stack(tmp_path):
    plumi = Plumi()
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    for args in [
        ["stack", "select", "dev"],
        ["stack", "init", "dev"],
        ["stack", "rm", "dev"],
        ["stack", "rename", "dev"],
        ["stack", "select"],
    ]:
        assert plumi.with_stack(args, environment) == args


def test_with_stack_uses_localhost_without_profiles():
    plumi = Plumi()
    environment = resolve_environment(environ={})

    args = plumi.with_stack(["up"], environment)

    assert args == ["up", "--stack", "localhost"]


def test_with_stack_ignores_profile_environment_key(tmp_path):
    aws_config = tmp_path / "config"
    aws_config.write_text("[profile qa]\nenvironment = staging\n")

    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=aws_config,
    )

    args = Plumi().with_stack(["preview"], environment)

    assert args == ["preview", "--stack", "qa"]


def test_with_refresh_injects_on_up():
    args = Plumi().with_refresh(["up", "--yes"])

    assert args == ["up", "--yes", "--refresh"]


def test_with_refresh_respects_a_caller_passed_flag():
    plumi = Plumi()

    assert plumi.with_refresh(["up", "--refresh"]) == ["up", "--refresh"]
    assert plumi.with_refresh(["up", "--refresh=false"]) == ["up", "--refresh=false"]


def test_with_refresh_skips_subcommands_other_than_up():
    plumi = Plumi()

    for args in [
        ["preview"],
        ["destroy", "--yes"],
        ["refresh", "--yes"],
    ]:
        assert plumi.with_refresh(args) == args


def test_run_dispatches_derived_stack(monkeypatch, tmp_path):
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    dispatched: list[list[str]] = []

    def fake_run_pulumi(self, args, environment):
        dispatched.append(args)
        return 0

    monkeypatch.setattr(Plumi, "run_pulumi", fake_run_pulumi)

    # _ensure_stack probes stack existence via a real subprocess before
    # dispatch; feed it a stack-present listing so nothing external runs
    fake_run = FakeRun([FakeResult(0, stdout='[{"name": "qa"}]')])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["preview"])

    assert return_code == 0
    assert ["preview", "--stack", "qa"] in dispatched


def test_run_pulumi_hints_missing_state_bucket(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    fake_run = FakeRun([
        FakeResult(
            1,
            stdout="",
            stderr=(
                'error: could not log in to the state backend '
                '"s3://pulumi-state-qa.acme.com": error listing stacks: '
                "could not list bucket: blob (code=NotFound): "
                "NoSuchBucket: The specified bucket does not exist\n"
            ),
        ),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()

    return_code = plumi.run_pulumi(["login", "--cloud-url", "s3://bucket"], environment)

    assert return_code == 1
    captured = capsys.readouterr()
    # the original failure still replays, hint follows it
    assert "NoSuchBucket" in captured.err
    assert (
        "plumi: state bucket s3://pulumi-state-qa.acme.com does not exist — "
        "run setup_aws_environment" in captured.err
    )


def test_run_pulumi_no_hint_for_failures_without_missing_bucket(
    monkeypatch, capsys,
):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    fake_run = FakeRun([FakeResult(1, stdout="", stderr="error: something else\n")])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()

    return_code = plumi.run_pulumi(["up"], local_environment())

    assert return_code == 1
    captured = capsys.readouterr()
    assert "error: something else" in captured.err
    assert "state bucket" not in captured.err


def test_run_pulumi_no_hint_on_success(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    fake_run = FakeRun([FakeResult(0, stdout="ok", stderr="")])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()

    return_code = plumi.run_pulumi(["preview"], local_environment())

    assert return_code == 0
    captured = capsys.readouterr()
    assert "state bucket" not in captured.err


def test_run_pulumi_surfaces_locked_stack_with_cancel_hint(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    fake_run = FakeRun([
        FakeResult(
            255,
            stdout="",
            stderr=(
                "error: The stack is currently locked by 1 lock(s): "
                "5b8f1f4c (held by dana@other-host)\n"
            ),
        ),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()

    return_code = plumi.run_pulumi(["up"], local_environment())

    assert return_code == 255
    captured = capsys.readouterr()
    assert "held by dana@other-host" in captured.err
    assert "if the holder is gone, run: plumi cancel" in captured.err


def test_run_login_failure_with_no_such_bucket_prints_hint(
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)
    fake_run = FakeRun([
        FakeResult(
            1,
            stdout="",
            stderr=(
                "error: problem logging in: error listing stacks: could not "
                "list bucket: blob (code=NotFound): NoSuchBucket\n"
            ),
        ),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["preview"])

    assert return_code == 1
    captured = capsys.readouterr()
    assert (
        "plumi: state bucket s3://pulumi-state-qa.acme.com does not exist" in captured.err
    )
    assert "failed to login" in captured.err


def test_bare_invocation_prints_pulumi_usage_without_config(
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.chdir(tmp_path)
    fake_run = FakeRun([FakeResult(0, stdout="pulumi usage text\n")])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=[])

    assert return_code == 0
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "--help"],
    ]
    captured = capsys.readouterr()
    assert "pulumi usage text" in captured.out
    assert "[organization]" not in captured.err


def test_help_flag_prints_pulumi_usage_without_config(
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.chdir(tmp_path)
    fake_run = FakeRun([FakeResult(0, stdout="pulumi usage text\n")])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["--help"])

    assert return_code == 0
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "--help"],
    ]
    captured = capsys.readouterr()
    assert "pulumi usage text" in captured.out
    assert "[organization]" not in captured.err


def test_help_lists_the_plumi_cancel_usage(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.chdir(tmp_path)
    fake_run = FakeRun([FakeResult(0, stdout="pulumi usage text\n")])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["--help"])

    assert return_code == 0
    captured = capsys.readouterr()
    assert "plumi cancel" in captured.out


def test_short_help_flag_prints_pulumi_usage_without_config(
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.chdir(tmp_path)
    fake_run = FakeRun([FakeResult(0, stdout="pulumi usage text\n")])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["-h"])

    assert return_code == 0
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "--help"],
    ]
    captured = capsys.readouterr()
    assert "pulumi usage text" in captured.out
    assert "[organization]" not in captured.err
