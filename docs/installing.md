# Installing ChimeDeck

The supported install is a **`git clone` + `uv sync` + a systemd *user* unit**. No Docker and
no Node.js, and `uv` fetches its own Python if the system one is older than 3.11. The web UI
is built and committed in `web/dist`, so a fresh clone serves the real dashboard with no
Node.js and no build step (§5).

The target is a fresh 64-bit Debian 12 install to a working doorbell in about ten minutes,
including the reboot you need for the audio group. Everything below is a copy-paste command
unless it says otherwise.

---

## 1. What is supported

| Platform | Supported | Notes |
| --- | --- | --- |
| Debian 12 (bookworm) / 13 (trixie) x86_64 | ✅ | primary target |
| Raspberry Pi OS 64-bit (bookworm / trixie) | ✅ | Lite and Desktop |
| Ubuntu 24.04 x86_64 / arm64 | ✅ | |
| 32-bit ARM (armv7, Pi Zero v1) | ❌ | no aarch64 wheels for Piper/onnxruntime; do not try |
| Home Assistant OS host | ❌ | use a separate box; audio and display belong to the panel |

**Python ≥ 3.11 is required.** `uv` will download a managed CPython if the system interpreter
is older, so you do not need to upgrade the system Python to install this.

---

## 2. System packages

```bash
sudo apt update
sudo apt install -y libmpv2 ffmpeg espeak-ng git curl
```

- `libmpv2` — the audio playback backend. Without it the app starts but has no sound.
- `ffmpeg` — format inspection (the duration probe) for imported sound files.
- `espeak-ng` — the zero-configuration TTS engine, so speech works before you configure
  anything else.
- `git curl` — for the clone and for installing `uv`.

---

## 3. Put your user in the audio group — then restart the user manager

```bash
sudo usermod -aG audio "$USER"
loginctl terminate-user "$USER"      # or just reboot
```

**A new SSH session is not enough.** The group list is captured when the systemd *user
manager* is spawned, not when a shell starts, so the manager has to be re-spawned before the
unit can inherit the `audio` group. Terminating your user session does that; rebooting
definitely does.

Verify **on the running service**, not in your shell, after you have installed and started
the unit in §6:

```bash
systemctl --user show -p MainPID chime-deck
grep ^Groups /proc/<MainPID>/status     # must contain the audio gid
getent group audio                       # the gid you are looking for
```

### Errata: there is no `SupplementaryGroups=audio` in the unit

Design 06 §4 and §9 (and decision D18) show `SupplementaryGroups=audio` in the unit. **That
is wrong and the shipped unit deliberately omits it.** In a *user* unit any value for that
key fails with `status=216/GROUP` — "Changing group credentials failed: Operation not
permitted" — because `setgroups()` requires `CAP_SETGID`, which a user manager does not have.
This was measured on systemd 262 (dev host) and on the ARM board (Armbian/Ubuntu 26.04).

`usermod -aG audio` plus a user-manager restart is the supported route. Do not "fix" a
missing audio group by adding `SupplementaryGroups=` back; it will refuse to start.

---

## 4. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new shell afterwards (or `source ~/.local/bin/env`) so `uv` is on `PATH`.

---

## 5. Clone, sync, and smoke-test in the foreground

```bash
git clone <repo> ~/chime_deck
cd ~/chime_deck
uv sync
uv sync --extra piper        # optional local Piper TTS, GPL-3.0-or-later (D14)
```

**The supported install is a git clone, and the path must stay `~/chime_deck`.** The
wheel also carries the built UI (`chime_deck/static`, force-included from `web/dist`), and
`default_static_dir()` resolves it package-relative, so a `pip install` or `uv tool install`
serves the same dashboard as a clone. Cloning to a different location works only if you also
change `WorkingDirectory`/`ExecStart` in the unit below.

`uv sync --extra piper` is optional: it pulls `piper-tts==1.8.0`, which is GPL-3.0-or-later
(decision D14) and adds a model download (tens of MB per voice). Skip it if you plan to use
`espeak_ng`, `ha_tts`, `remote_tts` or none. (`espeak-ng` is the apt package; the setting
value is `espeak_ng`. The shipped engine values are `ha_tts`, `piper`, `remote_tts`,
`espeak_ng` and `none`.)

Run it in the foreground once:

```bash
uv run chime-deck --host 127.0.0.1 --port 8080
```

What "working" looks like on today's build:

- the log prints `startup: 1/7` through `startup: 7/7 ready`, and
  `startup: 5/7 listening on http://127.0.0.1:8080`;
- `curl -fsS http://127.0.0.1:8080/api/health` returns a JSON health document (HTTP 200);
- `Ctrl-C` exits promptly (a second or so, not thirty).

The **web UI ships with the app** — `web/dist` is committed, the wheel carries it as
`chime_deck/static`, and the root path serves the dashboard — so the first-run wizard and
**Settings → Audio → *Play test tone*** are both there. The integration tier
(`./scripts/test-integration.sh`) additionally exercises the speaker path without a browser.

A placeholder instead of the dashboard means the running install has no built UI at all
(`web/dist` missing from a clone, or a wheel built without the static data); see
*Troubleshooting*.

### Optional shared token

The app ships with authentication **off**: `server.token` is empty and every request is served
to anyone who can reach the port, which is why §11 and the unit keep it on loopback. A
non-empty `server.token` turns a single gate on in front of every state-bearing surface — the
`/api` routes, `/custom-icons/…` and the `/ws` websocket — and it takes effect on the **next
request, with no restart**:

```bash
curl -X PATCH http://127.0.0.1:8080/api/settings \
  -H 'content-type: application/json' \
  -d "{\"server.token\": \"$(openssl rand -base64 24)\"}"
```

Store the value you generated somewhere safe before you send it: the API never returns it
again, only whether one is set. Once it is set, every client must present it — open the UI and
the page asks for it once, or send `Authorization: Bearer <token>` (so `curl` needs
`-H "Authorization: Bearer <token>"` from then on). `GET /api/health` stays open on purpose, so
the reachability check above keeps working with no credential.

To **clear** it, PATCH `null` — the secret rule rejects `""` with "send null to clear a
secret" so a credential cannot be blanked by accident. While a token is set that PATCH must
itself carry the token:

```bash
curl -X PATCH http://127.0.0.1:8080/api/settings \
  -H 'content-type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"server.token": null}'
```

What the browser keeps is a **session cookie**, not the token: `doorbell_session` is
`HttpOnly` (no script can read it), `SameSite=Lax` (not attached to cross-site `PATCH`/`DELETE`),
`Path=/` and valid for 400 days, so a kiosk survives a browser restart. It carries a signed
timestamp whose key is derived from the token, so the token itself is never in the cookie and
rotating the token signs every existing cookie out. **`Secure` is deliberately not set:** the
documented deployment is plain HTTP (loopback by default, optionally a LAN bind), and a
`Secure` cookie is silently discarded on an `http://` origin, which would make the kiosk ask
for the token on every page load. That cookie raises the cost of a stray click or a curious
neighbour on a LAN you already trust; it is not transport encryption.

**If you forget the token**, stop the service and clear the row — the setting is read from the
database at startup, so an empty row means the gate is off again:

```bash
systemctl --user stop chime-deck
sqlite3 ~/chime_deck/data/doorbell.db "DELETE FROM settings WHERE key='server.token'"
systemctl --user start chime-deck
```

**This is not a substitute for keeping the app off the internet.** A shared token on a plain
HTTP port is a door on a LAN you already trust, not a hardened internet-facing deployment: the
traffic is still in the clear, and nothing here rate-limits or audits a guess. Keep it bound to
loopback (the default) or to a LAN you control, exactly as before.

---

## 6. Install the systemd user unit

```bash
mkdir -p ~/.config/systemd/user
cp ~/chime_deck/deploy/chime-deck.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now chime-deck
loginctl enable-linger "$USER"       # MANDATORY
systemctl --user status chime-deck
journalctl --user -u chime-deck -f
```

It is a **user** unit on purpose: the PipeWire/PulseAudio sockets live in the user session
(`$XDG_RUNTIME_DIR`), and a system unit starts before any session exists and silently falls
back to raw ALSA.

`loginctl enable-linger "$USER"` is **mandatory for the acceptance criterion**: without it the
user manager — and therefore this unit — only exists while you are logged in, so the doorbell
stops making noise as soon as you close your SSH session or log out of the desktop. With
lingering enabled, `logind` starts the user manager at boot and the doorbell runs with no
session at all.

The unit also carries the stop-path contract you should not have to think about:

- `KillSignal=SIGTERM` + `SuccessExitStatus=143 SIGTERM`, so `systemctl --user stop` records a
  clean stop as success instead of a crash;
- `TimeoutStopSec=75`, derived from every bounded wait on the shutdown path (35 s of serial
  stages plus 19 s of concurrent work plus 5 s for the future HA websocket client, times
  1.25 and rounded up). A stop waits for the database to close, never SIGKILLs it mid-write;
- `KillMode=mixed`, so the main process gets that budget to reap its own transient children
  and the cgroup is killed at the deadline if it is wedged;
- `Restart=always` with `RestartPreventExitStatus=78`, so a too-new database is held failed
  instead of restarting every three seconds forever (see §9).

---

## 7. Where does synthesis run?

This is the only deployment choice that exists, and it is **not** "where does the app run".

**The panel always runs the app and always makes the sound.** Only *speech synthesis* can be
offloaded. There is no supported topology in which the chime comes out of a machine that is
not the panel — that would not be a doorbell. Running the whole backend on a PC and using the
panel as a dumb display was considered and dropped (D23).

| | **A. Panel does everything (primary)** | **B. Panel + remote renderer** |
| --- | --- | --- |
| App runs on | the panel | the panel |
| Synthesis | `espeak_ng`, `ha_tts`, or local Piper | a renderer box (Piper) or an external provider |
| Sound comes from | the panel | the panel |
| RAM | 1 GB with `ha_tts`/`espeak_ng`; 2 GB+ for local Piper | 1 GB — no model loaded |
| Network | loopback only | panel → renderer over the LAN, bearer token |
| Good for | everything; this is the product | a weak panel that wants better voices |

Setup for B — on the renderer box leave `uv sync --extra piper` in, then:

```bash
chime-deck --role renderer --host 0.0.0.0 --port 8090
# once, against the renderer's data dir: set tts.renderer.token
```

On the panel, in **Settings → Speech** (or the settings API):

```
tts.remote_tts.url   = http://renderer.local:8090
tts.remote_tts.token = <the same secret as tts.renderer.token>
tts.default_engine   = remote_tts
```

The token is the renderer's only authentication: never port-forward it to the internet. Because
static phrases are baked once (D21), the panel makes **one request per phrase for the life of
the button**; only phrases with templates pay the round trip on every press.

> **Errata.** Design 06 §5.1 ("In a split deployment the URL is the server's address…") and the
> B arm of D22 still describe a browser-audio mode where the kiosk browser plays the sound.
> That mode (D23) is dropped. In every supported topology the app and the speaker are on the
> panel.

---

## 8. Can this box run speech?

| Engine | Works on | Notes |
| --- | --- | --- |
| `espeak_ng` (apt package `espeak-ng`) | anything | ~2 MB, effectively instant, robotic |
| `ha_tts` | anything with RAM to play audio | Home Assistant synthesises; we download the file. Needs HA reachable |
| `remote_tts` | anything | another box synthesises, the panel plays. One request per static phrase, ever (D24) |
| `piper` (local) | **2 GB+ RAM** | ~380 MB RSS for a `medium` voice on top of everything else; not supported on 1 GB boards, even `x_low` |

The external-provider engine (`http_tts`, D25 — ElevenLabs, OpenAI, Azure…) ships: add a
provider under Settings → Speech (a preset plus its credentials; the API key is write-only),
then select `http_tts` as the default engine and pick that provider. A paid provider is billed
per character, and a templated phrase is billed on **every** press unless you accept it at save
time. Presets are data files, so a vendor can be added without a code change.

A 1 GB panel is a **fully supported doorbell** with `ha_tts` or `espeak_ng`. Only local Piper
needs the bigger machine — and if the machine cannot be bigger, use topology B and offload the
synthesis, not the application.

---

## 9. Upgrades, backups, and the schema-version refusal

### Upgrade

```bash
cd ~/chime_deck
git pull
uv sync
systemctl --user restart chime-deck
```

Database migrations run automatically on start and are **forward-only**. There is no downgrade
path.

### Back up first

Back up `data/` — or use **Settings → About → Export config** — and in
particular:

- `data/doorbell.db` (all configuration, buttons, entities, press log),
- `data/sounds/`, `data/icons/` (imported media).

`data/tts-cache/` and `data/tts/baked/` are disposable: both are regenerated on demand. Copying
the bakes only saves you one synthesis request per static phrase.

### Schema newer than the code

If the database was written by a **newer build** than the one you are starting, the CLI refuses
to start *before binding a port*, logs exactly one line:

```
database schema version <db> is newer than the newest version this build supports (<code>): refusing to start. Upgrade ChimeDeck (git pull && uv sync) or restore a backup taken with this version; downgrades are not supported.
```

and exits **78**.

The unit sets `RestartPreventExitStatus=78`, so systemd holds the unit `failed` instead of
restarting it every three seconds on a headless panel. `systemctl --user status chime-deck`
shows the refusal in the journal.

The fix is to upgrade the code (`git pull && uv sync`, then restart) or to restore a backup
taken with this version. **Downgrades are not supported** — attaching an older build to a newer
schema would corrupt data, which is exactly what the refusal prevents.

The read-only check described above is deliberately best effort: it opens the database without
creating it, and it treats any SQLite error as "cannot tell". A version that is still only in an
un-checkpointed WAL, or one sitting behind a hot rollback journal left by a crash or a power cut,
is invisible to it. That gap is closed before anything is bound: the CLI then opens the store
once, before uvicorn or the renderer start, so the store's own migration guard makes the same
refusal with the same single ERROR line and the same exit **78**. `RestartPreventExitStatus=78`
therefore holds the unit `failed` on every too-new path, and no port is ever bound for it.

---

## Upgrading from HA Super Doorbell

ChimeDeck is the same application under a new name: the database, the settings and the media
are untouched by the rename. What changes is naming, and four of the changes below need you to
act.

- **Environment variables are now `CHIMEDECK_*`** (`CHIMEDECK_HOST`, `CHIMEDECK_PORT`,
  `CHIMEDECK_DATA_DIR`, `CHIMEDECK_DATA`, `CHIMEDECK_LOG_LEVEL`, `CHIMEDECK_LOG_FILE`, …). The
  old `SUPER_DOORBELL_*` names are still read for one release and log a deprecation warning
  naming the replacement, so an installed unit that still sets the old names keeps working
  during that window. Update it anyway: after that release the old names are ignored.
- **The unit files, the console script and the kiosk wrapper are renamed**: `chime-deck.service`,
  `chime-deck-kiosk.service`, `deploy/chime-deck-kiosk.desktop`,
  `/usr/local/bin/chime-deck-kiosk`, and `chime-deck` on the command line. Disable the old unit,
  install the new one, and reinstall the wrapper under its new name, because the kiosk unit's
  `ExecStart` names it:

  ```bash
  cp ~/chime_deck/deploy/chime-deck.service ~/.config/systemd/user/
  sudo install -m 755 ~/chime_deck/deploy/kiosk.sh /usr/local/bin/chime-deck-kiosk
  systemctl --user daemon-reload
  systemctl --user disable --now super-doorbell
  systemctl --user enable --now chime-deck
  ```

- **The checkout directory is now `~/chime_deck`.** If yours is still `~/HA_Super_Doorbell`,
  either move it (`mv ~/HA_Super_Doorbell ~/chime_deck`), or edit `WorkingDirectory`,
  `ExecStart` and `CHIMEDECK_DATA_DIR` in your installed unit to point at the old path — the
  shipped unit is written for `~/chime_deck`.
- **The MQTT identifiers, the device name and the Home Assistant entity ids change.** The old
  `super_doorbell_*` discovery entities would otherwise linger as ghosts, because discovery is
  not retained. On the first start after the upgrade the app publishes empty payloads for them
  — removing the discovery configs and blanking their retained state and availability topics —
  so they clear themselves. No broker surgery and no Home Assistant restart are needed.
- **Selection entity ids change** from `binary_sensor.ha_super_doorbell_<slug>` to
  `switch.chime_deck_<slug>`. Automations that reference the old ids must be updated.
  See [home-assistant.md](home-assistant.md) for the switch contract and the migration.
- **A configuration export made before the rename still imports.** Documents carrying the old
  `"format": "ha-super-doorbell/config"` tag are accepted by **Settings → About → Import**
  exactly as before, so there is nothing to re-export.

---

## 10. Not in this batch

Still future work, deliberately undocumented here:

- a published container image (the release pipeline is HSD-in8q),
- the reverse-proxy "if you must" snippet.

The Docker files now ship, and §11 explains why they are supported but not the recommended
path.

The kiosk display now has its own guide: see [kiosk.md](kiosk.md) for the Chromium wrapper,
the Desktop autostart entry and the `cage` user unit on Lite images. A Lite kiosk needs
exclusive access to the display — the compositor has to be the only thing holding
`/dev/dri/card0` — so if the panel never comes up, read
[kiosk.md §0](kiosk.md#0-read-this-first-the-kiosk-needs-exclusive-access-to-the-display)
first; a desktop session left running is the usual cause. If the panel comes up at
a mode that is unusable on a small screen — the EDID-preferred mode is inherited by default —
pin it from inside the compositor with `wlr-randr`; see
[kiosk.md §7](kiosk.md#7-pinning-the-display-mode). Do not force it on the kernel command
line: that route can cost you the display entirely and needs physical access. The kiosk is
a second install surface with its own failure modes (seat, DRM, HDMI, screen blanking),
which is why it is not a section of this backend guide.

---

## 11. Docker (supported, not recommended)

Everything above is the recommended install: ten minutes, no Docker, no Node.js. Docker works,
and the files ship — the multi-stage `Dockerfile` at the repository root and
`deploy/docker-compose.yml` — but three things make it supported rather than recommended, and
all three belong to the host:

- **Audio passthrough is host-specific.** On a plain ALSA host the container needs `/dev/snd`
  and the `audio` group. On a desktop, or a Pi running PipeWire/PulseAudio, it needs the user
  session's audio socket instead, and that socket's path contains your UID. The compose file
  ships the ALSA form, with the PipeWire variant as a commented block. Note that
  `group_add: audio` names the group inside the image — gid 29 on the supported Debian-based
  distributions — while the passed-through devices keep your host's gid, so on a host whose
  `audio` group is not 29 put the number from `getent group audio` in the compose file.
- **`./data` is a bind mount, so its UID has to match.** The container runs as a non-root user
  (the image's `doorbell` user) and that user must own `./data`, or the application cannot
  write its database. The image takes `UID`/`GID` build args for exactly this; set them to
  your own `id -u` / `id -g`.
- **The kiosk browser cannot live in the container.** Chromium needs the host's display, seat
  and DRM devices. Run the app in the container and the kiosk on the host
  ([kiosk.md](kiosk.md)), pointed at `http://127.0.0.1:8080`.

The short version:

```bash
git clone <this repo> && cd chime_deck
mkdir -p data                 # owned by you: the container user must be able to write it
docker compose -f deploy/docker-compose.yml up -d --build
curl -fsS http://127.0.0.1:8080/api/health
```

The compose file builds the repo-root `Dockerfile`'s `runtime` target. The UI is built inside
the image, so a Docker host needs no Node.js at all. Only `127.0.0.1:8080` is published, on
purpose: authentication is off by default (D20) and the optional shared token (§5) does not
change that, so the app must never be published beyond loopback.

For a PipeWire/PulseAudio host, use the commented block at the end of the service in the
compose file. It replaces the `devices`/`group_add` entries with a bind mount of
`/run/user/$(id -u)/pulse` and sets `PULSE_SERVER` to that socket. The UID in the path is
yours — that is the host-specificity above, not a typo.

The compose file builds from the checkout rather than pulling, because the shipped file cannot
know which owner you would pull from. If you would rather pull than build, the release workflow
publishes multi-arch (`linux/amd64` + `linux/arm64`) images to `ghcr.io/<owner>/chime-deck`
on every `v*` tag:

```bash
docker pull ghcr.io/<owner>/chime-deck:<version>
```

Then replace the compose file's `image:` line with that tag and delete `build:`.
[releasing.md](releasing.md) has the tag table (`<version>`, `latest`, `<version>-piper`,
`piper`) and the pre-flight list for cutting a release.

### Local Piper in Docker

The default image has no local speech engine, for the same reason the default install does not
(D14: Piper is GPL-3.0-or-later, and the default install stays free of it). The `:piper` tag is
a second image, built from the same Dockerfile, that adds the piper extra:

```bash
docker build --target piper -t chime-deck:piper .
```

That tag carries its licence notice in its own image metadata, so the notice survives being
copied, pushed or archived without this page:

```bash
docker image inspect --format '{{json .Config.Labels}}' chime-deck:piper
```

Both tags report the same aggregate `org.opencontainers.image.licenses` value. That is not a
mistake and it is not a licence change: the default image already bundles `espeak-ng`
(GPL-3.0-or-later), `libmpv2` and `ffmpeg` from Debian. The project's own code is MIT; the image
carrying it aggregates third-party copyleft binaries. `:piper` adds Piper itself, and its
`description` label says so.

No voice model is baked into either tag. A voice model is licensed separately from Piper and the
licence varies per voice — some are CC0 or public domain, some are CC-BY-SA — so the app
downloads the one you pick at runtime. Open the Speech panel and install a voice there, or:

```bash
curl -fsS -X POST http://127.0.0.1:8080/api/voices/en_GB-alan-low/download
```

Voices are written to `<data dir>/voices`, inside the `/data` volume, so they survive replacing
the container. The Speech panel shows each voice's licence before you download it.

---

## Verifying the unit on a real machine (integration tier)

The systemd assertions are opt-in because they write a throwaway unit into
`~/.config/systemd/user` and need a live `systemd --user` session. Run them from a committed
checkout (`~/chime_deck`) whose `.venv/bin/chime-deck` exists:

```bash
cd ~/chime_deck
CHIMEDECK_TEST_SYSTEMD=1 \
CHIMEDECK_EXPECTED_HEAD="$(git rev-parse HEAD)" \
  uv run pytest -q tests/test_stop_path.py
```

The two gated tests install the shipped unit under a unique name with a drop-in that points it
at a temporary data directory and a free port, then prove: `kill -9` is followed by a new
MainPID (and `NRestarts` increases), the restarted service is in the `audio` group, its
database passes `integrity_check`, and a database newer than the code leaves the unit `failed`
with `RestartPreventExitStatus=78` instead of restarting every three seconds. They clean up the
unit, its drop-in and its failed state, and restore login lingering unchanged.

---

## Troubleshooting

**The UI loads but there is no sound.**
Check, in this order: the unit is a *user* unit (`systemctl --user status chime-deck`, not
the system one); your user is in the `audio` group (`getent group audio`, and
`grep ^Groups /proc/<MainPID>/status` for the service itself); the user manager was re-spawned
after the `usermod` (`loginctl terminate-user "$USER"` or a reboot); and
**Settings → Audio → *Play test tone***. If the test tone is silent, the problem is the device
or the sink, not the app. If it plays, the problem is the phrase or the engine.

**The unit will not start.**
`journalctl --user -u chime-deck -n 50 --no-pager` first. `status=216/GROUP` means someone
re-added `SupplementaryGroups=` to the unit — remove it and use `usermod` (§3). If the binary is
missing, `.venv/bin/chime-deck` does not exist: run `uv sync` in `~/chime_deck`.

**I get a placeholder page instead of the UI.**
The server found no built UI: `startup: 4/7 no built UI found at <path>` names the directory it
looked in. In a clone that path is `web/dist`, so restore it with `git checkout -- web/dist`
(the bundle is committed, there is no build step). In a wheel/`pip`/`uv tool` install it is
`<venv>/lib/python3.x/site-packages/chime_deck/static`, so a wheel that lacks it was built
without the frontend data — replace it with a release wheel or a clone.

**It refuses to start and the log mentions the schema version, exit 78.**
The database is newer than the code. Upgrade with `git pull && uv sync` and restart; or restore
a backup taken with this build. Do not try to downgrade the schema.
