"""Private file storage.

- local (dev): files under LOCAL_STORAGE_DIR; downloads go through
  `GET /api/v1/files/{token}` with a short-lived signed token.
- s3 (prod): any S3-compatible bucket; downloads are presigned URLs.

`download_url` returns either an absolute URL (s3) or an API-relative path
starting with "/" (local) that clients join with their API origin.
"""

import asyncio
import logging
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from jose import JWTError, jwt

from app.core.config import settings
from app.core.errors import AppError
from app.core.security import utcnow

logger = logging.getLogger(__name__)


class StorageUnavailable(AppError):
    """The file store can't be written to (misconfigured, or a read-only disk)."""

    status_code = 503
    code = "storage_unavailable"

    def __init__(self) -> None:
        super().__init__("We couldn't save the file right now. Please try again later.")


ALLOWED_TYPES = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}


def sniff_file_type(data: bytes) -> str | None:
    """Detect PDF/PNG/JPEG from the file's first bytes (never trust the client's type)."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


class Storage(Protocol):
    async def save(self, key: str, data: bytes, content_type: str) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def read(self, key: str) -> bytes: ...
    async def download_url(self, key: str, filename: str, content_type: str) -> str: ...


# ---------------------------------------------------------------- local


def _sign_file_token(key: str, filename: str, content_type: str) -> str:
    payload = {
        "type": "file",
        "k": key,
        "n": filename,
        "t": content_type,
        "exp": int((utcnow() + timedelta(seconds=settings.signed_url_expire_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_file_token(token: str) -> dict[str, Any] | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    return payload if payload.get("type") == "file" else None


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path_for(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid storage key")
        return path

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        path = self.path_for(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        try:
            await asyncio.to_thread(_write)
        except OSError as e:
            # e.g. serverless hosts (Vercel) have a read-only disk: STORAGE_BACKEND=s3 is needed.
            logger.error("local storage write failed for %s: %s", key, e)
            raise StorageUnavailable() from e

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.path_for(key).unlink, True)

    async def read(self, key: str) -> bytes:
        return await asyncio.to_thread(self.path_for(key).read_bytes)

    async def download_url(self, key: str, filename: str, content_type: str) -> str:
        return f"{settings.api_v1_prefix}/files/{_sign_file_token(key, filename, content_type)}"


# ---------------------------------------------------------------- s3


class S3Storage:
    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=settings.s3_access_key_id or None,
            aws_secret_access_key=settings.s3_secret_access_key or None,
            region_name=settings.s3_region or None,
            # Path-style + SigV4: works with AWS and S3-compatible hosts (Supabase, R2, MinIO).
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        try:
            await asyncio.to_thread(
                self.client.put_object,
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        except Exception as e:  # botocore errors: bad keys, missing bucket, network
            logger.error("s3 upload failed for %s: %s", key, e)
            raise StorageUnavailable() from e

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

    async def read(self, key: str) -> bytes:
        obj = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(obj["Body"].read)

    async def download_url(self, key: str, filename: str, content_type: str) -> str:
        return await asyncio.to_thread(
            self.client.generate_presigned_url,
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": f'inline; filename="{filename}"',
            },
            ExpiresIn=settings.signed_url_expire_seconds,
        )


# ---------------------------------------------------------------- vercel blob

BLOB_API_URL = "https://vercel.com/api/blob"
BLOB_API_VERSION = "13"


class BlobFileNotFound(Exception):
    pass


class BlobStorage:
    """A private Vercel Blob store, over its HTTP API.

    Private blobs need the token to read, so downloads go through the same short-lived
    signed `/files/{token}` links as local storage and the API streams the bytes.
    """

    def __init__(self, token: str) -> None:
        # Token shape: vercel_blob_rw_<storeId>_<secret>
        parts = token.split("_")
        if len(parts) < 5 or not parts[3]:
            raise ValueError("BLOB_READ_WRITE_TOKEN is not a Vercel Blob read-write token")
        self.token = token
        self.store_id = parts[3]
        self.transport: Any = None  # tests swap in an httpx.MockTransport

    def _client(self) -> Any:
        import httpx

        return httpx.AsyncClient(timeout=30, transport=self.transport)

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.token}",
            "x-api-version": BLOB_API_VERSION,
            "x-vercel-blob-store-id": self.store_id,
        }

    def url_for(self, key: str) -> str:
        return f"https://{self.store_id}.private.blob.vercel-storage.com/{key}"

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        import httpx

        headers = {
            **self._headers(),
            "x-vercel-blob-access": "private",
            "x-content-type": content_type,
            "x-add-random-suffix": "0",
            "x-allow-overwrite": "1",
        }
        try:
            async with self._client() as client:
                resp = await client.put(
                    f"{BLOB_API_URL}/", params={"pathname": key}, content=data, headers=headers
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            detail = e.response.text[:300] if isinstance(e, httpx.HTTPStatusError) else ""
            logger.error("blob upload failed for %s: %s %s", key, e, detail)
            raise StorageUnavailable() from e

    async def delete(self, key: str) -> None:
        async with self._client() as client:
            resp = await client.post(
                f"{BLOB_API_URL}/delete",
                json={"urls": [self.url_for(key)]},
                headers=self._headers(),
            )
        if resp.is_error:
            logger.warning("blob delete failed for %s: %s", key, resp.status_code)

    async def read(self, key: str) -> bytes:
        async with self._client() as client:
            resp = await client.get(
                self.url_for(key), headers={"authorization": f"Bearer {self.token}"}
            )
        if resp.status_code == 404:
            raise BlobFileNotFound(key)
        resp.raise_for_status()
        return resp.content

    async def download_url(self, key: str, filename: str, content_type: str) -> str:
        return f"{settings.api_v1_prefix}/files/{_sign_file_token(key, filename, content_type)}"


@lru_cache
def get_storage() -> Storage:
    if settings.storage_backend == "s3":
        return S3Storage()
    if settings.storage_backend == "blob" or settings.blob_read_write_token:
        return BlobStorage(settings.blob_read_write_token)
    return LocalStorage(settings.resolve_path(settings.local_storage_dir))
