from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from app.config import RUNTIME_DIR, Settings, load_streams_config
from app.routes import router
from app.services.files import FileService
from app.services.worker import ChannelManager

logger = logging.getLogger("uvicorn.error")


def configure_logging(settings: Settings) -> None:
    level = logging.DEBUG if settings.debug else logging.INFO
    logging.getLogger().setLevel(level)
    logging.getLogger("uvicorn").setLevel(level)
    logging.getLogger("uvicorn.error").setLevel(level)
    logger.info(
        "application logging configured: debug=%s",
        settings.debug,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    configure_logging(settings)
    streams_config = load_streams_config(settings.streams_config)
    file_service = FileService(RUNTIME_DIR)
    await file_service.prepare_runtime_root()

    channel_manager = ChannelManager(
        settings=settings,
        file_service=file_service,
        channels=streams_config.channels,
    )
    app.state.settings = settings
    app.state.file_service = file_service
    app.state.channel_manager = channel_manager
    await channel_manager.start()

    try:
        yield
    finally:
        await channel_manager.stop()


app = FastAPI(title="restreamer", lifespan=lifespan)
app.include_router(router)
