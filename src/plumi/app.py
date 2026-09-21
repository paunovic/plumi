import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable

from botocore.exceptions import BotoCoreError, ClientError

from plumi import locks, preflight
from plumi.environment import Environment, PlumiConfigError, resolve_environment
from plumi.locks import LockHolder


class Plumi:

    def run(self, args: list[str] | None = None) -> int:

        args = args if args is not None else sys.argv[1:]

        # help works anywhere, no config needed
        if not args or args[0] in {"--help", "-h"}:
            return self._dispatch_help()

        try:
            environment: Environment = resolve_environment()
        except PlumiConfigError as e:
            sys.stderr.write(f"error: {e}\n")
            sys.stderr.flush()
            return 1

        # check credentials before touching state
        preflight_return_code = self._preflight(args, environment)
        if preflight_return_code is not None:
            return preflight_return_code

        # probe the holder before cancel touches state
        if args[0] == "cancel":
            guard_return_code = self._cancel_guard(environment)
            if guard_return_code is not None:
                return guard_return_code

        # environment bucket, default .pulumi/... layout
        if environment.uses_local_state:
            login_args: list[str] = ["login", "--local"]
        else:
            login_args = [
                "login",
                "--cloud-url",
                f"s3://{environment.state_bucket}",
            ]

        login_exit_status = self.run_pulumi(args=login_args, environment=environment)
        if login_exit_status != 0:
            sys.stderr.write(f"error: plumi failed to login to s3 backend, exit code {login_exit_status}\n")
            sys.stderr.flush()
            return login_exit_status

        ensure_return_code = self._ensure_stack(args, environment)
        if ensure_return_code is not None:
            return ensure_return_code

        if args[0] == "cancel":
            return self._run_cancel(args, environment)

        return self.run_pulumi(
            args=self.with_stack(
                self.with_refresh(
                    self.with_secrets_provider(args, environment),
                ),
                environment,
            ),
            environment=environment,
        )

    def _dispatch_help(self) -> int:
        # let pulumi print its own usage
        is_interactive: bool = sys.stdout.isatty()

        result = subprocess.run(
            ["pulumi", "--help"],
            check=False,
            capture_output=not is_interactive,
            text=not is_interactive,
        )

        if is_interactive:
            self._print_cancel_usage()
            return result.returncode

        self._replay_output(result)
        self._print_cancel_usage()
        return result.returncode

    def _print_cancel_usage(self) -> None:
        # the one plumi command, not in pulumi's help
        sys.stdout.write(
            "\nplumi cancel\n"
            "  clear a stale lock on the environment state; refuses "
            "while the holder is a live local process\n",
        )
        sys.stdout.flush()

    def with_secrets_provider(
        self,
        args: list[str],
        environment: Environment,
    ) -> list[str]:
        # flag only valid on up and stack init
        is_up: bool = bool(args) and args[0] == "up"
        is_stack_init: bool = args[:2] == ["stack", "init"]

        if (is_up or is_stack_init) and not environment.uses_local_state:
            return [*args, "--secrets-provider", environment.secrets_provider]
        return args

    def with_stack(
        self,
        args: list[str],
        environment: Environment,
    ) -> list[str]:
        # same stack name as the environment, no prompt on fresh
        # workspaces
        stack_aware_subcommands = {
            "up", "preview", "refresh", "destroy", "import",
            "watch", "config", "stack", "cancel",
        }
        if not args or args[0] not in stack_aware_subcommands:
            return args

        for arg in args:
            if arg in {"-s", "-S", "--stack"} or arg.startswith("--stack="):
                return args

        # positional stack name, pulumi refuses --stack next to it
        if args[0] == "stack" and len(args) > 1 and args[1] in {
            "init", "select", "rm", "rename",
        }:
            return args

        return [*args, "--stack", environment.name]

    def with_refresh(self, args: list[str]) -> list[str]:
        # refresh before diff, console edits revert to code
        if not args or args[0] != "up":
            return args

        # caller passed one already
        for arg in args:
            if arg == "--refresh" or arg.startswith("--refresh="):
                return args

        return [*args, "--refresh"]

    def _ensure_stack(self, args: list[str], environment: Environment) -> int | None:
        # first up/preview inits the stack. destroy/refresh/watch
        # still fail, nothing there yet
        if not args or args[0] not in {"up", "preview"}:
            return None

        # explicit stack flag, user knows best
        for arg in args:
            if arg in {"-s", "-S", "--stack"} or arg.startswith("--stack="):
                return None

        stacks = self._list_stacks(environment)
        if stacks is None or environment.name in stacks:
            return None

        init_args = self.with_secrets_provider(
            ["stack", "init", environment.name],
            environment,
        )
        init_result = subprocess.run(
            ["pulumi", *init_args],
            env=self.pulumi_environment(environment),
            check=False,
            capture_output=True,
            text=True,
        )

        # a concurrent init winning the race reads as success
        is_race: bool = "already exists" in init_result.stderr
        if init_result.returncode != 0 and not is_race:
            sys.stderr.write(
                f"error: plumi failed to init stack {environment.name}, "
                f"exit code {init_result.returncode}\n",
            )
            sys.stderr.flush()
            self._replay_output(init_result)
            return init_result.returncode

        self._replay_output(init_result)
        return None

    def _list_stacks(self, environment: Environment) -> list[str] | None:
        ls_result = subprocess.run(
            ["pulumi", "stack", "ls", "--json"],
            env=self.pulumi_environment(environment),
            check=False,
            capture_output=True,
            text=True,
        )
        if ls_result.returncode != 0:
            return None

        # banners first, json from the first bracket line
        lines: list[str] = ls_result.stdout.splitlines()
        for index, line in enumerate(lines):
            if not line.lstrip().startswith(("[", "{")):
                continue
            try:
                return [entry["name"] for entry in json.loads(
                    "\n".join(lines[index:]),
                )]
            except (json.JSONDecodeError, KeyError, TypeError):
                return None
        return None

    def pulumi_environment(self, environment: Environment) -> dict:
        pulumi_env: dict = os.environ.copy()

        # local: empty passphrase. real: awskms
        if environment.uses_local_state:
            pulumi_env["PULUMI_CONFIG_PASSPHRASE"] = ""
        else:
            pulumi_env.pop("PULUMI_CONFIG_PASSPHRASE", None)

        return pulumi_env

    def _preflight(self, args: list[str], environment: Environment) -> int | None:
        if not args or args[0] not in {"up", "destroy", "refresh", "cancel"}:
            return None

        if environment.uses_local_state:
            return None

        try:
            preflight.verify_credentials(environment)
            preflight.verify_secrets_provider(environment)
        except preflight.PreflightError as e:
            sys.stderr.write(f"preflight error: {e}\n")
            sys.stderr.flush()
            return 1

        return None

    def _cancel_guard(
        self,
        environment: Environment,
        newest_holder: Callable[[], LockHolder | None] | None = None,
        is_pid_alive: Callable[[int], bool] | None = None,
    ) -> int | None:
        # a live local holder keeps its lock
        if environment.uses_local_state:
            # local state lock path unknown here, pulumi cancel
            # handles it
            return None

        holder: LockHolder | None
        if newest_holder is None:
            try:
                holder = locks.newest_lock_holder(environment)
            except (BotoCoreError, ClientError):
                # unreadable bucket, pulumi will complain anyway
                return None
        else:
            holder = newest_holder()

        if holder is None:
            return None

        if is_pid_alive is None:
            is_pid_alive = locks.process_is_alive

        holder_details = f"{holder.username}@{holder.hostname} (pid {holder.pid})"

        if holder.hostname == socket.gethostname() and is_pid_alive(holder.pid):
            sys.stderr.write(
                f"plumi: refusing to cancel, the lock is held by a live "
                f"local process: {holder_details}; canceling would allow "
                "concurrent state mutation; let the holder finish or "
                "kill it first\n",
            )
            sys.stderr.flush()
            return 1

        # can't verify remote/dead holders, log and continue
        sys.stderr.write(
            f"plumi: canceling the lock held by {holder_details}\n",
        )
        sys.stderr.flush()
        return None

    def _run_cancel(self, args: list[str], environment: Environment) -> int:
        return_code = self.run_pulumi(
            args=self.with_stack(args, environment),
            environment=environment,
        )

        if return_code != 0:
            return return_code

        # point at refresh before the retry
        sys.stderr.write(
            "plumi: state may be partial after a killed run; run "
            "`plumi refresh`, then retry your command\n",
        )
        sys.stderr.flush()
        return return_code

    def run_pulumi(self, args: list[str], environment: Environment) -> int:
        # capture when not a tty, replay after
        is_interactive: bool = sys.stdout.isatty()

        result = subprocess.run(
            ["pulumi", *args],
            env=self.pulumi_environment(environment),
            check=False,
            capture_output=not is_interactive,
            text=not is_interactive,
        )

        if is_interactive:
            return result.returncode

        if (
            result.returncode != 0
            and "error: no stack selected; please use `pulumi stack select` "
            "or `pulumi stack init` to choose one" in result.stderr
        ):
            remediated_return_code = self._remediate_no_stack(args, environment)
            if remediated_return_code is not None:
                return remediated_return_code

        self._replay_output(result)

        # bare NoSuchBucket gets the bucket name and the fix
        if result.returncode != 0 and "NoSuchBucket" in (
            (result.stdout or "") + (result.stderr or "")
        ):
            sys.stderr.write(
                f"plumi: state bucket s3://{environment.state_bucket} does not "
                "exist; run setup_aws_environment from the code repo once "
                "to bootstrap the org\n",
            )
            sys.stderr.flush()

        # locked stack: holder + recovery command
        if result.returncode != 0:
            holder_sentence = self._locked_holder_sentence(
                (result.stdout or "") + (result.stderr or ""),
            )
            if holder_sentence is not None:
                sys.stderr.write(
                    f"plumi: {holder_sentence}; if the holder is gone, "
                    "run: plumi cancel\n",
                )
                sys.stderr.flush()

        return result.returncode

    def _locked_holder_sentence(self, output: str) -> str | None:
        marker = "the stack is currently locked by"
        index = output.lower().find(marker)
        if index == -1:
            return None

        holder_sentence = output[index:]
        for separator in ("\n", "."):
            cut = holder_sentence.find(separator)
            if cut != -1:
                holder_sentence = holder_sentence[:cut]
        return holder_sentence.strip() or None

    def _remediate_no_stack(
        self,
        args: list[str],
        environment: Environment,
    ) -> int | None:
        # one stack: select. none: init. more: user decides
        stacks = self._list_stacks(environment)
        if stacks is None:
            return None

        if not stacks:
            return self._init_stack_and_retry(args, environment)

        if len(stacks) > 1:
            names = ", ".join(stacks)
            sys.stderr.write(
                f"plumi: no stack selected and multiple stacks exist: {names}; "
                "run `pulumi stack select <name>` and re-run\n",
            )
            sys.stderr.flush()
            return None

        select_result = subprocess.run(
            ["pulumi", "stack", "select", stacks[0]],
            env=self.pulumi_environment(environment),
            check=False,
            capture_output=True,
            text=True,
        )
        if select_result.returncode != 0:
            self._replay_output(select_result)
            return None

        retry_result = subprocess.run(
            ["pulumi", *args],
            env=self.pulumi_environment(environment),
            check=False,
            capture_output=True,
            text=True,
        )
        self._replay_output(retry_result)
        return retry_result.returncode

    def _init_stack_and_retry(
        self,
        args: list[str],
        environment: Environment,
    ) -> int | None:
        # init and retry once
        init_args = self.with_secrets_provider(
            ["stack", "init", environment.name],
            environment,
        )
        init_result = subprocess.run(
            ["pulumi", *init_args],
            env=self.pulumi_environment(environment),
            check=False,
            capture_output=True,
            text=True,
        )
        if init_result.returncode != 0:
            self._replay_output(init_result)
            return None

        retry_result = subprocess.run(
            ["pulumi", *args],
            env=self.pulumi_environment(environment),
            check=False,
            capture_output=True,
            text=True,
        )
        self._replay_output(retry_result)
        return retry_result.returncode

    def _replay_output(self, result: subprocess.CompletedProcess) -> None:
        sys.stdout.write(result.stdout)
        sys.stdout.flush()
        sys.stderr.write(result.stderr)
        sys.stderr.flush()
