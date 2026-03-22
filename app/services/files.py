import asyncio
import re
from pathlib import Path


class FileService:
    playlist_name = "index.m3u8"
    segment_template = "segment_%06d.ts"
    _asset_pattern = re.compile(r"^segment_\d{6}\.ts$")

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir

    async def prepare_runtime_root(self) -> None:
        await asyncio.to_thread(self.runtime_dir.mkdir, parents=True, exist_ok=True)

    def channel_dir(self, channel_name: str) -> Path:
        return self.runtime_dir / channel_name

    def playlist_path(self, channel_name: str) -> Path:
        return self.channel_dir(channel_name) / self.playlist_name

    def segment_path_pattern(self, channel_name: str) -> str:
        return str(self.channel_dir(channel_name) / self.segment_template)

    async def prepare_channel_dir(self, channel_name: str) -> None:
        await asyncio.to_thread(self.channel_dir(channel_name).mkdir, parents=True, exist_ok=True)

    async def cleanup_hls_outputs(self, channel_name: str) -> None:
        await self.prepare_channel_dir(channel_name)
        channel_dir = self.channel_dir(channel_name)
        for pattern in ("*.m3u8", "*.ts", "*.m4s", "*.mp4", "*.tmp"):
            for path in await asyncio.to_thread(lambda: list(channel_dir.glob(pattern))):
                await asyncio.to_thread(path.unlink, missing_ok=True)

    async def playlist_exists(self, channel_name: str) -> bool:
        return await self.file_exists(self.playlist_path(channel_name))

    async def file_exists(self, path: Path) -> bool:
        return await asyncio.to_thread(path.is_file)

    async def read_bytes(self, path: Path) -> bytes:
        return await asyncio.to_thread(path.read_bytes)

    def resolve_hls_asset_path(self, channel_name: str, name: str) -> Path | None:
        if not self._asset_pattern.fullmatch(name):
            return None
        return self.channel_dir(channel_name) / name
