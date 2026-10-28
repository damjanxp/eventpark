# /// script
# requires-python = ">=3.13"
# dependencies = ["boto3"]
# ///
"""Smoke test: prove the local AWS emulators behave like S3 and SQS before the app relies on them.

    uv run --env-file .env scripts/smoke_aws_emulators.py        (from the repo root, stack running)

(The block at the top is "inline script metadata": `uv run` reads it and installs boto3 for this script
only, so no project or virtualenv is needed.)

It uses the plain boto3 calls our workers will use in Phase 6, configured only by the standard AWS env vars
(AWS_ENDPOINT_URL_S3 / _SQS, keys, region). Pointed at real AWS (endpoints unset), the same script would work
unchanged: that's the "same code, different config" rule.

Checks
  S3:  bucket exists, put -> get (same bytes), presigned GET URL works over plain HTTP, delete
  SQS: every queue + its DLQ exist with the expected visibility timeout and redrive policy (maxReceiveCount 5),
       send -> receive (same body) -> delete -> queue empty again
"""

import json
import os
import sys
import urllib.request
import uuid
from collections.abc import Callable

import boto3
from botocore.config import Config

# Same values as docker/elasticmq/elasticmq.conf and docs/02-architecture.md §6 (visibility timeout in seconds).
QUEUES = {"entry-events": 30, "exit-events": 30, "notifications": 60, "reports": 300, "maintenance": 60}
MAX_RECEIVE_COUNT = 5

results: list[tuple[str, bool, str]] = []


class CheckFailed(Exception):
    pass


def expect(condition: bool, message: object) -> None:
    """Like `assert`, but never skipped: Python drops assert statements when run with -O."""
    if not condition:
        raise CheckFailed(str(message))


def check(name: str, fn: Callable[[], str]) -> None:
    try:
        detail = fn()
        results.append((name, True, detail))
    except Exception as exc:
        results.append((name, False, f"{type(exc).__name__}: {exc}"))


def s3_checks() -> None:
    bucket = os.environ["BUCKET_FILES"]
    s3 = boto3.client("s3")
    key = f"smoke/{uuid.uuid4()}.txt"
    body = b"hello from the EventPark smoke test"

    check("S3 bucket exists", lambda: (s3.head_bucket(Bucket=bucket), bucket)[1])

    def put_get() -> str:
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/plain")
        got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        expect(got == body, f"got {got!r}")
        return f"{len(body)} bytes round-tripped ({key})"

    check("S3 put -> get", put_get)

    def presigned() -> str:
        # Presigned URLs embed the host in the signature, so sign with the address the *browser* will use
        # (S3_PUBLIC_ENDPOINT_URL), not the Docker-internal one. See docs/02-architecture.md §14 "Gotcha".
        public = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_PUBLIC_ENDPOINT_URL") or None,
            config=Config(signature_version="s3v4"),
        )
        url = public.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=60)
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 (our own presigned URL)
            got = response.read()
        expect(got == body, f"got {got!r}")
        return "plain HTTP GET with the signed URL works (expires in 60 s)"

    check("S3 presigned GET", presigned)

    def delete() -> str:
        s3.delete_object(Bucket=bucket, Key=key)
        listed = s3.list_objects_v2(Bucket=bucket, Prefix=key).get("KeyCount", 0)
        expect(listed == 0, f"still listed: {listed}")
        return "object gone"

    check("S3 delete", delete)


def sqs_checks() -> None:
    sqs = boto3.client("sqs")

    for name, visibility in QUEUES.items():

        def queue_config(name: str = name, visibility: int = visibility) -> str:
            url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
            sqs.get_queue_url(QueueName=f"{name}-dlq")
            attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
            expect(int(attrs["VisibilityTimeout"]) == visibility, f"visibility {attrs['VisibilityTimeout']}")
            redrive = json.loads(attrs["RedrivePolicy"])
            expect(redrive["deadLetterTargetArn"].endswith(f":{name}-dlq"), redrive)
            expect(int(redrive["maxReceiveCount"]) == MAX_RECEIVE_COUNT, redrive)
            return f"visibility {visibility}s, DLQ {name}-dlq after {MAX_RECEIVE_COUNT} receives"

        check(f"SQS queue {name}", queue_config)

    def send_receive_delete() -> str:
        url = sqs.get_queue_url(QueueName="maintenance")["QueueUrl"]
        body = json.dumps({"type": "smoke.test", "message_id": str(uuid.uuid4())})
        sqs.send_message(QueueUrl=url, MessageBody=body)
        messages = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=1, WaitTimeSeconds=2).get("Messages", [])
        expect(bool(messages), "no message received")
        expect(messages[0]["Body"] == body, messages[0]["Body"])
        sqs.delete_message(QueueUrl=url, ReceiptHandle=messages[0]["ReceiptHandle"])
        left = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=1, WaitTimeSeconds=1).get("Messages", [])
        expect(not left, f"message still there after delete: {left}")
        return "send -> receive -> delete on 'maintenance', queue empty afterwards"

    check("SQS send/receive/delete", send_receive_delete)


def main() -> int:
    s3_checks()
    sqs_checks()
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<28} {detail}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
