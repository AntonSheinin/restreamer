from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import FileResponse

from app.dependencies import FileServiceDep, WorkerServiceDep
from app.models import HealthResponse, WorkerStatus

router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    return HealthResponse()


@router.get("/status", response_model=WorkerStatus, tags=["health"])
async def status_endpoint(worker_service: WorkerServiceDep) -> WorkerStatus:
    return worker_service.get_status()


@router.get("/hls/index.m3u8", tags=["hls"])
async def playlist(file_service: FileServiceDep) -> FileResponse:
    playlist_path = file_service.playlist_path
    if not await file_service.playlist_exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="playlist is not ready",
        )

    response = FileResponse(
        path=playlist_path,
        media_type="application/vnd.apple.mpegurl",
        filename=playlist_path.name,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/hls/{asset_name}", tags=["hls"])
async def hls_asset(asset_name: str, file_service: FileServiceDep) -> Response:
    asset_path = file_service.resolve_asset_path(asset_name)
    if asset_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")

    if not await file_service.file_exists(asset_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")

    media_type = "video/mp2t"

    response = FileResponse(
        path=Path(asset_path),
        media_type=media_type,
        filename=asset_path.name,
    )
    response.headers["Cache-Control"] = "no-cache"
    return response
