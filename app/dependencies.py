from typing import Annotated, cast

from fastapi import Depends, Request

from app.config import Settings
from app.services.files import FileService
from app.services.worker import WorkerService


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_file_service(request: Request) -> FileService:
    return cast(FileService, request.app.state.file_service)


def get_worker_service(request: Request) -> WorkerService:
    return cast(WorkerService, request.app.state.worker_service)


SettingsDep = Annotated[Settings, Depends(get_settings)]
FileServiceDep = Annotated[FileService, Depends(get_file_service)]
WorkerServiceDep = Annotated[WorkerService, Depends(get_worker_service)]
