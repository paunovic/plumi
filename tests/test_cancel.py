import socket
import sys

from plumi import app as app_module
from plumi import locks, preflight
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
        self.calls.append({"command": command})
        return self.results.pop(0)


def qa_environment(tmp_path):
    return resolve_environment(
        environ={"ENVO_ENVIRONMENT": "qa"},
        aws_config_path=tmp_path / "config",
    )


def test_cancel_refuses_while_the_holder_is_a_live_local_process(
    tmp_path, capsys,
):
    plumi = Plumi()
    holder = locks.LockHolder(
        username="marko",
        hostname=socket.gethostname(),
        pid=1234,
    )

    return_code = plumi._cancel_guard(
        qa_environment(tmp_path),
        newest_holder=lambda: holder,
        is_pid_alive=lambda pid: True,
    )

    assert return_code == 1
    captured = capsys.readouterr()
    assert "refusing to cancel" in captured.err
    assert f"marko@{socket.gethostname()} (pid 1234)" in captured.err
    assert "concurrent state mutation" in captured.err


def test_cancel_runs_pulumi_cancel_for_a_remote_holder(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    environment = qa_environment(tmp_path)
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)
    monkeypatch.setattr(preflight, "verify_credentials", lambda environment: None)
    monkeypatch.setattr(preflight, "verify_secrets_provider", lambda environment: None)

    holder = locks.LockHolder(username="dana", hostname="other-host", pid=10)
    monkeypatch.setattr(
        app_module.locks,
        "newest_lock_holder",
        lambda environment: holder,
    )

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(0, stdout="lock canceled"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["cancel"])

    assert return_code == 0
    assert [call["command"] for call in fake_run.calls] == [
        ["pulumi", "login", "--cloud-url", "s3://pulumi-state-qa.acme.com"],
        ["pulumi", "cancel", "--stack", "qa"],
    ]
    captured = capsys.readouterr()
    assert "dana@other-host (pid 10)" in captured.err


def test_cancel_runs_pulumi_cancel_for_a_dead_local_holder(
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    environment = qa_environment(tmp_path)
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)
    monkeypatch.setattr(preflight, "verify_credentials", lambda environment: None)
    monkeypatch.setattr(preflight, "verify_secrets_provider", lambda environment: None)

    holder = locks.LockHolder(
        username="marko",
        hostname=socket.gethostname(),
        pid=1234,
    )
    monkeypatch.setattr(
        app_module.locks,
        "newest_lock_holder",
        lambda environment: holder,
    )
    monkeypatch.setattr(app_module.locks, "process_is_alive", lambda pid: False)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(0, stdout="lock canceled"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["cancel"])

    assert return_code == 0
    assert fake_run.calls[1]["command"] == ["pulumi", "cancel", "--stack", "qa"]
    captured = capsys.readouterr()
    assert "state may be partial after a killed run" in captured.err
    assert "plumi refresh" in captured.err


def test_cancel_on_local_state_skips_the_holder_probe(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    environment = resolve_environment(environ={})
    monkeypatch.setattr(app_module, "resolve_environment", lambda: environment)

    def exploding_probe(environment):
        raise AssertionError("localhost must not probe s3 locks")

    monkeypatch.setattr(app_module.locks, "newest_lock_holder", exploding_probe)

    fake_run = FakeRun([
        FakeResult(0),
        FakeResult(0, stdout="lock canceled"),
    ])
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    return_code = Plumi().run(args=["cancel"])

    assert return_code == 0
    assert fake_run.calls == [
        {"command": ["pulumi", "login", "--local"]},
        {"command": ["pulumi", "cancel", "--stack", "localhost"]},
    ]


def test_newest_lock_holder_picks_the_newest_blob_and_parses_the_holder(tmp_path):
    class FakeBody:
        def __init__(self, payload: str) -> None:
            self.payload = payload

        def read(self) -> bytes:
            return self.payload.encode()

    class FakeS3:
        def list_objects_v2(self, Bucket, Prefix):
            assert Prefix == ".pulumi/locks/qa/"
            return {
                "Contents": [
                    {"Key": ".pulumi/locks/qa/older.json", "LastModified": 1},
                    {"Key": ".pulumi/locks/qa/newer.json", "LastModified": 2},
                ],
            }

        def get_object(self, Bucket, Key):
            assert Key == ".pulumi/locks/qa/newer.json"
            return {
                "Body": FakeBody(
                    '{"username": "marko", "hostname": "workstation", "pid": 42}',
                ),
            }

    holder = locks.newest_lock_holder(qa_environment(tmp_path), client=FakeS3())

    assert holder == locks.LockHolder(
        username="marko",
        hostname="workstation",
        pid=42,
    )


def test_newest_lock_holder_reports_no_holder_when_the_prefix_is_empty(tmp_path):
    class FakeS3:
        def list_objects_v2(self, Bucket, Prefix):
            return {}

    holder = locks.newest_lock_holder(qa_environment(tmp_path), client=FakeS3())

    assert holder is None
