import re
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from pydantic import ValidationError

from app.dependencies import AccessTokenDep, ChannelManagerDep, FileServiceDep, SettingsDep
from app.models import ChannelStatus, HealthResponse, StatsResponse
from app.services.worker import ActiveStreamConflict

router = APIRouter()
BYTE_RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")


def _cache_headers(cache_control: str, content_length: int) -> dict[str, str]:
    return {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache_control,
        "Content-Length": str(content_length),
    }


def _parse_byte_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if range_header is None:
        return None

    match = BYTE_RANGE_PATTERN.fullmatch(range_header.strip())
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{file_size}"},
            detail="unsupported byte range",
        )

    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{file_size}"},
            detail="invalid byte range",
        )

    if not start_text:
        suffix_length = int(end_text)
        if suffix_length == 0:
            raise HTTPException(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                headers={"Content-Range": f"bytes */{file_size}"},
                detail="invalid byte range",
            )
        start = max(file_size - suffix_length, 0)
        end = file_size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1

    if file_size == 0 or start >= file_size or end < start:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{file_size}"},
            detail="byte range not satisfiable",
        )

    return start, min(end, file_size - 1)


def _range_headers(cache_control: str, file_size: int, start: int, end: int) -> dict[str, str]:
    content_length = end - start + 1
    return {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache_control,
        "Content-Length": str(content_length),
        "Content-Range": f"bytes {start}-{end}/{file_size}",
    }


def _playlist_not_ready(status_model: ChannelStatus | None) -> HTTPException:
    if status_model is None:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="playlist is not ready",
        )

    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "message": "playlist is not ready",
            "state": status_model.state,
            "restart_count": status_model.restart_count,
            "last_error": status_model.last_error,
            "pid": status_model.pid,
        },
    )


def _add_access_token_to_playlist(content: bytes, access_token: str | None) -> bytes:
    if access_token is None:
        return content

    token_query = f"access_token={quote(access_token, safe='')}"
    playlist = content.decode("utf-8")
    rewritten_lines: list[str] = []
    for line in playlist.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            rewritten_lines.append(line)
            continue

        separator = "&" if "?" in line else "?"
        rewritten_lines.append(f"{line}{separator}{token_query}")

    suffix = "\n" if playlist.endswith("\n") else ""
    return ("\n".join(rewritten_lines) + suffix).encode("utf-8")


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health(_access_token: AccessTokenDep) -> HealthResponse:
    return HealthResponse()


@router.get("/stats", response_model=StatsResponse, tags=["stats"])
async def stats(
    _access_token: AccessTokenDep,
    channel_manager: ChannelManagerDep,
) -> StatsResponse:
    return StatsResponse(
        active_channels=channel_manager.count_active_channels(),
        consumed_channels=channel_manager.count_consumed_channels(),
    )


@router.get("/channels", response_model=list[ChannelStatus], tags=["channels"])
async def list_channels(
    _access_token: AccessTokenDep,
    channel_manager: ChannelManagerDep,
) -> list[ChannelStatus]:
    return channel_manager.list_statuses()


@router.get("/channels/{channel}", response_model=ChannelStatus, tags=["channels"])
async def channel_status(
    channel: str,
    _access_token: AccessTokenDep,
    channel_manager: ChannelManagerDep,
) -> ChannelStatus:
    status_model = channel_manager.get_status(channel)
    if status_model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    return status_model


@router.post("/channels/{channel}/reload", response_model=ChannelStatus, tags=["channels"])
async def reload_channel(
    channel: str,
    _access_token: AccessTokenDep,
    settings: SettingsDep,
    channel_manager: ChannelManagerDep,
) -> ChannelStatus:
    try:
        status_model = await channel_manager.reload_channel(channel, settings.streams_config)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"streams config not found: {settings.streams_config}",
        ) from exc
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if status_model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    return status_model


@router.get("/channels/{channel}/hls", tags=["hls"])
@router.get("/channels/{channel}/hls/", tags=["hls"])
@router.get("/channels/{channel}/hls/index.m3u8", tags=["hls"])
async def hls_playlist(
    channel: str,
    _access_token: AccessTokenDep,
    channel_manager: ChannelManagerDep,
    file_service: FileServiceDep,
) -> Response:
    channel_config = channel_manager.get_channel(channel)
    if channel_config is None or channel_config.output_format != "hls":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")

    status_model = channel_manager.get_status(channel)
    playlist_path = file_service.playlist_path(channel)
    if not await file_service.playlist_exists(channel):
        raise _playlist_not_ready(status_model)

    try:
        content = await file_service.read_cached_playlist(playlist_path)
    except FileNotFoundError as exc:
        raise _playlist_not_ready(status_model) from exc

    content = _add_access_token_to_playlist(content, _access_token)
    return Response(
        content=content,
        media_type="application/vnd.apple.mpegurl",
        headers=_cache_headers("no-store", len(content)),
    )


@router.head("/channels/{channel}/hls", tags=["hls"])
@router.head("/channels/{channel}/hls/", tags=["hls"])
@router.head("/channels/{channel}/hls/index.m3u8", tags=["hls"])
async def hls_playlist_head(
    channel: str,
    _access_token: AccessTokenDep,
    channel_manager: ChannelManagerDep,
    file_service: FileServiceDep,
) -> Response:
    channel_config = channel_manager.get_channel(channel)
    if channel_config is None or channel_config.output_format != "hls":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")

    status_model = channel_manager.get_status(channel)
    playlist_path = file_service.playlist_path(channel)
    if not await file_service.playlist_exists(channel):
        raise _playlist_not_ready(status_model)

    try:
        size = await file_service.file_size(playlist_path)
    except FileNotFoundError as exc:
        raise _playlist_not_ready(status_model) from exc

    return Response(
        content=b"",
        media_type="application/vnd.apple.mpegurl",
        headers=_cache_headers("no-store", size),
    )


@router.get("/channels/{channel}/hls/{asset_name}", tags=["hls"])
async def hls_asset(
    channel: str,
    asset_name: str,
    _access_token: AccessTokenDep,
    channel_manager: ChannelManagerDep,
    file_service: FileServiceDep,
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    channel_config = channel_manager.get_channel(channel)
    if channel_config is None or channel_config.output_format != "hls":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")

    asset_path = file_service.resolve_hls_asset_path(channel, asset_name)
    if asset_path is None or not await file_service.file_exists(asset_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")

    try:
        size = await file_service.file_size(asset_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found") from exc

    byte_range = _parse_byte_range(range_header, size)
    if byte_range is not None:
        start, end = byte_range
        try:
            file = await file_service.open_binary(asset_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found") from exc
        return StreamingResponse(
            file_service.iter_byte_range(file, start, end),
            media_type="video/mp2t",
            headers=_range_headers("no-cache", size, start, end),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
        )

    try:
        file = await file_service.open_binary(asset_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found") from exc
    return StreamingResponse(
        file_service.iter_file(file),
        media_type="video/mp2t",
        headers=_cache_headers("no-cache", size),
    )


@router.head("/channels/{channel}/hls/{asset_name}", tags=["hls"])
async def hls_asset_head(
    channel: str,
    asset_name: str,
    _access_token: AccessTokenDep,
    channel_manager: ChannelManagerDep,
    file_service: FileServiceDep,
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    channel_config = channel_manager.get_channel(channel)
    if channel_config is None or channel_config.output_format != "hls":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")

    asset_path = file_service.resolve_hls_asset_path(channel, asset_name)
    if asset_path is None or not await file_service.file_exists(asset_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found")

    try:
        size = await file_service.file_size(asset_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="asset not found") from exc

    byte_range = _parse_byte_range(range_header, size)
    if byte_range is not None:
        start, end = byte_range
        return Response(
            content=b"",
            media_type="video/mp2t",
            headers=_range_headers("no-cache", size, start, end),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
        )

    return Response(
        content=b"",
        media_type="video/mp2t",
        headers=_cache_headers("no-cache", size),
    )


@router.get("/channels/{channel}/stream.ts", tags=["tshttp"])
async def ts_stream(
    channel: str,
    _access_token: AccessTokenDep,
    channel_manager: ChannelManagerDep,
) -> StreamingResponse:
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
