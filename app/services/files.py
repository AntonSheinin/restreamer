import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import BinaryIO
from pathlib import Path


@dataclass(frozen=True)
class CachedFile:
    content: bytes
    size: int
    mtime_ns: int


class FileService:
    playlist_name = "index.m3u8"
    segment_template = "segment_%06d.ts"
    _asset_pattern = re.compile(r"^segment_\d{6}\.ts$")
    _default_chunk_size = 64 * 1024

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self._playlist_cache: dict[Path, CachedFile] = {}

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
        self._playlist_cache.pop(self.playlist_path(channel_name), None)
        for pattern in ("*.m3u8", "*.ts", "*.m4s", "*.mp4", "*.tmp"):
            for path in await asyncio.to_thread(lambda: list(channel_dir.glob(pattern))):
                await asyncio.to_thread(path.unlink, missing_ok=True)

    async def playlist_exists(self, channel_name: str) -> bool:
        return await self.file_exists(self.playlist_path(channel_name))

    async def file_exists(self, path: Path) -> bool:
        return await asyncio.to_thread(path.is_file)

    async def read_cached_playlist(self, path: Path) -> bytes:
        stat = await asyncio.to_thread(path.stat)
        cached = self._playlist_cache.get(path)
        if cached is not None and cached.size == stat.st_size and cached.mtime_ns == stat.st_mtime_ns:
            return cached.content

        content = await asyncio.to_thread(path.read_bytes)
        self._playlist_cache[path] = CachedFile(
            content=content,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        return content

    async def open_binary(self, path: Path) -> BinaryIO:
        return await asyncio.to_thread(path.open, "rb")

    async def iter_file(
        self,
        file: BinaryIO,
        chunk_size: int = _default_chunk_size,
    ) -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await asyncio.to_thread(file.read, chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(file.close)

    async def iter_byte_range(
        self,
        file: BinaryIO,
        start: int,
        end: int,
        chunk_size: int = _default_chunk_size,
    ) -> AsyncIterator[bytes]:
        try:
            await asyncio.to_thread(file.seek, start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = await asyncio.to_thread(file.read, min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(file.close)

    async def file_size(self, path: Path) -> int:
        return await asyncio.to_thread(lambda: path.stat().st_size)

    def resolve_hls_asset_path(self, channel_name: str, name: str) -> Path | None:
        if not self._asset_pattern.fullmatch(name):
            return None
        return self.channel_dir(channel_name) / name
