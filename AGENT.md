# AGENT.md

## Purpose

This file defines how AI agents should understand, navigate, and modify this
repository. It is the primary instruction source for automated code agents
working on this project.

Agents should behave as careful maintainers: make the smallest useful change,
preserve the streaming behavior, and validate the result with the commands this
repository actually supports.

## Repository Overview

`restreamer` is a minimal Python 3.12 FastAPI service that starts one `ffmpeg`
worker per configured channel and exposes each channel as HLS or MPEG-TS over
HTTP.

Important entry points:

- `app/app.py`: FastAPI app, lifespan setup, logging, settings, file service,
  and channel manager startup/shutdown.
- `app/routes.py`: HTTP API, token-protected endpoints, HLS playlist and segment
  serving, range requests, TSHTTP streaming.
- `app/config.py`: Pydantic settings, `.env` loading, `streams.toml` parsing,
  channel validation, defaults.
- `app/services/worker.py`: `ffmpeg` process supervision, restart backoff, HLS
  health checks, TSHTTP consumer handling.
- `app/services/source_resolver.py`: source resolution, including the
  `mako_keshet12` resolver.
- `app/services/files.py`: runtime output paths and HLS asset path validation.
- `streams.toml`: channel definitions.
- `.env.example`: documented environment variables; never place real secrets in
  this file.
- `Dockerfile` and `docker-compose.yml`: production runtime and resource limits.
- `.github/workflows/deploy.yml`: deployment workflow for pushes to `master`.

Generated/runtime data belongs under `runtime/` or the Docker volume mounted at
`/app/runtime`; do not commit generated playlists or media segments.

## Instruction Precedence

Follow the nearest, most specific agent instruction file when more than one is
present. If future directory-specific instructions are added, they override this
root file only for their scope. Direct user instructions override this file when
they are explicit and safe to follow.

## General Principles

- Make minimal, safe, and reversible changes.
- Prefer clarity over cleverness.
- Preserve existing architecture, naming, and module boundaries.
- Do not modify unrelated code, deployment files, or channel configuration.
- Inspect existing behavior before changing it.
- Search for an existing helper or pattern before adding a new one.
- Keep public HTTP behavior backward compatible unless the task explicitly
  requires a breaking change.
- Do not introduce large refactors while fixing narrow bugs.

## Project-Specific Boundaries

- Keep FastAPI route concerns in `app/routes.py`.
- Keep request dependencies and authentication helpers in `app/dependencies.py`.
- Keep settings and TOML validation in `app/config.py`.
- Keep process supervision, `ffmpeg` command construction, and worker state in
  `app/services/worker.py`.
- Keep filesystem path construction and path validation in
  `app/services/files.py`.
- Keep external source lookup and ticket/playlist resolution in
  `app/services/source_resolver.py`.
- Keep response schemas in `app/models.py`.

Avoid creating parallel implementations for auth, channel loading, path
resolution, worker state, or `ffmpeg` argument generation.

## Configuration And Runtime

- `.env` is local and ignored by git. Do not read, print, commit, or hardcode
  real values from it.
- `.env.example` should contain placeholders and safe defaults only.
- `ACCESS_TOKEN` protects operational and playback endpoints. Preserve
  constant-time comparisons and avoid logging tokens.
- `STREAMS_CONFIG` defaults to `streams.toml`.
- `RUNTIME_DIR` is `/app/runtime` in the container. Be careful when changing
  runtime paths because Docker Compose mounts a named volume there.
- `FFMPEG_THREADS` is a global resource-control setting. Keep changes compatible
  with shared-host deployments.
- Channel names must continue to match `[A-Za-z0-9_-]+`.

When changing `streams.toml` behavior, update `README.md` and `.env.example` if
operators need to know about new settings.

## Deployment Constraints

- Do not touch the production server directly.
- Do not SSH into the production server.
- Do not run remote Docker, Docker Compose, git, shell, log, or inspection
  commands on the production server.
- Deployments must happen only through the existing GitHub Actions deployment
  workflow.
- Do not manually deploy, restart, rebuild, stop, remove, or recreate production
  containers outside GitHub Actions.
- Do not modify `.github/workflows/deploy.yml` or any GitHub Actions deployment
  workflow unless the user explicitly asks for workflow changes.
- If deployment or remote verification is needed, ask the user to run or rerun
  the defined GitHub Actions workflow, or use GitHub Actions status/logs when
  available. Do not bypass the workflow.

## Security Rules

- Never expose secrets, bearer tokens, SSH keys, Akamai tickets, or real private
  configuration in code, docs, logs, test fixtures, or examples.
- Do not weaken authentication on protected endpoints.
- Preserve the existing support for `Authorization: Bearer <token>` and
  `?access_token=<token>` unless explicitly told otherwise.
- Do not log full upstream URLs if they may include temporary tickets or tokens.
- Validate external input at boundaries. This is especially important for
  channel names, HLS asset names, byte ranges, and URLs.
- Do not widen filesystem access. HLS asset serving must remain constrained to
  validated segment names under the configured channel directory.
- Do not suppress security-relevant errors silently.

## Streaming And ffmpeg Rules

- Treat `app/services/worker.py` as high-risk code. Small changes can affect
  live streaming stability.
- Preserve graceful process termination before killing `ffmpeg`.
- Preserve restart backoff behavior unless the task is specifically about
  restart policy.
- Do not remove HLS playlist advancement checks, segment probes, TSHTTP
  staleness checks, or active-consumer protection without a clear replacement.
- Keep blocking file and network operations out of the event loop. Use existing
  `asyncio.to_thread` patterns where appropriate.
- Preserve HLS byte-range behavior and headers when touching segment serving.
- Preserve playlist token rewriting for HLS playback clients.
- For source resolvers, use explicit timeouts and meaningful
  `SourceResolutionError` messages.

## Code Style

- Use Python 3.12 syntax and type hints consistent with the existing code.
- Prefer Pydantic models and validators for structured configuration.
- Prefer standard library functionality unless a dependency is already present
  or clearly necessary.
- Keep functions small enough to review, but do not split code into abstractions
  that are only used once without a clear benefit.
- Use clear exception types and messages.
- Comments should explain why, not restate what the code does.
- Keep files ASCII unless the edited file already requires non-ASCII text.

## Dependencies

Current runtime dependencies are declared in `pyproject.toml`:

- `fastapi[standard]`
- `pydantic-settings`
- `cryptography`

Do not add dependencies unless necessary. If a dependency is added:

- Choose a stable, widely adopted library.
- Keep the version constraint compatible with the current style.
- Explain the operational reason in the change summary or documentation.
- Update Docker/build expectations if needed.

## File Operations

- Prefer editing existing files over creating new ones.
- Create new files only when the project structure clearly needs them.
- Do not delete files unless you have verified they are unused.
- Do not commit generated files from `runtime/`, `__pycache__/`, `.venv/`, media
  segments, local `.env`, or Docker volume contents.
- Do not rename or move files unless necessary for the requested change.

## Testing And Validation

This repository currently has no committed test suite. When changing code, use
the strongest available validation for the affected area.

Recommended local checks:

```bash
python -m compileall app
```

```bash
docker compose config
```

For Docker/runtime changes:

```bash
docker compose build
```

```bash
docker compose up -d
```

Manual smoke checks after the service starts:

```bash
curl -H "Authorization: Bearer <ACCESS_TOKEN>" http://127.0.0.1:8092/health
```

```bash
curl -H "Authorization: Bearer <ACCESS_TOKEN>" http://127.0.0.1:8092/channels
```

Use the configured host and port from `.env` when they differ from defaults.
Avoid running long-lived streaming checks unless the task requires them.

If adding tests in the future:

- Use the existing test framework if one has been introduced.
- Add focused tests for new behavior or regressions.
- Prefer unit tests for config parsing, path validation, auth behavior, byte
  ranges, and command construction.
- Avoid tests that require real upstream live streams unless they are explicitly
  marked as integration/manual.

## Documentation

- Update `README.md` when endpoints, configuration, runtime behavior, Docker
  limits, or operator workflows change.
- Update `.env.example` when adding or changing environment variables.
- Keep docs concise and accurate.
- Use placeholder tokens and example URLs where possible.
- Do not document secrets, private server names, or temporary ticketed URLs.

## Error Handling

- Do not suppress errors silently.
- Preserve meaningful HTTP status codes and response details.
- Keep `ValidationError` and `ValueError` handling user-facing where operators
  need actionable feedback.
- Avoid broad `except Exception` blocks unless they already match a supervisor
  boundary and include logging or state updates.
- Maintain useful `last_error` values for channel status.

## Performance And Reliability

- Avoid premature optimization, but be careful with live-streaming hot paths.
- Do not introduce unnecessary disk I/O in request handlers.
- Avoid blocking the event loop with file, process, crypto, or network work.
- Be mindful of memory growth in streaming paths and queues.
- Preserve bounded TSHTTP queue behavior.
- Keep resource limits in Docker Compose conservative for shared hosts.

## Git And Version Control

- Do not rewrite history unless explicitly requested.
- Keep changes atomic and logically grouped.
- Do not revert or overwrite unrelated user changes.
- Inspect the worktree before editing when the task may touch files that are
  already modified.
- Deployment runs on pushes to `master`; treat changes to `.github/workflows`,
  `Dockerfile`, and `docker-compose.yml` as production-affecting.

## When Requirements Are Unclear

- Infer intent from the code, README, and existing configuration.
- Choose the safest, most conservative approach.
- Ask a concise question only when a reasonable assumption would be risky.
- Do not invent features beyond the requested behavior.

## Forbidden Actions

Agents must not:

- Introduce breaking API or configuration changes without explicit reason.
- Modify deployment, Docker, or resource limits blindly.
- Remove authentication or weaken token handling.
- Log secrets, access tokens, or ticketed upstream URLs.
- Add large refactors unrelated to the request.
- Remove health checks, restart behavior, or path validation without an
  equivalent replacement.
- Commit generated runtime media files.
- Ignore existing conventions and module boundaries.

## Recommended Workflow

1. Inspect `git status --short`.
2. Locate the relevant entry point and module boundary.
3. Search for existing behavior before adding code.
4. Read the affected code and nearby callers.
5. Apply the minimal change.
6. Run available validation, at least `python -m compileall app` for Python
   changes.
7. Run Docker validation for Docker or Compose changes.
8. Update `README.md` or `.env.example` when operator-facing behavior changes.
9. Summarize the change, validation performed, and any residual risk.

## Summary

This file enforces safety, consistency, minimalism, and respect for existing
streaming behavior. Agents should preserve the service's operational reliability
while making focused, well-validated changes.
