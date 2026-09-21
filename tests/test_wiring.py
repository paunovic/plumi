import sys

from plumi import app as app_module
from plumi import preflight
from plumi.app import Plumi
from plumi.environment import resolve_environment


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
        self.calls.append({
            "command": command,
            "capture_output": capture_output,
            "text": text,
        })
        return self.results.pop(0)


def test_run_pulumi_selects_the_only_stack_and_retries_once(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    fake_run = FakeRun([
        FakeResult(
            1,
            stdout="",
            stderr=(
                "error: no stack selected; please use `pulumi stack select` "
                "or `pulumi stack init` to choose one\n"
            ),
        ),
        FakeResult(0, stdout='[{"name": "dev"}]', stderr=""),
        FakeResult(0, stdout="", stderr=""),
        FakeResult(0, stdout="resources: 1 done", stderr=""),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()
    environment = resolve_environment(environ={})

    return_code = plumi.run_pulumi(["up"], environment)

    assert return_code == 0
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "up"],
        ["pulumi", "stack", "ls", "--json"],
        ["pulumi", "stack", "select", "dev"],
        ["pulumi", "up"],
    ]
    assert all(call["capture_output"] for call in fake_run.calls)


def test_run_pulumi_inits_stack_when_none_exist_on_local_and_retries_once(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    fake_run = FakeRun([
        FakeResult(
            1,
            stdout="",
            stderr=(
                "error: no stack selected; please use `pulumi stack select` "
                "or `pulumi stack init` to choose one\n"
            ),
        ),
        FakeResult(0, stdout="[]", stderr=""),
        FakeResult(0, stdout="", stderr=""),
        FakeResult(0, stdout="resources: 1 done", stderr=""),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()
    environment = resolve_environment(environ={})

    return_code = plumi.run_pulumi(["up"], environment)

    assert return_code == 0
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "up"],
        ["pulumi", "stack", "ls", "--json"],
        ["pulumi", "stack", "init", "localhost"],
        ["pulumi", "up"],
    ]


def test_run_pulumi_inits_stack_with_awskms_provider_on_real_environment(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    fake_run = FakeRun([
        FakeResult(
            1,
            stdout="",
            stderr=(
                "error: no stack selected; please use `pulumi stack select` "
                "or `pulumi stack init` to choose one\n"
            ),
        ),
        FakeResult(0, stdout="[]", stderr=""),
        FakeResult(0, stdout="", stderr=""),
        FakeResult(0, stdout="resources: 1 done", stderr=""),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    return_code = plumi.run_pulumi(["up"], environment)

    assert return_code == 0
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "up"],
        ["pulumi", "stack", "ls", "--json"],
        [
            "pulumi",
            "stack",
            "init",
            "qa",
            "--secrets-provider",
            "awskms://alias/pulumi-secrets?region=us-east-1",
        ],
        ["pulumi", "up"],
    ]


def test_run_pulumi_returns_retry_code_when_zero_stack_retry_fails(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    retry_failure = FakeResult(2, stdout="", stderr="retry failed")
    fake_run = FakeRun([
        FakeResult(
            1,
            stdout="",
            stderr=(
                "error: no stack selected; please use `pulumi stack select` "
                "or `pulumi stack init` to choose one\n"
            ),
        ),
        FakeResult(0, stdout="[]", stderr=""),
        FakeResult(0, stdout="", stderr=""),
        retry_failure,
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()
    environment = resolve_environment(environ={})

    return_code = plumi.run_pulumi(["up"], environment)

    assert return_code == retry_failure.returncode
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "up"],
        ["pulumi", "stack", "ls", "--json"],
        ["pulumi", "stack", "init", "localhost"],
        ["pulumi", "up"],
    ]


def test_run_pulumi_keeps_failure_when_several_stacks_exist(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    fake_run = FakeRun([
        FakeResult(
            1,
            stdout="",
            stderr=(
                "error: no stack selected; please use `pulumi stack select` "
                "or `pulumi stack init` to choose one\n"
            ),
        ),
        FakeResult(0, stdout='[{"name": "dev"}, {"name": "prod"}]', stderr=""),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()
    environment = resolve_environment(environ={})

    return_code = plumi.run_pulumi(["up"], environment)

    assert return_code == 1
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "up"],
        ["pulumi", "stack", "ls", "--json"],
    ]
    captured = capsys.readouterr()
    assert "no stack selected" in captured.err
    assert "dev" in captured.err
    assert "prod" in captured.err


def test_run_pulumi_unparseable_stack_ls_replays_original_failure(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    fake_run = FakeRun([
        FakeResult(
            1,
            stdout="",
            stderr=(
                "error: no stack selected; please use `pulumi stack select` "
                "or `pulumi stack init` to choose one\n"
            ),
        ),
        FakeResult(0, stdout="plugin noise, not json", stderr=""),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()
    environment = resolve_environment(environ={})

    return_code = plumi.run_pulumi(["up"], environment)

    assert return_code == 1
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "up"],
        ["pulumi", "stack", "ls", "--json"],
    ]
    captured = capsys.readouterr()
    assert "no stack selected" in captured.err


def test_run_pulumi_does_not_remediate_other_failures(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    fake_run = FakeRun([FakeResult(1, stdout="boom out", stderr="boom err")])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()
    environment = resolve_environment(environ={})

    return_code = plumi.run_pulumi(["up"], environment)

    assert return_code == 1
    assert len(fake_run.calls) == 1
    captured = capsys.readouterr()
    assert "boom out" in captured.out
    assert "boom err" in captured.err


def test_run_pulumi_keeps_interactive_passthrough(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    fake_run = FakeRun([FakeResult(1, stdout=None, stderr=None)])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    plumi = Plumi()
    environment = resolve_environment(environ={})

    return_code = plumi.run_pulumi(["up"], environment)

    assert return_code == 1
    assert len(fake_run.calls) == 1
    assert fake_run.calls[0]["capture_output"] is False
    assert fake_run.calls[0]["text"] is False


def test_run_verifies_credentials_before_state_mutating_dispatch(
    monkeypatch, tmp_path,
):
    verified: list[str] = []

    def fake_verify_credentials(environment):
        verified.append(environment.name)

    monkeypatch.setattr(preflight, "verify_credentials", fake_verify_credentials)
    monkeypatch.setattr(preflight, "verify_secrets_provider", lambda environment: None)

    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)
    monkeypatch.chdir(tmp_path)

    dispatched: list[list[str]] = []

    def fake_run_pulumi(self, args, environment):
        dispatched.append(args)
        return 0

    monkeypatch.setattr(Plumi, "run_pulumi", fake_run_pulumi)

    # _ensure_stack probes stack existence via a real subprocess before
    # dispatch; feed it a stack-present listing so nothing external runs
    fake_run = FakeRun([FakeResult(0, stdout='[{"name": "qa"}]')])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["up"])

    assert return_code == 0
    assert verified == ["qa"]
    assert ["login", "--cloud-url", "s3://pulumi-state-qa-acme-com"] in dispatched
    assert [
        "up",
        "--secrets-provider",
        "awskms://alias/pulumi-secrets?region=us-east-1",
        "--refresh",
        "--stack",
        "qa",
    ] in dispatched


def test_run_skips_verify_credentials_for_read_only_commands(monkeypatch, tmp_path):
    verified: list[str] = []

    def fake_verify_credentials(environment):
        verified.append(environment.name)

    monkeypatch.setattr(preflight, "verify_credentials", fake_verify_credentials)

    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)
    monkeypatch.chdir(tmp_path)

    def fake_run_pulumi(self, args, environment):
        return 0

    monkeypatch.setattr(Plumi, "run_pulumi", fake_run_pulumi)

    return_code = Plumi().run(args=["stack", "ls"])

    assert return_code == 0
    assert verified == []


def test_run_skips_verify_credentials_on_local_state(monkeypatch, tmp_path):
    verified: list[str] = []

    def fake_verify_credentials(environment):
        verified.append(environment.name)

    monkeypatch.setattr(preflight, "verify_credentials", fake_verify_credentials)

    environment = resolve_environment(environ={})
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)
    monkeypatch.chdir(tmp_path)

    def fake_run_pulumi(self, args, environment):
        return 0

    monkeypatch.setattr(Plumi, "run_pulumi", fake_run_pulumi)

    # _ensure_stack probes stack existence via a real subprocess before
    # dispatch; feed it a stack-present listing so nothing external runs
    fake_run = FakeRun([FakeResult(0, stdout='[{"name": "localhost"}]')])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["up"])

    assert return_code == 0
    assert verified == []


def test_run_fails_fast_when_preflight_refuses(monkeypatch, tmp_path, capsys):
    def fake_verify_credentials(environment):
        raise preflight.PreflightError(
            "raw AWS_ACCESS_KEY_ID in the environment without envo",
        )

    monkeypatch.setattr(preflight, "verify_credentials", fake_verify_credentials)

    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)
    monkeypatch.chdir(tmp_path)

    dispatched: list[list[str]] = []

    def fake_run_pulumi(self, args, environment):
        dispatched.append(args)
        return 0

    monkeypatch.setattr(Plumi, "run_pulumi", fake_run_pulumi)

    return_code = Plumi().run(args=["up"])

    assert return_code == 1
    captured = capsys.readouterr()
    assert "envo" in captured.err
    assert dispatched == []



def test_run_inits_missing_stack_before_up(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(preflight, "verify_credentials", lambda environment: None)
    monkeypatch.setattr(preflight, "verify_secrets_provider", lambda environment: None)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(0, stdout="[]"),
        FakeResult(0, stdout="Created stack 'qa'\n"),
        FakeResult(0, stdout="resources: 1 done"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["up", "--yes"])

    assert return_code == 0
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "login", "--cloud-url", "s3://pulumi-state-qa-acme-com"],
        ["pulumi", "stack", "ls", "--json"],
        [
            "pulumi",
            "stack",
            "init",
            "qa",
            "--secrets-provider",
            "awskms://alias/pulumi-secrets?region=us-east-1",
        ],
        [
            "pulumi",
            "up",
            "--yes",
            "--secrets-provider",
            "awskms://alias/pulumi-secrets?region=us-east-1",
            "--refresh",
            "--stack",
            "qa",
        ],
    ]


def test_run_inits_missing_stack_before_preview_without_provider(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(0, stdout="[]"),
        FakeResult(0, stdout="Created stack 'qa'\n"),
        FakeResult(0, stdout="preview done"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["preview"])

    assert return_code == 0
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "login", "--cloud-url", "s3://pulumi-state-qa-acme-com"],
        ["pulumi", "stack", "ls", "--json"],
        [
            "pulumi",
            "stack",
            "init",
            "qa",
            "--secrets-provider",
            "awskms://alias/pulumi-secrets?region=us-east-1",
        ],
        ["pulumi", "preview", "--stack", "qa"],
    ]


def test_run_inits_missing_stack_on_local_state(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    environment = resolve_environment(environ={})
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(0, stdout="[]"),
        FakeResult(0, stdout="Created stack 'localhost'\n"),
        FakeResult(0, stdout="resources: 1 done"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["up", "--yes"])

    assert return_code == 0
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "login", "--local"],
        ["pulumi", "stack", "ls", "--json"],
        ["pulumi", "stack", "init", "localhost"],
        ["pulumi", "up", "--yes", "--refresh", "--stack", "localhost"],
    ]


def test_run_skips_init_when_stack_present(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(0, stdout='[{"name": "qa"}]'),
        FakeResult(0, stdout="preview done"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["preview"])

    assert return_code == 0
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "login", "--cloud-url", "s3://pulumi-state-qa-acme-com"],
        ["pulumi", "stack", "ls", "--json"],
        ["pulumi", "preview", "--stack", "qa"],
    ]


def test_run_does_not_auto_init_for_destroy(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(preflight, "verify_credentials", lambda environment: None)
    monkeypatch.setattr(preflight, "verify_secrets_provider", lambda environment: None)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(1, stdout="", stderr="error: no stack named 'qa' found"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["destroy", "--yes"])

    assert return_code == 1
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "login", "--cloud-url", "s3://pulumi-state-qa-acme-com"],
        ["pulumi", "destroy", "--yes", "--stack", "qa"],
    ]


def test_run_does_not_auto_init_for_refresh(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(preflight, "verify_credentials", lambda environment: None)
    monkeypatch.setattr(preflight, "verify_secrets_provider", lambda environment: None)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(1, stdout="", stderr="error: no stack named 'qa' found"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["refresh", "--yes"])

    assert return_code == 1
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "login", "--cloud-url", "s3://pulumi-state-qa-acme-com"],
        ["pulumi", "refresh", "--yes", "--stack", "qa"],
    ]


def test_run_init_failure_aborts_dispatch(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(0, stdout="[]"),
        FakeResult(255, stdout="", stderr="error: kms blew up"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["preview"])

    assert return_code == 255
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "login", "--cloud-url", "s3://pulumi-state-qa-acme-com"],
        ["pulumi", "stack", "ls", "--json"],
        [
            "pulumi",
            "stack",
            "init",
            "qa",
            "--secrets-provider",
            "awskms://alias/pulumi-secrets?region=us-east-1",
        ],
    ]
    captured = capsys.readouterr()
    assert "failed to init stack qa" in captured.err
    assert "kms blew up" in captured.err


def test_run_treats_init_race_as_success(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(0, stdout="[]"),
        FakeResult(
            255,
            stdout="",
            stderr="error: stack 'acme/probe-restricted-secrets/qa' already exists",
        ),
        FakeResult(0, stdout="preview done"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["preview"])

    assert return_code == 0
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "login", "--cloud-url", "s3://pulumi-state-qa-acme-com"],
        ["pulumi", "stack", "ls", "--json"],
        [
            "pulumi",
            "stack",
            "init",
            "qa",
            "--secrets-provider",
            "awskms://alias/pulumi-secrets?region=us-east-1",
        ],
        ["pulumi", "preview", "--stack", "qa"],
    ]


def test_run_skips_ensure_for_explicit_stack_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(preflight, "verify_credentials", lambda environment: None)
    monkeypatch.setattr(preflight, "verify_secrets_provider", lambda environment: None)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(1, stdout="", stderr="error: no stack named 'other' found"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["up", "--yes", "-s", "other"])

    assert return_code == 1
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "login", "--cloud-url", "s3://pulumi-state-qa-acme-com"],
        [
            "pulumi",
            "up",
            "--yes",
            "-s",
            "other",
            "--secrets-provider",
            "awskms://alias/pulumi-secrets?region=us-east-1",
            "--refresh",
        ],
    ]


def test_run_up_refreshes_tracked_resources_before_diffing(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(preflight, "verify_credentials", lambda environment: None)
    monkeypatch.setattr(preflight, "verify_secrets_provider", lambda environment: None)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(0, stdout='[{"name": "qa"}]'),
        FakeResult(0, stdout="resources: 1 done"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["up", "--yes"])

    assert return_code == 0
    assert fake_run.calls[-1]["command"] == [
        "pulumi",
        "up",
        "--yes",
        "--secrets-provider",
        "awskms://alias/pulumi-secrets?region=us-east-1",
        "--refresh",
        "--stack",
        "qa",
    ]


def test_run_up_keeps_a_caller_passed_refresh_flag_once(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(preflight, "verify_credentials", lambda environment: None)
    monkeypatch.setattr(preflight, "verify_secrets_provider", lambda environment: None)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(0, stdout='[{"name": "qa"}]'),
        FakeResult(0, stdout="resources: 1 done"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["up", "--refresh"])

    assert return_code == 0
    up_command = fake_run.calls[-1]["command"]
    assert up_command.count("--refresh") == 1
    assert up_command == [
        "pulumi",
        "up",
        "--refresh",
        "--secrets-provider",
        "awskms://alias/pulumi-secrets?region=us-east-1",
        "--stack",
        "qa",
    ]


def test_list_stacks_tolerates_login_banner_before_json(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    environment = resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )

    fake_run = FakeRun([
        FakeResult(
            0,
            stdout=(
                "Logged in as qa (s3://pulumi-state-qa-acme-com)\n"
                "warning: plugin noise\n"
                '[{"name": "qa"}]\n'
            ),
        ),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    assert Plumi()._list_stacks(environment) == ["qa"]
