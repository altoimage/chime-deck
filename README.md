# ChimeDeck

A small, self-hosted chime-and-status panel for [Home Assistant](https://www.home-assistant.io/).

It runs on the cheap Linux box you already want by the door — a Raspberry Pi, an old
NUC, a thin client — and does two simple jobs:

1. **Shows the state of things you care about.** A few large, colourful tiles: is the
   front door locked, is the garage open, what is the temperature in the kitchen.
2. **Makes a noise when Home Assistant tells it to.** Home Assistant gets a real
   `button` entity for each chime you configure. Press the button (from an automation,
   a dashboard, a physical button, anything) and this device plays the sound — or
   speaks the sentence — that you chose.

Think "video doorbell chime, but for every event in my house".

```
                    ┌───────────────────────────────┐
   Home Assistant   │           ChimeDeck           │
   ┌───────────┐    │  ┌─────────────────────────┐  │
   │ automation│───▶│  │ MQTT bridge             │  │
   │  press    │    │  └───────────┬─────────────┘  │
   └───────────┘    │              ▼                │
   ┌───────────┐    │  ┌─────────────────────────┐  │
   │  states   │◀──▶│  │ audio engine → 🔊       │  │
   └───────────┘    │  └─────────────────────────┘  │
                    │  ┌─────────────────────────┐  │
                    │  │ web UI (kiosk browser)  │  │
                    │  └─────────────────────────┘  │
                    └───────────────────────────────┘
```

## What it does

- **Status tiles** — pick any Home Assistant entity; the tile shows its icon, name and
  current state, updating live over the Home Assistant websocket.
- **Chime buttons** — each one appears in Home Assistant as a native device button.
  Configure what it plays: an audio file, a downloaded sound URL, or a spoken phrase.
- **Speech you can template** — the spoken phrase can include live Home Assistant values,
  e.g. *"The garage door is open"*.
- **Or no speech at all** — sound files only is a completely supported setup, with no
  synthesiser installed and no nagging.
- **Speech from wherever suits you** — the device can use Home Assistant's voices, a local
  neural engine, a renderer on a bigger machine, or a cloud provider. A cheap 1 GB panel is
  still a capable doorbell because it only has to *play* the audio, never generate it.
- **Speech that costs nothing to repeat** — a fixed announcement is rendered to audio **once**
  and reused forever. Pressing the button a thousand times makes one TTS request, not a
  thousand, so cloud voice quotas last and presses are as fast as playing a local file.
  Only phrases containing live values are re-rendered per press.
- **Quiet hours** — stop the 3am chimes, with per-button exceptions for real alarms.
- **Testable and observable** — every button has a test action, and a press history log
  tells you what played and when.
- **No cloud, no account** — it talks to your Home Assistant and your MQTT broker, and
  to nothing else.

## Requirements

- A 64-bit Linux machine (x86_64 or aarch64 / Raspberry Pi OS 64-bit). 32-bit ARM is
  not supported.
- Python 3.11 or newer.
- An MQTT broker that Home Assistant is already connected to (the Mosquitto add-on is
  the usual choice).
- A Home Assistant long-lived access token.
- Audio hardware on the device itself — the sound plays locally, not in the browser.

### Where does it run?

**On the panel.** One small device with a touchscreen and a speaker runs the whole app —
UI, MQTT, Home Assistant connection and audio playback. That is the product.

What you can move off the panel is *speech generation*, if the board is small:

| Engine | Runs where | Needs |
| --- | --- | --- |
| `espeak_ng` (apt package `espeak-ng`) | the panel | ~2 MB, robotic but instant |
| Home Assistant TTS (`ha_tts`) | your Home Assistant | nothing on the panel |
| Remote renderer (`remote_tts`) | another machine | a token |
| External provider (`http_tts`) | the vendor's cloud | ships: pick `http_tts` and a provider in Settings → Speech; a paid provider bills per character |
| Local Piper | the panel | **~2 GB RAM** |

So a 1 GB panel is a perfectly good doorbell: it just gets its voice from somewhere else.
The sound always comes out of the panel — that is the one thing that never moves.

## Quick start

```bash
git clone <this repo>
cd chime_deck

# system packages (Debian / Raspberry Pi OS)
sudo apt install libmpv2 libportaudio2 ffmpeg espeak-ng

uv sync
uv run chime-deck          # serves http://127.0.0.1:8080
```

### Rendering speech on another machine

A small panel can borrow the voice of a bigger box: run `chime-deck --role renderer`
there, and it binds to localhost unless `--host` says otherwise. The renderer refuses to
start until `tts.renderer.token` is set in that data directory's settings, so set it once by
running the server role against the same `--data-dir`, then put the same value in
`tts.remote_tts.token` on the panel along with the renderer's address in
`tts.remote_tts.url`, and choose `remote_tts` as the speech engine. That token is its only
authentication, so never port-forward it.

The frontend ships in two stages. The **design tokens and component kit have landed** (the
built bundle is committed in `web/dist/`, so a fresh clone needs no Node.js), but the
dashboard and settings screens do not exist yet: `/` currently serves a calm "under
construction" page built from the kit. The screens arrive in later frontend tickets. Until
then, start the server and check `curl -fsS http://127.0.0.1:8080/api/health` (see
docs/installing.md §5).

Working on the UI: `cd web && npm ci && npm run dev`, and open `/gallery` for the component
gallery (dev server only — it is tree-shaken out of the production bundle). `npm run build`
writes `web/dist/`, and `npm run verify` runs every frontend check (types, WCAG contrast for
both themes, gallery coverage, build, bundle checks).

Full installation instructions live in [`docs/installing.md`](docs/installing.md): system
packages, `uv sync`, the systemd **user** unit and `loginctl enable-linger`, upgrades and
backups, and the two topologies (the panel always runs the app and always makes the sound;
only speech synthesis can be offloaded). Kiosk setup has its own guide in
[`docs/kiosk.md`](docs/kiosk.md), and cutting a release — including the multi-arch container
images it publishes — is in [`docs/releasing.md`](docs/releasing.md).

### Docker (supported, not recommended)

Docker works, and the files ship: the multi-stage `Dockerfile` at the repository root and
`deploy/docker-compose.yml`. The native install above is still the recommended path — ten
minutes, no Docker — because every awkward part of running this in a container is
host-specific:

- **Audio passthrough.** A plain ALSA host needs `/dev/snd` and the `audio` group; a desktop
  or a PipeWire/PulseAudio box needs the user's socket and, unavoidably, that machine's UID
  in the mount path.
- **`./data` is a UID-mapped bind mount.** The container user must be the host user that
  owns the directory, so the image takes `UID`/`GID` build args.
- **The compose file builds from the checkout.** That is deliberate: the shipped file cannot
  know which owner you pull from. The release workflow publishes multi-arch (`amd64` +
  `arm64`) images to `ghcr.io/<owner>/chime-deck` on every `v*` tag — see
  [releasing.md](docs/releasing.md) for the tag list and how to point the compose file at one.

**The kiosk browser cannot live in the container.** Chromium needs the host's display, seat
and DRM devices, so run the kiosk on the host ([kiosk.md](docs/kiosk.md)) and point it at
`http://127.0.0.1:8080`.

[`docs/installing.md`](docs/installing.md) §11 has the commands; the caveats and the design
behind them are in `Project_state/design/06-deployment.md` §6.

## Testing

Three tiers, and every result should say which one it came from.

| Tier | Command | Runs where | Proves |
| --- | --- | --- | --- |
| Unit | `uv run pytest -q` | your machine | logic, parsing, error paths — everything stubbed, offline, seconds |
| Integration | `./scripts/test-integration.sh` | a container | real `espeak-ng`, real `libmpv`, real `ffmpeg`, real file handling, ~1 minute |
| aarch64 | `./scripts/test-integration.sh --board` | the ARM board | the same on the target architecture and CPU |

```bash
uv run pytest -q                           # unit tier, no system packages needed
uv run pytest -q -m integration            # only the integration tests (skips what is missing)
./scripts/test-integration.sh              # integration tier in Docker
./scripts/test-integration.sh -- -k audio  # pass arguments through to pytest
./scripts/test-integration.sh --board      # rsync the tree to the board and run it there
./scripts/test-integration.sh --arm64-image  # arm64 correctness under QEMU, no timing claims
```

The board workflow is the two steps the script automates, per design 07 §6:
`rsync` the tree to `~/sd-test` on the board, then
`uv sync --frozen && uv run pytest -q` there. The board needs `ffmpeg` and `libmpv2`
installed for the audio-format tier.

Two honest limits, from design 07 §3. The container plays to `--ao=null`, so "it plays"
means "it decoded and played to a null sink" — never audible output — and a container has
no systemd or PipeWire user session. The board is real aarch64 but has a single HDMI sink,
so "played" there still is not "heard". The full reference is
`Project_state/design/07-testing.md`.

## Where things live

| Path | What |
| --- | --- |
| `src/chime_deck/` | Python backend (FastAPI, MQTT bridge, audio engine, TTS) |
| `tests/` | pytest suite (unit, integration and slow markers) |
| `web/` | Svelte 5 frontend; tokens and component kit built, screens in progress (the built bundle is committed in `web/dist/`) |
| `deploy/` | systemd user units (`chime-deck.service`, `chime-deck-kiosk.service`), the kiosk wrapper and its autostart entry, and the Docker compose file (`docker-compose.yml`) |
| `docs/` | Operator guides: [`installing.md`](docs/installing.md), [`kiosk.md`](docs/kiosk.md), [`releasing.md`](docs/releasing.md) |
| `Dockerfile` | Multi-stage build from the repo root: the product image, the `:piper` tag and the integration harness, all from one file |
| `data/` | Runtime state: database, sounds, icons, TTS cache (*git-ignored*) |
| `Project_state/` | Architecture, design docs and decisions (*git-ignored*) |

## Status

Early development. The architecture and design decisions are settled; the build is being
tracked as tickets. See `tk list` in the repository root.

## Licence

The project's own code is MIT — see [LICENSE](LICENSE).

The Docker images bundle third-party copyleft binaries on top of it, and their
`org.opencontainers.image.licenses` label says so: `MIT AND LGPL-2.1-or-later AND
GPL-2.0-or-later AND GPL-3.0-or-later`. `espeak-ng` (GPL-3.0-or-later), `libmpv2` and `ffmpeg`
come from the Debian base image in *both* tags, so "MIT-only image" would be false of either.

What the separate `:piper` tag exists for is the optional local Piper TTS engine
(`piper-tts`, GPL-3.0-or-later), which is never bundled in a default install: it is an opt-in
extra (`uv sync --extra piper`) or that second tag. Neither tag bakes a voice model — voice
models are licensed separately, the licence varies per voice, and the app downloads the one you
choose, showing its licence in the Speech panel.

Bundled demo sounds are CC0/public domain, with sources recorded alongside them.
