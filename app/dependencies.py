from secrets import compare_digest
from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, Request, status

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
) -> None:
    configured_token = settings.access_token.get_secret_value() if settings.access_token else None
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="access token is not configured",
        )

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not compare_digest(token, configured_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


SettingsDep = Annotated[Settings, Depends(get_settings)]
FileServiceDep = Annotated[FileService, Depends(get_file_service)]
ChannelManagerDep = Annotated[ChannelManager, Depends(get_channel_manager)]
AccessTokenDep = Annotated[None, Depends(require_access_token)]
