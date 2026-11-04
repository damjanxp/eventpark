"""S3 wrapper: put, head, presigned download/upload URLs.

Keys are deterministic where possible (qr/{ticket_id}.png, reports/{event_id}/{report_id}.csv): writing the same key
twice just overwrites the same object, so a worker that processes a message twice does no harm (idempotent by nature).
Buckets are private; browsers only ever get short-lived presigned URLs.
"""

from dataclasses import dataclass
from typing import Any

from botocore.config import Config
from botocore.exceptions import ClientError

from eventpark.core import aws
from eventpark.core.config import Settings


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str | None


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.bucket_files
        self._region = settings.aws_region
        self._public_endpoint = settings.s3_public_endpoint_url

    def _client(self) -> Any:
        return aws.client("s3", self._region)

    def _signer(self) -> Any:
        """Presigning client: same credentials, but the endpoint the BROWSER will use (it's part of the signature)."""
        import boto3  # local import: only presigning needs a separately configured client

        return boto3.client(
            "s3",
            region_name=self._region,
            endpoint_url=self._public_endpoint,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"} if self._public_endpoint else {}),
        )

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        await aws.run(self._client().put_object, Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)

    async def head(self, key: str) -> ObjectInfo | None:
        try:
            meta = await aws.run(self._client().head_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return ObjectInfo(size=int(meta["ContentLength"]), content_type=meta.get("ContentType"))

    async def get(self, key: str) -> bytes:
        response = await aws.run(self._client().get_object, Bucket=self.bucket, Key=key)
        data: bytes = await aws.run(response["Body"].read)
        return data

    def presigned_get(self, key: str, *, expires: int = 300, filename: str | None = None) -> str:
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        url: str = self._signer().generate_presigned_url("get_object", Params=params, ExpiresIn=expires)
        return url

    def presigned_put(self, key: str, *, content_type: str, expires: int = 300) -> str:
        url: str = self._signer().generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type}, ExpiresIn=expires
        )
        return url
