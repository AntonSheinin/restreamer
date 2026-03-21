from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import RUNTIME_DIR, Settings
from app.routes import router
from app.services.files import FileService
from app.services.worker import WorkerService


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    file_service = FileService(RUNTIME_DIR)
    await file_service.prepare_runtime_dir()

    worker_service = WorkerService(settings=settings, file_service=file_service)
    app.state.settings = settings
    app.state.file_service = file_service
    app.state.worker_service = worker_service
    await worker_service.start()

    try:
        yield
    finally:
        await worker_service.stop()


app = FastAPI(title="livex2hls-service", lifespan=lifespan)
app.include_router(router)
