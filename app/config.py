from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

FFMPEG_PATH = Path("/usr/bin/ffmpeg")
RUNTIME_DIR = Path("/app/runtime")


class Settings(BaseSettings):
    source_url: str = Field(..., alias="SOURCE_URL")
    hls_segment_time: int = Field(2, alias="HLS_SEGMENT_TIME", ge=1)
    hls_list_size: int = Field(12, alias="HLS_LIST_SIZE", ge=2)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )
