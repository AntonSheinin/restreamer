from pathlib import Path
import re
import tomllib
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

FFMPEG_PATH = Path("/usr/bin/ffmpeg")
RUNTIME_DIR = Path("/app/runtime")
CHANNEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class HlsChannelConfig(BaseModel):
    segment_time: int = Field(4, ge=1)
    list_size: int = Field(6, ge=2)


class TranscodingConfig(BaseModel):
    video: Literal["copy", "transcode"] = "copy"
    audio: Literal["copy", "transcode"] = "transcode"


class TshttpChannelConfig(BaseModel):
    stale_output_seconds: int = Field(15, ge=1)
    input_fflags: str | None = None
    copytb: Literal[0, 1] = 1
    mpegts_copyts: bool = True


class ChannelConfig(BaseModel):
    name: str
    source_url: str
    output_format: Literal["hls", "tshttp"]
    transcoding: TranscodingConfig = Field(default_factory=TranscodingConfig)
    hls: HlsChannelConfig | None = None
    tshttp: TshttpChannelConfig | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not CHANNEL_NAME_PATTERN.fullmatch(value):
            raise ValueError("channel name must match [A-Za-z0-9_-]+")
        return value

    @model_validator(mode="after")
    def validate_output_settings(self) -> "ChannelConfig":
        if self.output_format == "hls":
            if self.tshttp is not None:
                raise ValueError("hls channels cannot define tshttp settings")
            self.hls = self.hls or HlsChannelConfig()
        else:
            if self.hls is not None:
                raise ValueError("tshttp channels cannot define hls settings")
            self.tshttp = self.tshttp or TshttpChannelConfig()
        return self


class StreamsConfig(BaseModel):
    channels: list[ChannelConfig]


class Settings(BaseSettings):
    ffmpeg_loglevel: str = Field("info", alias="FFMPEG_LOGLEVEL")
    streams_config: Path = Field(Path("streams.toml"), alias="STREAMS_CONFIG")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )


def load_streams_config(path: Path) -> StreamsConfig:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_channels = raw.get("channels")
    if not isinstance(raw_channels, dict) or not raw_channels:
        raise ValueError("streams.toml must define at least one channel under [channels.<name>]")

    channels: list[ChannelConfig] = []
    for name, payload in raw_channels.items():
        if not isinstance(payload, dict):
            raise ValueError(f"channel '{name}' must be a table")
        channels.append(ChannelConfig(name=name, **payload))

    channels.sort(key=lambda channel: channel.name)
    return StreamsConfig(channels=channels)
