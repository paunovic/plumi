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

        # help must work in any directory; config resolution only
        # gates real pulumi commands
        if not args or args[0] in {"--help", "-h"}:
            return self._dispatch_help()

        try:
            environment: Environment = resolve_environment()
        except PlumiConfigError as e:
            sys.stderr.write(f"error: {e}\n")
            sys.stderr.flush()
            return 1

        # state-mutating commands against real environments need working
        # credentials before any state-backend login
        preflight_return_code = self._preflight(args, environment)
        if preflight_return_code is not None:
            return preflight_return_code

        # cancel drops another process's lock; the holder gets probed
        # before anything touches the state backend
        if args[0] == "cancel":
            guard_return_code = self._cancel_guard(environment)
            if guard_return_code is not None:
                return guard_return_code

        # login is the only state placement: the environment's bucket
        # with pulumi's default .pulumi/... key layout
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
        # passthrough keeps pulumi's usage authoritative instead of
        # plumi duplicating it; works without a [organization] table
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
        # cancel is the one plumi-added subcommand; the passthrough
        # pulumi help does not describe the safety probe around it
        sys.stdout.write(
            "\nplumi cancel\n"
            "  clear a stale lock on the environment's state; refuses "
            "while the holder is a live local process\n",
        )
        sys.stdout.flush()

    def with_secrets_provider(
        self,
        args: list[str],
        environment: Environment,
    ) -> list[str]:
        # pulumi accepts the flag on up and stack init; everything
        # else uses the provider recorded at init
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
        # env = envo environment = stack name: passing the derived stack
        # keeps pulumi from prompting for one on a fresh workspace
        stack_aware_subcommands = {
            "up", "preview", "refresh", "destroy", "import",
            "watch", "config", "stack", "cancel",
        }
        if not args or args[0] not in stack_aware_subcommands:
            return args

        for arg in args:
            if arg in {"-s", "-S", "--stack"} or arg.startswith("--stack="):
                return args

        # these name their stack positionally; pulumi rejects a
        # --stack flag next to the positional ("only one of --stack
        # or argument stack name may be specified")
        if args[0] == "stack" and len(args) > 1 and args[1] in {
            "init", "select", "rm", "rename",
        }:
            return args

        return [*args, "--stack", environment.name]

    def with_refresh(self, args: list[str]) -> list[str]:
        # up aligns tracked resources with real infrastructure before
        # diffing, so console edits between deploys revert to code
        if not args or args[0] != "up":
            return args

        # an explicit refresh flag means the caller owns its semantics
        for arg in args:
            if arg == "--refresh" or arg.startswith("--refresh="):
                return args

        return [*args, "--refresh"]

    def _ensure_stack(self, args: list[str], environment: Environment) -> int | None:
        # up and preview are the first-run entry points, so a missing
        # stack gets inited; destroy/refresh/watch keep failing on one
        # — there is nothing to destroy, refresh or watch yet
        if not args or args[0] not in {"up", "preview"}:
            return None

        # an explicit stack flag means the user owns stack choice
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

        # the payload may be preceded by login banners or warnings;
        # json starts at the first bracket line and may span lines
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

        # local state keeps passphrase-encrypted secrets prompt-free;
        # real environments use awskms and never see a passphrase
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
        # deleting a live holder's lock enables concurrent state
        # mutation; refuse while the holder is provably alive here
        if environment.uses_local_state:
            # the file backend's lock root is not reliably known from
            # the wrapper; pulumi cancel itself owns that case
            return None

        holder: LockHolder | None
        if newest_holder is None:
            try:
                holder = locks.newest_lock_holder(environment)
            except (BotoCoreError, ClientError):
                # an unreadable bucket must not block the cancel; the
                # pulumi cancel call surfaces the same problem itself
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

        # a remote or dead holder cannot be verified from here; cancel
        # with a trace of who held the lock
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

        # a killed run can leave half-written state; point at the
        # recovery order before the user retries the real command
        sys.stderr.write(
            "plumi: state may be partial after a killed run; run "
            "`plumi refresh`, then retry your command\n",
        )
        sys.stderr.flush()
        return return_code

    def run_pulumi(self, args: list[str], environment: Environment) -> int:
        # interactive terminals keep pulumi's native output; captured
        # non-interactive output can be inspected for remediation
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

        # pulumi reports a missing state bucket as a bare NoSuchBucket
        # blob error; name the derived bucket and the bootstrap path
        # instead of leaving the reader to decode it
        if result.returncode != 0 and "NoSuchBucket" in (
            (result.stdout or "") + (result.stderr or "")
        ):
            sys.stderr.write(
                f"plumi: state bucket s3://{environment.state_bucket} does not "
                "exist — run setup_aws_environment from the code repo once "
                "to bootstrap the org\n",
            )
            sys.stderr.flush()

        # a locked stack names its holder in pulumi's own sentence;
        # repeat it with the recovery command instead of leaving the
        # reader to search pulumi's docs
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
        # auto-select when exactly one stack exists, auto-init when
        # none do; anything more ambiguous is left to the user
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
        # zero stacks: init the environment's stack and retry once
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
