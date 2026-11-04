"""moto's S3 + SQS as a real local HTTP server for tests (like ElasticMQ/SeaweedFS, but in-process and disposable).

A real endpoint (not moto's in-process patching) matters because presigned URLs are fetched with plain HTTP, exactly
like a browser would, and because boto3 runs in worker threads.
"""

import json
import socket
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import boto3
import pytest
from moto.server import ThreadedMotoServer

from eventpark.core import aws

QUEUES = {"entry-events": 30, "exit-events": 30, "notifications": 60, "reports": 300, "maintenance": 60}
BUCKET = "eventpark-files-test"


@dataclass
class AwsEnv:
    endpoint: str
    queue_urls: dict[str, str]
    bucket: str = BUCKET

    def sqs(self):  # type: ignore[no-untyped-def]
        return boto3.client("sqs", region_name="eu-central-1", endpoint_url=self.endpoint)

    def s3(self):  # type: ignore[no-untyped-def]
        return boto3.client("s3", region_name="eu-central-1", endpoint_url=self.endpoint)

    def drain(self, queue: str) -> list[str]:
        """Receive and delete everything currently in a queue; returns the bodies."""
        bodies: list[str] = []
        sqs = self.sqs()
        while True:
            messages = sqs.receive_message(
                QueueUrl=self.queue_urls[queue], MaxNumberOfMessages=10, WaitTimeSeconds=0
            ).get("Messages", [])
            if not messages:
                return bodies
            for m in messages:
                bodies.append(m["Body"])
                sqs.delete_message(QueueUrl=self.queue_urls[queue], ReceiptHandle=m["ReceiptHandle"])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def moto_server() -> Iterator[str]:
    port = _free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
def aws_env(moto_server: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[AwsEnv]:
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "eu-central-1",
        "AWS_ENDPOINT_URL_S3": moto_server,
        "AWS_ENDPOINT_URL_SQS": moto_server,
    }.items():
        monkeypatch.setenv(key, value)
    aws.client.cache_clear()
    sqs = boto3.client("sqs", region_name="eu-central-1", endpoint_url=moto_server)
    s3 = boto3.client("s3", region_name="eu-central-1", endpoint_url=moto_server)
    if BUCKET not in [b["Name"] for b in s3.list_buckets().get("Buckets", [])]:
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-central-1"})
    # Fresh queues per test (unique names): no message from one test can leak into the next.
    suffix = uuid.uuid4().hex[:8]
    urls = {}
    for name, visibility in QUEUES.items():
        dlq = sqs.create_queue(QueueName=f"{name}-{suffix}-dlq")["QueueUrl"]
        dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        redrive = json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"})
        urls[name] = sqs.create_queue(
            QueueName=f"{name}-{suffix}", Attributes={"VisibilityTimeout": str(visibility), "RedrivePolicy": redrive}
        )["QueueUrl"]
        urls[f"{name}-dlq"] = dlq
    env = AwsEnv(endpoint=moto_server, queue_urls=urls)
    yield env
    aws.client.cache_clear()
