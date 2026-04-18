import asyncio
import base64
import json
import logging
import re
import uuid
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.config import ChannelConfig, MakoKeshet12InputConfig

logger = logging.getLogger("uvicorn.error")

MAKO_PLAYLIST_KEY = "LTf7r/zM2VndHwP+4So6bw=="
MAKO_TOKEN_KEY = "YhnUaXMmltB6gd8p9SWleQ=="
MAKO_AES_IV = "theExact16Chars="
DEFAULT_HTTP_TIMEOUT_SECONDS = 20
STREAM_INF_PATTERN = re.compile(r"#EXT-X-STREAM-INF:(?P<attrs>.*)", re.IGNORECASE)
RESOLUTION_PATTERN = re.compile(r"RESOLUTION=(?P<width>\d+)x(?P<height>\d+)", re.IGNORECASE)
BANDWIDTH_PATTERN = re.compile(r"(?:^|,)BANDWIDTH=(?P<bandwidth>\d+)", re.IGNORECASE)


class SourceResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedSource:
    url: str
    video_map: str = "0:v:0"
    audio_map: str = "0:a:0?"


@dataclass(frozen=True)
class HlsVariant:
    index: int
    width: int | None
    height: int | None
    bandwidth: int | None


class SourceResolver:
    async def resolve(self) -> ResolvedSource:
        raise NotImplementedError


class StaticSourceResolver(SourceResolver):
    def __init__(self, channel: ChannelConfig) -> None:
        self._channel = channel

    async def resolve(self) -> ResolvedSource:
        if not self._channel.source_url:
            raise SourceResolutionError(f"channel '{self._channel.name}' missing source_url")
        return ResolvedSource(url=self._channel.source_url)


class MakoKeshet12Resolver(SourceResolver):
    def __init__(self, channel: ChannelConfig) -> None:
        if channel.input is None:
            raise SourceResolutionError(f"channel '{channel.name}' missing mako input settings")
        self._channel = channel
        self._input = channel.input
        self._device_id = self._input.device_id or str(uuid.uuid4())

    async def resolve(self) -> ResolvedSource:
        playlist_data = await self._load_playlist_data()
        source = self._select_source(playlist_data)
        ticketed_url = await self._ticketed_url(source, playlist_data)
        variant_index = await self._select_variant_index(ticketed_url)
        logger.info(
            "resolved mako keshet12 source for %s: stream=%s variant_index=%s",
            self._channel.name,
            self._input.stream,
            variant_index,
        )
        return ResolvedSource(
            url=ticketed_url,
            video_map=f"0:v:{variant_index}",
            audio_map=f"0:a:{variant_index}?",
        )

    async def _load_playlist_data(self) -> dict[str, object]:
        query = urlencode(
            {
                "vcmid": self._input.vcmid,
                "videoChannelId": self._input.channel_id,
                "galleryChannelId": self._input.gallery_channel_id,
                "consumer": self._input.consumer,
            }
        )
        separator = "&" if "?" in self._input.playlist_url else "?"
        playlist_url = f"{self._input.playlist_url}{separator}{query}"
        encrypted = await asyncio.to_thread(_fetch_text, playlist_url)
        decrypted = _aes_decrypt(encrypted, MAKO_PLAYLIST_KEY)
        try:
            data = json.loads(decrypted)
        except json.JSONDecodeError as exc:
            raise SourceResolutionError("failed to decode mako playlist JSON") from exc
        if not isinstance(data, dict):
            raise SourceResolutionError("mako playlist response is not an object")
        return data

    def _select_source(self, playlist_data: dict[str, object]) -> dict[str, object]:
        stream = self._input.stream
        if stream == "clean":
            return _first_media(playlist_data, "mediaClean", stream)
        if stream == "clean_port":
            return _first_media(playlist_data, "mediaCleanPort", stream)

        media = playlist_data.get("media")
        if not isinstance(media, list):
            raise SourceResolutionError("mako playlist missing media list")

        want_ssai = stream == "dvr"
        for item in media:
            if isinstance(item, dict) and bool(item.get("ssai")) is want_ssai:
                return item
        raise SourceResolutionError(f"mako playlist missing {stream} media source")

    async def _ticketed_url(
        self,
        source: dict[str, object],
        playlist_data: dict[str, object],
    ) -> str:
        source_url = source.get("url")
        cdn = source.get("cdn") or "AKAMAI"
        if not isinstance(source_url, str) or not source_url:
            raise SourceResolutionError("selected mako source is missing url")
        if not isinstance(cdn, str):
            cdn = "AKAMAI"

        source_url = _force_https(source_url)
        video_details = playlist_data.get("videoDetails")
        vid = self._input.vcmid
        if isinstance(video_details, dict) and isinstance(video_details.get("vid"), str):
            vid = video_details["vid"]

        parsed = urlparse(source_url)
        lp = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        payload = {
            "lp": lp,
            "rv": cdn,
            "du": self._device_id,
            "dv": vid,
            "na": self._input.app_version,
        }
        encrypted_payload = _aes_encrypt(json.dumps(payload, separators=(",", ":")), MAKO_TOKEN_KEY)
        encrypted_response = await asyncio.to_thread(
            _fetch_text,
            self._input.entitlement_url,
            "POST",
            encrypted_payload.encode("utf-8"),
            {"content-type": "text/plain;charset=UTF-8"},
        )
        ticket_response = _decode_ticket_response(encrypted_response)
        ticket = _first_ticket(ticket_response)
        return _append_query(source_url, ticket)

    async def _select_variant_index(self, master_url: str) -> int:
        if self._input.variant_index is not None:
            return self._input.variant_index

        playlist = await asyncio.to_thread(_fetch_text, master_url)
        variants = _parse_hls_variants(playlist)
        if not variants:
            return 0

        if self._input.variant == "first":
            return variants[0].index
        if self._input.variant == "720p":
            matching = [variant for variant in variants if variant.height == 720]
            if matching:
                return max(matching, key=_variant_quality_key).index

        return max(variants, key=_variant_quality_key).index


def build_source_resolver(channel: ChannelConfig) -> SourceResolver:
    if channel.source_type == "mako_keshet12":
        return MakoKeshet12Resolver(channel)
    return StaticSourceResolver(channel)


def _fetch_text(
    url: str,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    request_headers = {
        "user-agent": "Mozilla/5.0",
        "accept": "*/*",
        "referer": "https://www.mako.co.il/",
    }
    if headers:
        request_headers.update(headers)
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=DEFAULT_HTTP_TIMEOUT_SECONDS) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise SourceResolutionError(f"HTTP {exc.code} while fetching {url}: {detail}") from exc
    except URLError as exc:
        raise SourceResolutionError(f"failed to fetch {url}: {exc.reason}") from exc


def _aes_decrypt(ciphertext: str, key: str) -> str:
    cipher = Cipher(algorithms.AES(key.encode("utf-8")), modes.CBC(MAKO_AES_IV.encode("utf-8")))
    decryptor = cipher.decryptor()
    padded = decryptor.update(base64.b64decode(ciphertext.strip())) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext.decode("utf-8")


def _aes_encrypt(plaintext: str, key: str) -> str:
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(key.encode("utf-8")), modes.CBC(MAKO_AES_IV.encode("utf-8")))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")


def _decode_ticket_response(encrypted_response: str) -> dict[str, object]:
    decrypted = _aes_decrypt(encrypted_response, MAKO_TOKEN_KEY)
    try:
        ticket_response = json.loads(decrypted)
    except json.JSONDecodeError as exc:
        raise SourceResolutionError("failed to decode mako ticket response JSON") from exc
    if not isinstance(ticket_response, dict):
        raise SourceResolutionError("mako ticket response is not an object")
    return ticket_response


def _first_ticket(ticket_response: dict[str, object]) -> str:
    tickets = ticket_response.get("tickets")
    if not isinstance(tickets, list) or not tickets:
        raise SourceResolutionError(f"mako ticket response has no tickets: {ticket_response!r}")
    first = tickets[0]
    if not isinstance(first, dict) or not isinstance(first.get("ticket"), str):
        raise SourceResolutionError("mako ticket response has invalid ticket")
    return first["ticket"]


def _first_media(playlist_data: dict[str, object], key: str, stream: str) -> dict[str, object]:
    media = playlist_data.get(key)
    if not isinstance(media, list) or not media:
        raise SourceResolutionError(f"mako playlist missing {stream} media source")
    first = media[0]
    if not isinstance(first, dict):
        raise SourceResolutionError(f"mako playlist has invalid {stream} media source")
    return first


def _force_https(url: str) -> str:
    if url.lower().startswith("http:"):
        return "https:" + url[5:]
    return url


def _append_query(url: str, query: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{query}"


def _parse_hls_variants(playlist: str) -> list[HlsVariant]:
    variants: list[HlsVariant] = []
    for line in playlist.splitlines():
        match = STREAM_INF_PATTERN.match(line.strip())
        if match is None:
            continue
        attrs = match.group("attrs")
        resolution = RESOLUTION_PATTERN.search(attrs)
        bandwidth = BANDWIDTH_PATTERN.search(attrs)
        variants.append(
            HlsVariant(
                index=len(variants),
                width=int(resolution.group("width")) if resolution else None,
                height=int(resolution.group("height")) if resolution else None,
                bandwidth=int(bandwidth.group("bandwidth")) if bandwidth else None,
            )
        )
    return variants


def _variant_quality_key(variant: HlsVariant) -> tuple[int, int]:
    pixels = (variant.width or 0) * (variant.height or 0)
    return pixels, variant.bandwidth or 0
