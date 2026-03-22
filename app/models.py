from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ChannelStatus(BaseModel):
    channel: str
    output_format: Literal["hls", "tshttp"]
    state: Literal["starting", "running", "restarting", "error"] = "starting"
    restart_count: int = 0
    last_error: str | None = None
    pid: int | None = None
