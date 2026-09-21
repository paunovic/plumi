import json
from dataclasses import dataclass
from pathlib import Path

import botocore.session

from plumi.environment import Environment


@dataclass(frozen=True)
class LockHolder:
    """the holder identity recorded in a diy-backend lock blob."""

    username: str
    hostname: str
    pid: int


def newest_lock_holder(
    environment: Environment,
    client=None,
) -> LockHolder | None:
    # diy-backend locks live at .pulumi/locks/<stack>/<lockID>.json in
    # the state bucket; the newest blob is the lock cancel removes
    if client is None:
        client = botocore.session.get_session().create_client("s3")

    listing = client.list_objects_v2(
        Bucket=environment.state_bucket,
        Prefix=f".pulumi/locks/{environment.name}/",
    )
    contents = listing.get("Contents", [])
    if not contents:
        return None

    newest = max(contents, key=lambda entry: entry["LastModified"])
    body = client.get_object(
        Bucket=environment.state_bucket,
        Key=newest["Key"],
    )["Body"].read()
    return parse_lock_holder(body)


def parse_lock_holder(body: bytes | str) -> LockHolder | None:
    # an unreadable blob must not block a cancel; no holder reads as
    # no safety check rather than a refused command
    try:
        document = json.loads(body)
        return LockHolder(
            username=document["username"],
            hostname=document["hostname"],
            pid=int(document["pid"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


def process_is_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()
