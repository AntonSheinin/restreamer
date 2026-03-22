from typing import Annotated, cast

from fastapi import Depends, Request

from app.config import Settings
from app.services.files import FileService
from app.services.worker import ChannelManager


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_file_service(request: Request) -> FileService:
    return cast(FileService, request.app.state.file_service)


def get_channel_manager(request: Request) -> ChannelManager:
    return cast(ChannelManager, request.app.state.channel_manager)


SettingsDep = Annotated[Settings, Depends(get_settings)]
FileServiceDep = Annotated[FileService, Depends(get_file_service)]
ChannelManagerDep = Annotated[ChannelManager, Depends(get_channel_manager)]
