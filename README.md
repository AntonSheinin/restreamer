# livex2hls-service

Minimal FastAPI service that pulls a DASH `.livx` stream with `ffmpeg` and exposes it as HLS for Flussonic ingest.

## Requirements

- Docker for containerized runtime

## Docker

1. Copy `.env.example` to `.env`.
2. Set `SOURCE_URL` to your KAN `.livx` URL.
3. Build and run with Docker Compose:

```bash
docker compose up --build -d
```

The container:

- installs `ffmpeg`
- installs Python dependencies with `uv`
- exposes the service on port `8092`
- writes HLS output to `./runtime` on the host

The service starts `ffmpeg` automatically and exposes HLS at:

```text
http://<host>:8092/hls/index.m3u8
```

## Environment Variables

- `SOURCE_URL`: required DASH `.livx` URL
- `HLS_SEGMENT_TIME`: HLS segment target duration, default `2`
- `HLS_LIST_SIZE`: number of segments kept in the playlist, default `12`

The container runtime is fixed:

- `ffmpeg` path is `/usr/bin/ffmpeg`
- HLS output directory is `/app/runtime` inside the container and `./runtime` on the host via Docker Compose
- host and port are controlled by the container command and compose port mapping

## Endpoints

- `GET /health`
- `GET /status`
- `GET /hls/index.m3u8`
- `GET /hls/{segment_name}`

## Troubleshooting

- `503` from `/hls/index.m3u8` means the worker has not produced the first playlist yet.
- If startup fails immediately, verify that the container image built successfully and includes `ffmpeg`.
- If the worker keeps restarting, verify that `SOURCE_URL` points to a reachable DASH MPD that `ffmpeg` can read.
