from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

from app.core.errors import Forbidden, NotFound
from app.services.storage_service import (
    BlobFileNotFound,
    BlobStorage,
    LocalStorage,
    get_storage,
    verify_file_token,
)

router = APIRouter(prefix="/files", tags=["files"])


def _inline_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "") or "file"
    return f"inline; filename=\"{ascii_name}\"; filename*=utf-8''{quote(filename)}"


@router.get("/{token}", include_in_schema=False)
async def download_file(token: str) -> Response:
    """Serves local / Vercel Blob files for signed, short-lived tokens."""
    payload = verify_file_token(token)
    if payload is None:
        raise Forbidden("This link is invalid or has expired", code="link_expired")
    storage = get_storage()
    headers = {"Cache-Control": "private, no-store"}
    if isinstance(storage, BlobStorage):
        try:
            data = await storage.read(payload["k"])
        except BlobFileNotFound:
            raise NotFound("File not found") from None
        headers["Content-Disposition"] = _inline_disposition(payload["n"])
        return Response(content=data, media_type=payload["t"], headers=headers)
    if not isinstance(storage, LocalStorage):
        raise NotFound("Not found")
    path = storage.path_for(payload["k"])
    if not path.is_file():
        raise NotFound("File not found")
    return FileResponse(
        path,
        media_type=payload["t"],
        filename=payload["n"],
        content_disposition_type="inline",
        headers=headers,
    )
