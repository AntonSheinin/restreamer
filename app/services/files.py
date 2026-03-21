import asyncio
import re
from pathlib import Path


class FileService:
    playlist_name = "index.m3u8"
    segment_template = "segment_%06d.ts"
    _asset_pattern = re.compile(r"^segment_\d{6}\.ts$")

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir

    @property
    def playlist_path(self) -> Path:
        return self.runtime_dir / self.playlist_name

    @property
    def segment_path_pattern(self) -> str:
        return str(self.runtime_dir / self.segment_template)

    async def prepare_runtime_dir(self) -> None:
        await asyncio.to_thread(self.runtime_dir.mkdir, parents=True, exist_ok=True)

    async def cleanup_outputs(self) -> None:
        await self.prepare_runtime_dir()
        for pattern in ("*.m3u8", "*.ts", "*.m4s", "*.mp4", "*.tmp"):
            for path in await asyncio.to_thread(lambda: list(self.runtime_dir.glob(pattern))):
                await asyncio.to_thread(path.unlink, missing_ok=True)

    async def playlist_exists(self) -> bool:
        return await self.file_exists(self.playlist_path)

    async def file_exists(self, path: Path) -> bool:
        return await asyncio.to_thread(path.is_file)

    def resolve_asset_path(self, name: str) -> Path | None:
        if not self._asset_pattern.fullmatch(name):
            return None
        return self.runtime_dir / name
