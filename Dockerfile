# syntax=docker/dockerfile:1
#
# The product image, and the integration harness that proves it, from ONE file.
#
# Docker is supported, not recommended: the native install is ten minutes and
# needs no Docker (Project_state/design/06-deployment.md). What makes it the
# alternative rather than the default is host-specific audio passthrough, the
# UID mapping of the /data bind mount, and the kiosk browser, which cannot live
# in a container. docs/installing.md §11 spells all three out.
#
# One file with targets instead of a shipped Dockerfile plus a test one: the
# harness ticket (HSD-8gft) requires the test container and the runtime image to
# share the same base and the same libmpv2/ffmpeg/espeak-ng versions, or a
# passing integration run stops predicting what users get. Two files drift
# silently; the `system` stage below is the only place those three packages are
# named, and every target is built from it.
#
# Targets:
#   runtime      (default) the shipped image: non-root, /data, UI baked in
#   piper        runtime + the piper extra. piper-tts is GPL-3.0-or-later, so it
#                ships as a separate tag whose labels carry the notice; no voice
#                model is bundled (voices are downloaded at runtime)
#   integration  the design 07 §2 harness: real binaries, root, --ao=null
#
# Both shipped targets carry OCI labels (org.opencontainers.image.*), because the
# licence notice has to travel with the image: a registry shows .description and
# .licenses, and `docker image inspect` shows them on a laptop with no docs
# checkout. .licenses is the AGGREGATE SPDX expression for the image contents —
# the app's own MIT licence AND the GPL/LGPL Debian packages the apt layer pulls
# in (espeak-ng, libmpv2, ffmpeg, glibc). It is not a claim that the image is
# MIT-licensed as a whole, and the default target is not "MIT-only".
# .version is a build arg: a local build says `dev` because nothing is released
# yet, and the release pipeline (HSD-in8q) passes the release tag. .source, .url
# and .documentation are deliberately absent — the project declares no repository
# URL or registry org, so any value here would be invented.
#
# The UI is BUILT here rather than copied from the committed web/dist, so the
# image never depends on the checkout's Node version and the frontend build is
# exercised on every image build.

# The uv pin must match the uv that wrote uv.lock (0.13.5), or `uv sync --frozen`
# refuses the lock outright.
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

# ---------------------------------------------------------------------------
# system — bookworm plus the audio/TTS runtime packages, stated once.
#
# python:3.11-slim is the supported Python floor (pyproject requires-python), so
# every target is also a free check that no 3.12+ syntax got in. bookworm is the
# Debian the supported targets (Debian 12, Raspberry Pi OS) ship, so this is the
# libmpv2 that a native install gets too.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS system

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libmpv2 \
        ffmpeg \
        espeak-ng \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# web — the frontend build.
#
# web/dist is architecture-independent, so it is built once on the build
# platform: an arm64 image does not re-run node under QEMU, and the amd64 and
# arm64 images carry identical frontend bytes.
#
# `npm ci`, not `--omit=dev`: vite is a devDependency, so the production bundle
# needs the full tree. node:22-slim satisfies web/package.json engines (>=22.12).
# ---------------------------------------------------------------------------
FROM --platform=$BUILDPLATFORM node:22-slim AS web

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# venv — the dependency layer, shared by runtime and piper, so that editing a
# source file does not refetch the world.
# ---------------------------------------------------------------------------
FROM system AS venv

COPY --from=uv /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
# runtime — what users run.
#
# The UID/GID are build args because /data is a bind mount from the host and the
# container user has to own it; the host UID is host-specific (design 06 §6),
# and 1000 is the first regular user on Debian, Raspberry Pi OS and Ubuntu.
#
# --no-editable installs a real wheel, so hatch's force-include puts the built UI
# at chime_deck/static inside site-packages. That is the copy
# config.default_static_dir() prefers, and it is the same layout a native
# install has, so the image cannot serve the placeholder page by accident.
# ---------------------------------------------------------------------------
FROM venv AS runtime

ARG UID=1000
ARG GID=1000

RUN groupadd --gid "$GID" doorbell \
    && useradd --uid "$UID" --gid "$GID" --create-home \
        --shell /usr/sbin/nologin doorbell

COPY src ./src
COPY --from=web /web/dist ./web/dist
RUN uv sync --frozen --no-dev --no-editable

# Created here, owned by the runtime user, so a bare `docker run` with no bind
# mount still starts. A bind-mounted ./data must be created by the host user
# first (deploy/docker-compose.yml says so).
RUN install -d -o doorbell -g doorbell /data

ENV VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    CHIMEDECK_DATA_DIR=/data \
    CHIMEDECK_HOST=0.0.0.0 \
    CHIMEDECK_PORT=8080

# Honest metadata for the apt layer as well as for us: this image aggregates
# GPL/LGPL third-party binaries, so labelling it plain "MIT" would be false.
# The D14 boundary is that Piper is absent — which is what .description says.
ARG CHIMEDECK_VERSION=dev
LABEL org.opencontainers.image.title="ChimeDeck"
LABEL org.opencontainers.image.description="A chime and status panel for Home Assistant. Contains only the MIT-licensed app and audio-processing binaries; no local TTS engine."
LABEL org.opencontainers.image.licenses="MIT AND LGPL-2.1-or-later AND GPL-2.0-or-later AND GPL-3.0-or-later"
LABEL org.opencontainers.image.version=$CHIMEDECK_VERSION

USER doorbell
EXPOSE 8080
CMD ["chime-deck"]

# ---------------------------------------------------------------------------
# piper — the optional local speech engine, as its own tag.
#
# piper-tts is GPL-3.0-or-later, so D14 keeps it out of the default install and
# out of the default tag: this tag exists so that the copyleft engine is a
# deliberate choice, and the labels below are the notice that travels with it.
#
# This stage deliberately ships NO voice model. A model's licence is separate
# from Piper's and varies per voice (tests/golden/piper_voices_v1_0_0.json
# records all thirteen, from CC0 to CC-BY-SA to "See model card"), so baking one
# in would redistribute an engine we can describe on a licence we cannot. The
# weight is the lesser reason. The app downloads the voice the user picks into
# <data-dir>/voices at runtime — the Speech panel, or POST
# /api/voices/<id>/download — and shows that voice's licence there.
#
# .licenses is inherited unchanged from `runtime`: this tag adds Piper, not a new
# licence family, and the default image already carries espeak-ng (GPL-3.0-or-
# later) and libmpv2. Only the description names the engine, because only this
# tag has it. `.version` is inherited from `runtime` too.
# ---------------------------------------------------------------------------
FROM runtime AS piper

LABEL org.opencontainers.image.title="ChimeDeck (with local Piper)"
LABEL org.opencontainers.image.description="ChimeDeck with the Piper TTS engine (GPL-3.0-or-later) added as a separate tag. No voice model is bundled; voices are downloaded at runtime and each carries its own licence."

USER root
RUN uv sync --frozen --no-dev --no-editable --extra piper
USER doorbell

# ---------------------------------------------------------------------------
# integration — the design 07 §2 harness, unchanged in behaviour.
#
# Real espeak-ng, real libmpv, real ffmpeg and real local Piper on the supported
# Python floor, with playback to --ao=null. It runs as root on purpose (see the
# note at the end of this stage) and its piper voice is a test fixture, not a
# shipped model.
#
# Exactly TWO build steps need the network: the apt + uv layers, and the single
# voice-model bake below. The *run* is offline: docker/compose.test.yml is
# network_mode: none.
#
# Build and run it through scripts/test-integration.sh, never by hand: the script
# supplies the timeouts and the offline runtime flags.
# ---------------------------------------------------------------------------
FROM system AS integration

ENV MPV_AO=null \
    CHIMEDECK_TEST_REAL_MPV=1

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./

# Dependencies get their own layer so that editing a test does not refetch them.
# The piper extra is installed here too, so a real synthesis can be measured.
RUN uv sync --frozen --no-install-project --extra piper

COPY src ./src
COPY tests ./tests
# The shipped systemd user unit is parsed by tests/test_stop_path.py, so the
# image must carry it. It is data for a test here, not a deployed unit: the
# container has no systemd session.
COPY deploy ./deploy
# tests/test_api_icons.py cross-checks the shipped icon manifest against the
# @mdi/svg version pinned in web/package.json, so that one frontend file is test
# data here. It is the only web/ file the suite reads from the tree.
COPY web/package.json ./web/package.json
# Load-bearing, and it is the same copy the runtime stage makes: pyproject's
# hatch force-include maps web/dist into the wheel, so `uv sync` cannot build the
# project without it (`FileNotFoundError: Forced include not found: /app/web/
# dist`, which is how this harness stopped rebuilding after HSD-sgkr added the
# force-include while the old image stayed cached). Sharing the `web` stage also
# means the harness serves the same UI bytes the shipped image does.
COPY --from=web /web/dist ./web/dist

# The dev group is included here, so pytest and pytest-asyncio exist. The sync
# follows the source copies so the extra lands in the same layer chain the run
# uses.
RUN uv sync --frozen --extra piper

# The image must carry a real voice model and the runtime cannot fetch one
# (compose.test.yml is network_mode: none). `timeout 300` because piper's
# download_voices has no timeout of its own; `--force-redownload` + `md5sum -c -`
# because piper verifies nothing, so a truncated fetch would otherwise be cached
# forever. The digests are the published v1.0.0 values from
# tests/golden/piper_voices_v1_0_0.json.
RUN timeout 300 uv run --no-sync python -m piper.download_voices nl_BE-nathalie-x_low \
        --force-redownload --data-dir /opt/piper-voices \
 && printf '%s  %s\n' 4a00803b60caecad30ea612bcd9f9344 /opt/piper-voices/nl_BE-nathalie-x_low.onnx | md5sum -c - \
 && printf '%s  %s\n' 5307e9c8398ab02060611bb9c2e0f137 /opt/piper-voices/nl_BE-nathalie-x_low.onnx.json | md5sum -c -

# Local copy, not a second download, so editing a test does not refetch ~20 MB.
# The model is stored twice on purpose: /opt is what the checksums verified.
RUN mkdir -p /app/tests/fixtures/piper-voices && cp -a /opt/piper-voices/. /app/tests/fixtures/piper-voices/

ENV CHIMEDECK_TEST_REAL_PIPER=1 \
    CHIMEDECK_TEST_PIPER_VOICES=/app/tests/fixtures/piper-voices

# `--no-sync` is load-bearing. The venv and the baked uv.lock come from the same
# `uv sync --frozen` layer chain above, so the run needs no network and cannot be
# stale. Do not "improve" this into a sync: the container runs with
# network_mode: none.
CMD ["uv", "run", "--no-sync", "pytest", "-q"]

# The container runs as root, so a future test that asserted "an unwritable path
# is rejected" would silently pass here. No such test exists today
# (`grep -rn 'chmod\|os.access' tests/` finds nothing). Do not add a USER to make
# that hypothetical test honest; write the test where it can fail instead.
