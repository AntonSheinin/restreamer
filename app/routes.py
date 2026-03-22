from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response, StreamingResponse

from app.dependencies import ChannelManagerDep, FileServiceDep
from app.models import ChannelStatus, HealthResponse
from app.services.worker import ActiveStreamConflict

router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    return HealthResponse()


@router.get("/channels", response_model=list[ChannelStatus], tags=["channels"])
async def list_channels(channel_manager: ChannelManagerDep) -> list[ChannelStatus]:
    return channel_manager.list_statuses()


@router.get("/channels/{channel}", response_model=ChannelStatus, tags=["channels"])
async def channel_status(channel: str, channel_manager: ChannelManagerDep) -> ChannelStatus:
    status_model = channel_manager.get_status(channel)
    if status_model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    return status_model


@router.get("/channels/{channel}/hls/index.m3u8", tags=["hls"])
async def hls_playlist(
    channel: str,
    channel_manager: ChannelManagerDep,
    file_service: FileServiceDep,
) -> Response:
    channel_config = channel_manager.get_channel(channel)
    if channel_config is None or channel_config.output_format != "hls":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")

    playlist_path = file_service.playlist_path(channel)
    if not await file_service.playlist_exists(channel):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="playlist is not ready",
        )

    try:
        content = await file_service.read_bytes(playlist_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="playlist is not ready",
        ) from exc

    return Response(
        content=content,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/channels/{channel}/hls/{asset_name}", tags=["hls"])
async def hls_asset(
    channel: str,
    asset_name: str,
    channel_manager: ChannelManagerDep,
    file_service: FileServiceDep,
) -> Response:
    channel_config = channel_manager.get_channel(channel)
    if channel_config is None or channel_config.output_format != "hls":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")

    asset_path = file_service.resolve_hls_asset_path(channel, asset_name)
    if asset_path is None or not await file_service.file_exists(asset_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")

    try:
        content = await file_service.read_bytes(asset_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found") from exc

    return Response(
        content=content,
        media_type="video/mp2t",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/channels/{channel}/stream.ts", tags=["tshttp"])
async def ts_stream(channel: str, channel_manager: ChannelManagerDep) -> StreamingResponse:
    channel_config = channel_manager.get_channel(channel)
    if channel_config is None or channel_config.output_format != "tshttp":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")

    worker = channel_manager.get_tshttp_worker(channel)
    if worker is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")

    try:
        stream = await worker.open_stream()
    except ActiveStreamConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    response = StreamingResponse(stream, media_type="video/mp2t")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Connection"] = "keep-alive"
    return response
