"""Create the S3 buckets in the local emulator (SeaweedFS). Run by the `init` service in docker-compose.yml.

Local only: on AWS, buckets are created by Terraform (Phase 11), never by the app. That's why this lives in
scripts/ (mounted into the init container) and not in the eventpark package that ships in the image.

Idempotent: running it again when the bucket already exists is fine.
Config comes from the standard AWS environment variables that boto3 reads by itself:
AWS_ENDPOINT_URL_S3, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION, plus our BUCKET_FILES.
"""

import os
import sys
import time

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError


def main() -> int:
    bucket = os.environ["BUCKET_FILES"]
    s3 = boto3.client("s3")

    # SeaweedFS may still be starting even after its healthcheck passed once; retry briefly.
    for attempt in range(1, 11):
        try:
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": os.environ["AWS_DEFAULT_REGION"]},
            )
            print(f"created bucket {bucket}")
            return 0
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                print(f"bucket {bucket} already exists")
                return 0
            raise
        except EndpointConnectionError:
            print(f"S3 not reachable yet (attempt {attempt}/10), retrying in 1 s", file=sys.stderr)
            time.sleep(1)
    print("S3 emulator never became reachable", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
