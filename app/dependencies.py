from secrets import compare_digest
from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, Query, Request, status

from app.config import Settings
from app.services.files import FileService
from app.services.worker import ChannelManager


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_file_service(request: Request) -> FileService:
    return cast(FileService, request.app.state.file_service)


def get_channel_manager(request: Request) -> ChannelManager:
    return cast(ChannelManager, request.app.state.channel_manager)


def require_access_token(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    access_token: Annotated[str | None, Query()] = None,
) -> str | None:
    configured_token = settings.access_token.get_secret_value() if settings.access_token else None
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="access token is not configured",
        )

    if access_token and compare_digest(access_token, configured_token):
        return access_token

    header_token: str | None = None
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            header_token = token

    if header_token and compare_digest(header_token, configured_token):
        return None

    if access_token or header_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token or access_token query parameter",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing bearer token or access_token query parameter",
        headers={"WWW-Authenticate": "Bearer"},
    )


SettingsDep = Annotated[Settings, Depends(get_settings)]
FileServiceDep = Annotated[FileService, Depends(get_file_service)]
ChannelManagerDep = Annotated[ChannelManager, Depends(get_channel_manager)]
AccessTokenDep = Annotated[str | None, Depends(require_access_token)]
