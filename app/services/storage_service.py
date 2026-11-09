"""Private file storage.

- local (dev): files under LOCAL_STORAGE_DIR; downloads go through
  `GET /api/v1/files/{token}` with a short-lived signed token.
- s3 (prod): any S3-compatible bucket; downloads are presigned URLs.

`download_url` returns either an absolute URL (s3) or an API-relative path
starting with "/" (local) that clients join with their API origin.
"""

import asyncio
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from jose import JWTError, jwt

from app.core.config import settings
from app.core.security import utcnow

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

        await asyncio.to_thread(_write)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.path_for(key).unlink, True)

    async def download_url(self, key: str, filename: str, content_type: str) -> str:
        return f"{settings.api_v1_prefix}/files/{_sign_file_token(key, filename, content_type)}"


# ---------------------------------------------------------------- s3


class S3Storage:
    def __init__(self) -> None:
        import boto3

        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=settings.s3_access_key_id or None,
            aws_secret_access_key=settings.s3_secret_access_key or None,
            region_name=settings.s3_region or None,
        )

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

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


@lru_cache
def get_storage() -> Storage:
    if settings.storage_backend == "s3":
        return S3Storage()
    return LocalStorage(settings.resolve_path(settings.local_storage_dir))
