# plumi

plumi is a thin wrapper around Pulumi for running the same stacks
against several environments. Instead of repeating the state
backend, stack name, and secrets provider on every command, plumi
derives all three from the active AWS profile and the current
project directory, logs in, and hands everything else to Pulumi.

## Install

```
uv tool install git+https://github.com/paunovic/plumi
```

## What plumi needs

A `[organization]` table in the pyproject.toml at or above the Pulumi project:

```toml
[organization]
name = "acme"
domain = "acme.io"
```

And an AWS profile, either directly via `AWS_PROFILE` or through
envo (`ENVO_ENVIRONMENT`). The profile name is the environment
name: `envo qa …` means environment `qa`.

## How plumi runs a command

From the environment and the `[organization]` table plumi derives the
state bucket `s3://pulumi-state-<env>.<domain>`, the stack name,
and the region, logs into the bucket, and forwards the remaining
arguments to Pulumi. Commands that take a stack get `--stack <env>`
added automatically, and `up` or `preview` create the stack on
first run, with `awskms` as the secrets provider on real
environments.

`up` also gets `--refresh` added automatically, so every deploy
refreshes first: tracked resources are aligned with real
infrastructure before diffing, and changes made in the console
between deploys are reverted to code. Resources created by hand
and never imported are invisible to Pulumi either way.

Anything plumi does not recognize is passed through untouched: `plumi
<pulumi args>` behaves like `pulumi <pulumi args>` with the
environment wired up.

## Recovering from a killed run

A killed `plumi up` can leave a state lock behind; the next run dies with
pulumi's locked-stack error, which plumi translates into the holder's
details and the fix:

```
$ plumi cancel
```

cancel refuses while the lock's holder is a live process on this machine
(the lock blob records hostname and pid) - deleting a live holder's lock
enables concurrent mutation. After canceling, refresh adopts whatever the
killed run created, then the command retries:

```
$ plumi refresh && plumi up
```

## Usage

```
$ cd my-project/infrastructure/pulumi/api
$ envo qa plumi up
```

plumi logs into `s3://pulumi-state-qa.acme.io`, selects stack `qa`,
and runs the update. `plumi` with no arguments or `--help` prints
Pulumi's usage and works from any directory.

## Development

```
uv sync
uv run ruff check src tests
uv run ty check src
uv run pytest tests
```
