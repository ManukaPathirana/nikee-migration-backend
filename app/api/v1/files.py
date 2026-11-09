from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.core.config import settings
from app.core.errors import Forbidden, NotFound
from app.services.storage_service import LocalStorage, get_storage, verify_file_token

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/{token}", include_in_schema=False)
async def download_file(token: str) -> FileResponse:
    """Serves local-storage files for signed, short-lived tokens (dev adapter)."""
    payload = verify_file_token(token)
    if payload is None:
        raise Forbidden("This link is invalid or has expired", code="link_expired")
    storage = get_storage()
    if settings.storage_backend != "local" or not isinstance(storage, LocalStorage):
        raise NotFound("Not found")
    path = storage.path_for(payload["k"])
    if not path.is_file():
        raise NotFound("File not found")
    return FileResponse(
        path,
        media_type=payload["t"],
        filename=payload["n"],
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )
