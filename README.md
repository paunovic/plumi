# plumi

plumi wraps pulumi for multi-environment stacks. The state backend,
stack name and secrets provider all come from the active aws
profile and the project directory. plumi logs in and hands the
rest to pulumi.

## Install

```
uv tool install git+https://github.com/paunovic/plumi
```

## What it needs

A `[organization]` table in the pyproject.toml at or above the
pulumi project:

```toml
[organization]
name = "acme"
domain = "acme.io"
```

And an aws profile, either directly through `AWS_PROFILE` or via
envo (`ENVO_ENVIRONMENT`). The profile name is the environment:
`envo qa …` means environment `qa`.

## What it does

From the environment and the `[organization]` table plumi derives
the state bucket `s3://pulumi-state-<env>-<domain-dashed>`, the
stack name and the region, logs into the bucket, and forwards the
remaining arguments to pulumi. Commands that take a stack get
`--stack <env>` added. `up` and `preview` create the stack on
first run, with `awskms` as the secrets provider on real
environments.

`up` also gets `--refresh`. Console edits between deploys revert
to code. Resources created by hand and never imported stay
invisible to pulumi either way.

Anything plumi does not recognize passes through untouched:
`plumi <pulumi args>` behaves like `pulumi <pulumi args>` with the
environment wired up.

## Recovering from a killed run

A killed `plumi up` can leave a state lock behind. The next run
dies with the locked-stack error. plumi prints the holder and the
fix:

```
$ plumi cancel
```

cancel refuses while the holder is a live process on this machine
(the lock blob records hostname and pid). Anything else would allow
concurrent state mutation. After canceling, refresh adopts
whatever the killed run created:

```
$ plumi refresh && plumi up
```

## Usage

```
$ cd my-project/infrastructure/pulumi/api
$ envo qa plumi up
```

plumi logs into `s3://pulumi-state-qa-acme-io`, selects stack
`qa`, and runs the update. `plumi` with no arguments or `--help`
prints pulumi usage and works from any directory.

## Development

```
uv sync
uv run ruff check src tests
uv run ty check src
uv run pytest tests
```
