# Kiosk display

The kiosk is a small machine showing the dashboard full-screen: no address bar,
no cursor, no scrolling. This is a separate file from [installing.md](installing.md)
because it is a second install surface with its own failure modes (seat, DRM,
HDMI, screen blanking); installing.md is the backend guide. The backend may be
on this machine (the default) or on another one (the split deployment, D22).

## 0. Read this first: the kiosk needs exclusive access to the display

On a Lite image the kiosk compositor (`cage`, or weston in §7.5) has to be the only
process driving the display. There is exactly one DRM master per card, and a desktop
session takes it on behalf of whatever it is drawing. With a session running, cage
does not fail to start — it starts and paints into a display it does not own.

**Measured on the reference board (Radxa ZERO 3W, Armbian/Ubuntu 26.04).** The machine
still had the KDE desktop enabled, and every "the panel never comes up" report had the
same shape:

| Observation | Value |
| --- | --- |
| Holders of `/dev/dri/card0` | `kwin_wayland` (pid 2835) and `systemd-logind` (pid 1093) |
| `systemctl get-default` | `graphical.target` |
| Display manager | `sddm`, running |
| Memory | 678 MB of 967 MB used (`plasmashell` 38 MB, `kwin_wayland` 28 MB, plus Xwayland and sddm) |
| cage's journal | `backend/drm/atomic.c:81 connector HDMI-A-1: Atomic commit failed: Invalid argument`, in a loop |
| The unit | `active`, panel blank |

`systemd-logind` holding the card is normal: it holds the device to manage the seat and
never keeps DRM master. That is why it is the one holder the wrapper ignores when it
checks (§7.6). Anything else holding the card is a display server using it.

The fix is two commands on the kiosk machine, once:

```bash
sudo systemctl set-default multi-user.target
sudo systemctl disable --now sddm        # the display manager, whatever yours is called
```

Then cold boot the machine. Nothing else has to be touched by hand. Measured
afterwards on the same board: cage is the only client on `/dev/dri/card0`, the journal
has zero `Atomic commit failed` lines, the mode change succeeds
(`kiosk: display mode is now 1280x720 on HDMI-A-1`), the panel comes up by itself at
1280x720, audio starts against the HDMI sink, and the unit is `active` with
`NRestarts=0`.

### Verifying it

```bash
fuser -v /dev/dri/card0           # or: lsof /dev/dri/card0
systemctl get-default             # multi-user.target
journalctl --user -u chime-deck-kiosk -b --no-pager \
  | grep -E 'Atomic commit failed|kiosk ready:|another display server'
```

`fuser` (or `lsof`) should list the compositor and maybe `systemd-logind`, and nothing
else. A start that worked has no `Atomic commit failed` line and one `kiosk ready:`
line, for example:

```
kiosk ready: displaying on HDMI-A-1 (enabled, dpms On; mode not reported by this kernel)
```

A line containing `another display server is using` means a desktop session is still
running; §7.6 quotes that warning in full.

### The trade-off

That machine gets no desktop session: it boots to a text console and the kiosk takes
the display. In exchange roughly 400 MB of the 967 MB comes back, along with a lot of
the boot contention. A wall panel wants that RAM more than a desktop does. If you want
a desktop on the same box, run the browser inside it rather than beside it — that is
the Desktop image (§4), where the desktop *is* the compositor.

### If you skip it

The wrapper warns once at startup and starts anyway:

```
warning: another display server is using /dev/dri/card0 (kwin_wayland pid 2835); the kiosk compositor cannot drive the display while it runs -- disable the desktop session and boot to multi-user.target (docs/kiosk.md section 0)
```

Then cage logs `Atomic commit failed: Invalid argument` in a loop and the panel stays
dark while the unit reads `active`. The warning is advisory, not a refusal: the browser
may be deliberately started inside a desktop session (§4), and that case has to keep
working, which is why the check only warns. The name and pid in the message are whatever
held the card at that moment.

## 1. Which image?

| Image | How the kiosk runs | Files |
| --- | --- | --- |
| Desktop (Raspberry Pi OS Desktop, Debian/Ubuntu desktop) | XDG autostart entry after login, plain Chromium | `deploy/chime-deck-kiosk.desktop` |
| Lite (no desktop) | `cage`, a minimal Wayland kiosk, under a systemd *user* unit | `deploy/chime-deck-kiosk.service` + `deploy/kiosk.sh --cage` |

The kiosk URL is configurable in both; the default is loopback.

## 2. Install the wrapper

```bash
sudo install -m 755 deploy/kiosk.sh /usr/local/bin/chime-deck-kiosk
chime-deck-kiosk --help
```

The wrapper is self-contained and needs no checkout at run time. The backend may
be a `uv tool install` wheel, so do not assume `~/chime_deck` exists. Both
images use the same file; `--cage` selects the Lite path.

## 3. The URL contract

Precedence is CLI > environment > default.

| Source | Example |
| --- | --- |
| CLI positional | `chime-deck-kiosk http://127.0.0.1:8080/?kiosk=1` |
| CLI option | `chime-deck-kiosk --url http://bigpc.local:8080/?kiosk=1` |
| Environment | `KIOSK_URL=http://bigpc.local:8080/?kiosk=1` |
| Default | `http://127.0.0.1:8080/?kiosk=1` (backend on this machine) |

`?kiosk=1` is the UI contract: it hides the chrome and cursor and stops the page
scrolling. `?kiosk=0` is the escape hatch for debugging with the controls
visible. A split deployment points the panel at the server:

```
http://bigpc.local:8080/?kiosk=1
```

Arguments after `--` are passed to the browser after the built-in flags, for
example `chime-deck-kiosk -- --lang=en-GB`.

If the backend has a shared token set (`server.token`, see
[installing.md §5](installing.md#optional-shared-token)), the panel asks for it once
on the first load and the session cookie keeps it signed in across restarts, so
re-imaging a kiosk means entering the token once more and nothing else. Nothing
about the URL changes: the cookie travels with the same-origin page load and the
websocket handshake, and the token never appears in the address bar.

## 4. Desktop image (autostart)

```bash
mkdir -p ~/.config/autostart
cp deploy/chime-deck-kiosk.desktop ~/.config/autostart/
```

The entry runs `/usr/local/bin/chime-deck-kiosk` and waits five seconds
(`X-GNOME-Autostart-Delay=5`) so the desktop session is ready first. For a split
deployment, edit `Exec=` and append the URL:

```
Exec=/usr/local/bin/chime-deck-kiosk http://bigpc.local:8080/?kiosk=1
```

Test the wrapper by hand (`chime-deck-kiosk`) before relying on login
autostart; stop it from another terminal with
`pkill -TERM -f chime-deck-kiosk`.

There is no DRM conflict to resolve on this image: the desktop session is the
compositor and it owns `/dev/dri/card0`, which is exactly right. The exclusive-access
prerequisite in §0 is about the Lite/cage path, where cage has to be the only DRM
client.

### Screen blanking

On X11 the wrapper runs `xset s off`, `xset -dpms` and `xset s noblank` every
time it starts, so the setting survives a reboot. On a Wayland desktop `xset`
cannot work (and the wrapper says so). Use the session's own settings; for GNOME:

```bash
gsettings set org.gnome.desktop.session idle-delay 0
gsettings set org.gnome.desktop.screensaver idle-activation-enabled false
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
```

Other desktops differ. Confirm the setting after a reboot.

## 5. Lite image (cage + user unit)

`cage` is a kiosk compositor: it shows exactly one client and nothing else.

```bash
sudo apt install -y cage
sudo apt install -y seatd        # Debian/Ubuntu; see the seat note below
sudo systemctl enable --now seatd
sudo usermod -aG video "$USER"
sudo install -m 755 deploy/kiosk.sh /usr/local/bin/chime-deck-kiosk

mkdir -p ~/.config/systemd/user
cp deploy/chime-deck-kiosk.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now chime-deck-kiosk
loginctl enable-linger "$USER"
```

Restart the user manager once after the `usermod` (`loginctl terminate-user
"$USER"`, or reboot) so the service inherits the `video` group.

`loginctl enable-linger` is **mandatory**. Without it the user manager exists
only while the user is logged in, so the kiosk would not start at boot and would
die on logout. Verify with `loginctl show-user "$USER" -p Linger`; it must say
`yes`.

A seat is what lets a process own the display and input devices. On the
reference ARM board (Radxa ZERO 3W, Armbian/Ubuntu 26.04) the packaged
`/run/seatd.sock` is `root:video 0770`, so a user in the `video` group can take a
seat from a lingering user unit — measured, not assumed. On other distributions
check the socket (`ls -l /run/seatd.sock`) and join whichever group owns it
(`usermod -aG <group>`); some packages use a dedicated `_seatd` group.

The unit runs exactly this, with the built-in flags from `deploy/kiosk.sh`
(shortened here):

```
cage -- chromium --kiosk --app=http://127.0.0.1:8080/?kiosk=1 ...
```

`cage` exits when its only client exits. The wrapper restarts the pair with an
exponential backoff: 1, 2, 4, 8, 16, 32, 60 seconds, capped at
`KIOSK_MAX_RESTART_DELAY`. A run that lasted at least `KIOSK_HEALTHY_AFTER`
seconds (default 60) resets the backoff, so a browser that crashes once a day
restarts immediately. Check, then stop:

```bash
systemctl --user status chime-deck-kiosk
journalctl --user -u chime-deck-kiosk -f
systemctl --user stop chime-deck-kiosk
systemctl --user disable chime-deck-kiosk
```

## 6. Seat and DRM failures

**A running desktop session comes first.** If cage or weston will not come up, or comes
up to a dark panel, read §0 before anything else: a session holding `/dev/dri/card0` as
DRM master stops the kiosk compositor from setting any mode, and the wrapper's own
warning names the process that holds it.

**No seat.** `cage` reports that it could not open a seat (`Could not open seat`
or `Could not create backend`). Either install and start `seatd` and put the user
in the group that owns its socket (`video` on the reference board), or give the
machine a real autologin session on a VT.

**No DRM device at all.** `cage` fails with `Found 0 GPUs` or cannot open
`/dev/dri/card0`. Check the nodes and the groups:

```bash
ls -l /dev/dri
id | tr ' ' '\n' | grep -E 'video|render'
```

The user has to be in `video`, and in `render` as well if the node is group-`render`.
Then check the seat itself: `seatd` running, and the user in the group that owns
`/run/seatd.sock` (§5).

cage 0.2.1 options that help here: `-d` don't draw client-side decorations,
`-D` debug logging (the way to see the seat/DRM messages above), `-m extend|last`
to extend across all outputs or use only the last connected one, `-s` allow VT
switching, and `-v` print the version and exit. Use `--` before the application:

```bash
cage -- chromium --kiosk --app=http://127.0.0.1:8080/?kiosk=1
```

The weston path (§7.5) uses weston's DRM backend, which takes the same seatd seat and the
same `/dev/dri` nodes as cage: a seat that works for cage works for weston, and the checks
above apply unchanged.

## 7. Pinning the display mode

A bare compositor takes whatever DRM mode the kernel selects, and the kernel selects
the mode the panel prefers in its EDID. On the reference board that is 1920x1080; on
a 7-inch class panel the same 1080p frame is squeezed into a few inches, so every table
cell and button is physically tiny and the dashboard is unreadable from arm's length.
The mode has to be pinned rather than inherited.

There are three routes, and they are not equally safe. The good one sets the mode from
inside the compositor, from the session that already owns the display, and needs
nothing but `wlr-randr`. The next lets a compositor choose the mode as its very first
modeset, so the mode never changes after startup (§7.5). The last forces it on the
kernel command line, which is the one that can cost you the display, and it needs
physical or serial access to the machine (§7.2).

**The prerequisite is §0, not a mode trick.** Before any of this can work, the kiosk
compositor has to be the only process holding the display. A desktop session in the way
produces exactly the symptoms that used to be blamed on the mode mechanisms: a rejected
atomic commit, `active` with a dark panel, nothing coming up by itself.

**What actually went wrong on the reference board.** The record is kept because the
wrong conclusion was expensive and this page used to publish it. Three panels came up
dark while the wrapper tried the kernel command line, then a post-startup `wlr-randr`,
then weston; a fourth darkness happened on a plain cold boot with no mode setting at
all. All four looked like mode problems and none of them were. A full KDE desktop
session was running and holding `/dev/dri/card0` as DRM master — §0 has the measured
detail: `kwin_wayland` pid 2835, `graphical.target` as the default, `sddm` running,
678 MB of 967 MB of RAM in use. While KWin owned the device no compositor could modeset:
cage logged `backend/drm/atomic.c:81 connector HDMI-A-1: Atomic commit failed: Invalid
argument` in a loop, the unit read `active` with a blank screen, and nothing came up by
itself. The "cold-boot race" this page used to describe was the same thing seen from the
wrong angle: nothing raced.

With the desktop session disabled (§0) the mode machinery is what it always was: it
works. Measured on the same board after `multi-user.target` plus a cold boot, with
nothing touched by hand: cage is the only client on `/dev/dri/card0`, zero
`Atomic commit failed` lines, the apply-once `wlr-randr` change succeeds
(`kiosk: display mode is now 1280x720 on HDMI-A-1`), the panel comes up at 1280x720, and
the unit is `active` with `NRestarts=0`.

One rule from that history survives as a genuine safety invariant: **a rejected atomic
commit is never retried.** A retry re-issues the same commit the kernel has already
refused, so a compositor loops on the refusal instead of falling back, and a panel that
could have shown the EDID mode shows nothing. Every route below asks at most once.

### 7.1 From inside the compositor (recommended for Lite/cage)

```bash
sudo apt install -y wlr-randr
```

That is the whole dependency (0.4.1 on Ubuntu). Version 0.4.1 is enough because the
wrapper only needs the plain listing plus the `--output` and `--mode` flags.

The wrapper reads three settings, all only on the `--cage` path:

| Setting | Meaning |
| --- | --- |
| `KIOSK_MODE` | Mode to pin, as `WIDTHxHEIGHT`, e.g. `1280x720` |
| `KIOSK_OUTPUT` | Connector to pin it on, e.g. `HDMI-A-1`. Optional |
| `KIOSK_MODE_WAIT` | Seconds to wait for an enabled output before the single mode change. Default `10`, `0` = one immediate probe |

`KIOSK_MODE_WAIT` is only about the display coming up: raise it on a slow panel that
needs longer than the default ten seconds to modeset, and set it to `0` only when it is
already known the compositor is up — experimenting by hand against a running session,
for instance. Every second of it is a second the browser has not started yet, so the
default is the right value for a cold boot.

To make it permanent, add it to the user unit and reload:

```bash
systemctl --user edit chime-deck-kiosk.service
```

Add these under `[Service]`:

```
Environment=KIOSK_MODE=1280x720
Environment=KIOSK_OUTPUT=HDMI-A-1
```

Then apply it:

```bash
systemctl --user daemon-reload
systemctl --user restart chime-deck-kiosk
```

The same thing by hand, which is the quicker way to try a mode before editing the unit:

```bash
chime-deck-kiosk --cage --mode 1280x720
```

`--mode WxH` on the command line beats `KIOSK_MODE` in the environment, the same
CLI-then-environment-then-default precedence as the URL (§3). `KIOSK_OUTPUT` has no
command-line form; it is a unit-file setting only. If it is unset, the wrapper uses the
first output that `wlr-randr` reports as enabled, which is right on a single-panel machine
and worth naming explicitly only when there is more than one connector. With no
`KIOSK_MODE` nothing changes: the compositor inherits the kernel's mode as before. A
Desktop image ignores all three settings entirely — the desktop owns the display there,
so use its own display settings (§4).

Mechanically the wrapper stops running `cage -- chromium ...` and runs
`cage -- sh -c '<script>' ...` instead. The script is what runs inside cage's Wayland
session: it gets the mode onto the output, then `exec`s Chromium, so the browser never
paints a frame at the wrong size.

At cold boot the script starts while cage has only just taken the DRM master, so this
change is the first thing asked of the connector after the compositor's own modeset.
That is the riskier of the two moments to ask for a mode — against an already-running
compositor it is unremarkable — and a change the kernel's atomic commit check refuses
looks like this:

```
backend/drm/atomic.c:81 connector HDMI-A-1: Atomic commit failed: Invalid argument
```

On this board that line used to be read as a property of the driver's mode handling
(see the history in §7). It was not: it was a desktop session holding the DRM master.
With §0 done, the same apply-once change succeeds at cold boot and the journal says
`kiosk: display mode is now 1280x720 on HDMI-A-1`.

The rule that came out of that misdiagnosis is still the right rule, for its own reason:
**never retry a rejected commit.** One early revision retried up to three times, half a
second apart; a retry re-issues the same refused commit, cage loops on that line instead
of recovering, and a panel that could have shown the EDID mode shows nothing. So the
wrapper asks once, falls back, and starts the browser either way.

The sequence is: poll `wlr-randr` every 0.25 s, for up to `KIOSK_MODE_WAIT` seconds
(default 10), until an output reports itself as enabled — `Enabled: yes`, or a mode
marked as current. Readiness is read from that listing, never assumed: with
`KIOSK_OUTPUT` set, that connector must be the one reporting enabled; unset, the first
enabled output wins. Then check the requested mode appears in that output's advertised
list, apply it with exactly one `wlr-randr --output <OUT> --mode <MODE>` call, and
confirm by re-reading the current mode, logging `display mode is now <MODE> on <OUT>` at
INFO. If the mode is already current, no commit is issued at all — a needless commit is
a needless chance to be rejected.

Every failure path warns and starts the browser anyway, so the journal says exactly what
happened:

```
output HDMI-A-1 does not offer 1280x720; leaving the display mode alone (available: ...)
no enabled Wayland output appeared within 10s; leaving the display mode alone
output HDMI-A-1 was not reported enabled within 10s; leaving the display mode alone
could not set 1280x720 on HDMI-A-1; continuing at the display's own mode
output HDMI-A-1 does not report 1280x720 as current after setting it; continuing
```

A mode the output does not advertise names both the mode asked for and the ones the
output does offer, and nothing is changed. An output that never reports itself enabled
in time uses the second or third line, depending on whether `KIOSK_OUTPUT` named it. A
mode change that the compositor accepts the request for but rejects in the commit uses
the fourth line — one line, no retry. A verification read that does not show the mode as
current uses the fifth. In all of them Chromium starts, at whatever mode the display is
actually in.

The invariant underneath all of that is one rule: a rejected atomic commit is never
retried. Re-issuing it is precisely what turns one refused modeset into a dead panel, so
the wrapper drops back to the display's own (EDID) mode instead. A readable-but-wrong-size
dashboard beats no dashboard; a tight loop over a commit the kernel has already rejected
beats neither.

The waiting is bounded: the browser starts after at most `KIOSK_MODE_WAIT` extra seconds
and never later, and a display that never becomes ready cannot hold it back
indefinitely.

`wlr-randr` missing altogether is the same story one step earlier: `KIOSK_MODE` is
ignored with a warning that says so, and the kiosk still comes up at the EDID mode. The
unit never fails over any of this, which is deliberate: a panel or driver that refuses
the mode must still show the dashboard.

The mode is set on every wrapper start, so it survives a reboot and a kiosk restart. It
does not persist after the kiosk stops, and that is intentional — it is session state,
not a hardware configuration, so stopping the kiosk hands the display back the way the
kernel left it.

To discover what the panel will actually accept, ask it from its own session. An ssh
shell has no Wayland session of its own, so it has to be told which one to talk to, and
the socket number is not fixed — measured on the reference board, `wayland-1` before a
kiosk restart and `wayland-0` after — so look it up first:

```bash
ls "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" | grep wayland
# then use the socket it printed:
XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 wlr-randr
```

(substitute whichever socket the first command lists). On the reference panel that
prints `HDMI-A-1 "Daewoo Electronics Company Ltd HDMI 0 (HDMI-A-1)"` with `1920x1080`
marked current and `1280x720` among the modes.

Read it as blocks: the first field of each block is a connector name (`HDMI-A-1`), and
under `Modes:` one of the listed modes is marked as the current one. Use one of those
exact `WxH` values for `KIOSK_MODE`; a mode the panel does not advertise is refused,
which is the failure described above and not a fault in the wrapper. On a machine with
no compositor running — a console-only install, or a panel still booting — the
connector names are in `/sys/class/drm/` instead:

```bash
ls /sys/class/drm/            # card0-HDMI-A-1 -> the connector is HDMI-A-1
cat /sys/class/drm/card0-HDMI-A-1/modes
```

To check it took effect, restart and grep the journal for the wrapper's own lines:

```bash
systemctl --user restart chime-deck-kiosk
journalctl --user -u chime-deck-kiosk -b --no-pager \
  | grep -E 'will set the display mode|was not reported enabled|could not set|not installed'
```

A working start prints a line beginning `will set the display mode inside cage:` naming
the mode, the output and how long it will wait, and then either `display mode is now` or
`display mode is already` — and nothing else on that topic. A `was not reported enabled`
line means no output came up within `KIOSK_MODE_WAIT`; `does not offer`, `could not set`
or `not installed` mean the mode was refused or never attempted. Any of those and
Chromium is on the EDID mode. Confirm from the compositor side with the `wlr-randr`
incantation above: the pinned mode is now the one marked current. Reboot and check both
once more — the unit is what makes it sticky, and the point of the exercise is a panel
that comes up correct with nobody logged in.

Confirmed on the reference panel, at cold boot, with the desktop session disabled (§0):
`KIOSK_MODE=1280x720` in a user-unit drop-in, `KIOSK_OUTPUT` deliberately unset so the
connector came from `wlr-randr`, gave a panel at `1280x720 @ 60.000 Hz`,
`kiosk: display mode is now 1280x720 on HDMI-A-1` in the journal, and no
`Atomic commit failed` line anywhere. The apply-once and fall-back paths are covered by
the suite against a fake `wlr-randr`: exactly one commit, no retry after a refusal, and
Chromium exec'd on every failure. What is still worth care is a change of panel, cable or
connector: keep a copy of a known-good drop-in and a way back in over serial or a second
machine before you reboot an unfamiliar display.

### 7.2 On the kernel command line (last resort — can leave you with no display)

> **Warning.** This is the one route that can leave you with no display, and the one
> that cannot be undone over ssh. Measured on the Radxa ZERO 3W reference panel:
> `video=HDMI-A-1:1280x720@60` was added to `extraargs` in `/boot/armbianEnv.txt`,
> `/proc/cmdline` showed the parameter applied, and the board booted — but the kiosk had
> no graphics at all. The text console stayed up while cage looped on
> `backend/drm/atomic.c:81 connector HDMI-A-1: Atomic commit failed: Invalid argument`,
> thousands of lines of it. That loop was the desktop session holding the DRM master
> (§0), not the command line — with §0 done the same board modesets and pins its mode
> fine without touching `/boot` at all, which is why §7.1 is the route to prefer. The
> parameter was reverted from a backup and the display came back at 1080p. Only try this
> with physical or serial access to the machine: over ssh alone, you may not get the
> display back. Any `Atomic commit failed: Invalid argument` you see along the way is
> the signature §7.1 refuses to retry.

A reader on other hardware may still need it, so the sequence is: back up first.

```bash
sudo cp /boot/armbianEnv.txt /boot/armbianEnv.txt.bak-$(date +%Y%m%d-%H%M)
```

Then add the parameter to the existing `extraargs=` line (append, do not replace):

```
video=HDMI-A-1:1280x720@60
```

The connector name must match the kernel's, not a guess: check `ls /sys/class/drm/`
first, and use the name in `card0-<name>`. A wrong name means no display at all.
Reboot, then verify:

```bash
cat /proc/cmdline
cat /sys/class/drm/card0-HDMI-A-1/modes
systemctl --user is-active chime-deck-kiosk
journalctl --user -u chime-deck-kiosk -b --no-pager | grep 'Atomic commit failed'
```

`/proc/cmdline` proving the parameter applied means nothing on its own; the modes file
must list your mode and the journal must be free of `Atomic commit failed`. If it is
not, revert from the backup you made:

```bash
sudo cp /boot/armbianEnv.txt.bak-<stamp> /boot/armbianEnv.txt
sudo reboot
```

The Raspberry Pi has its own, safer equivalent for this — `config.txt` rather than the
command line — in §8.

### 7.3 Which should I use?

| Situation | Route |
| --- | --- |
| Lite image, `cage`, `wlr-randr` installed | §7.1 — set it from inside the compositor. This is what the reference board runs, and it works: the mode is applied once at boot; see §0 for the prerequisite |
| A driver that refuses a mode change after the compositor has started | §7.5 — let weston choose the mode as its initial modeset. Works, and stays optional: where §7.1 works there is no reason to switch |
| Desktop image | The desktop's own display settings (§4); the mode chosen in GNOME or KDE persists there |
| Nothing can set the mode *and* the panel is still too dense, or you do not want to change the mode | `ui.scale` (Settings → Appearance → Interface scale) — §7.3a. It grows the page inside the same framebuffer: text stays soft, but it cannot break the display |
| Nothing works: the panel or driver refuses every mode | §7.2 only with physical or serial access, and expect to revert |

Whatever the route: satisfy §0 first. A desktop session holding the DRM master defeats
all of them, and it is the reason this table used to read as if the routes themselves
were broken.

**Changing the display mode is the first thing to try**, and it is what §7.1, §7.2, §7.5
and §8 all do: it makes the framebuffer smaller instead of magnifying a larger one, so
text is rendered crisply and video costs what it should. When the panel cannot change
mode, or you would rather keep its native resolution, `ui.scale` is the supported answer
rather than a last resort — §7.3a.

### 7.3a `ui.scale`: making the interface bigger without touching the mode

**Settings → Appearance → Interface scale.** Five steps, as a percentage of the base
size: **85 %, 100 % (the default), 115 %, 130 %, 150 %**. A step is a percent integer in
the settings registry (`ui.scale`), so it survives export/import and cannot be set to a
size the UI has no step for.

What it does:

- It multiplies the size of the **whole page**: text, tap targets, the D28 control rail,
  sheets, the settings screens and the dashboard header. It is one multiplier, applied
  once, as a root `zoom` — not a stack of independent ones.
- It composes with the dashboard's container-query layout (design 05 §2.1): a tile's
  internals are a fraction of that tile's own box, so they keep their relationship to the
  tile at every step. The step changes the *base* the layout works from; it does not
  scale a tile's text a second time.
- Tap targets stay at least 48 px **as rendered** at every step, including 85 %. The type
  and tap floors are floors on the rendered size, not on the declared size.
- It applies on load before the first paint (the same pre-paint cache the theme uses), so
  a stored scale does not flash an unscaled panel on the way in.

What it does **not** do:

- It does not touch the **system cursor**, the compositor's own chrome, the console, or
  video resolution. Those belong to the compositor (and to the mode), not to the app; a
  cursor that is too small at 4K is a compositor setting.
- It does not make text crisper. A larger page in the same framebuffer means the glyphs
  are magnified, not re-rasterised, which is why §7.3 puts the mode change first.
- It does not re-render video or change what a stream costs.

Practical limits, measured at 1280×720 with six tiles (the reference panel): 115 % raises
the chrome with almost no cost to the tiles; 130 % makes the dashboard header wrap to a
second row, and 150 % costs the tiles enough height that the tile text gets *smaller* than
at 100 % — the tiles are already laid out to fill the panel, so a bigger interface has to
take that space from somewhere. On a panel that is physically small but high resolution
(1920×1080 and up), every step raises everything with the tiles untouched, which is what
this setting is for. Scale up on a panel that is already tight and it is the tiles that
pay.

### 7.4 Forward compatibility: a future `kiosk.mode` setting

> **Design intent, not a shipped feature.** Nothing in this section exists yet.

Nothing above is specific to a unit file. A `kiosk.mode` setting in the settings UI would
drive exactly the same sequence: the app writes the desired mode into its config, and a
supervisor applies it once, verifies it, and falls back on refusal. One mechanism, two
front ends — the environment variable and `--mode` today, the settings UI later.

The caveat is why the web process should not shell out to `wlr-randr` itself. To talk to
a compositor the caller needs that compositor's Wayland socket: `WAYLAND_DISPLAY`
resolved inside `XDG_RUNTIME_DIR` (`/run/user/<uid>`), and the same user that owns the
session. A service-managed web process has neither by nature, and the socket number is
not stable across restarts — measured on the reference board, `wayland-1` before a kiosk
restart and `wayland-0` after — so a hard-coded name would be wrong half the time.

The safe shape is therefore: the app records intent, a small supervisor inside the
session applies it once, verifies, and leaves the mode alone if it is refused. Never a
retry loop, and never the web tier driving the display directly. None of this is
implemented today, deliberately: it is recorded here so that when the setting arrives
the mechanism is not reinvented with a retry loop.

The weston path (§7.5) is the mechanism this section would end up driving, and it shapes
the supervisor action: weston reads `[output] mode=` only at startup, so applying a new
`kiosk.mode` means **rewriting the compositor configuration and restarting the
compositor** — a supervisor action, not a runtime adjustment. Restarting the compositor
restarts the browser with it, so the panel is dark for the length of a start; treat it
like any other kiosk restart, and the wrapper's display observation (§7.6) then reports
whether the panel came back.

Two things the web process must not do, for the reasons above and for their own:

- It must not shell out to `wlr-randr`. It has no Wayland socket of its own, the socket
  name is not stable across restarts, and on the weston path `wlr-randr` could not talk
  to the compositor even with one: weston is not wlroots and does not implement
  `zwlr_output_management_v1`.
- It must not rewrite the generated ini and restart the unit from inside the app. That
  file lives in a private `mktemp -d` directory owned by the wrapper and is removed on
  exit, and an app that restarts the service serving it can loop.

So the shape stays: the app records intent, a supervisor inside the session rewrites the
compositor configuration and restarts the compositor once, and the wrapper observes the
result (§7.6). Never a retry loop, and never the web tier driving the display directly.

For a panel whose modes genuinely cannot be changed, the app-level `ui.scale` setting of
§7.3a remains the zero-risk alternative from §7.3, because it cannot break the display.

### 7.5 Making the compositor choose the mode at startup (weston)

§7.1 changes the mode after the compositor has started. weston instead reads its output
configuration at startup and sets the mode as part of the initial modeset, so a start
needs no later mode change at all. That is worth having for a driver which refuses a
mode change once the connector has been modeset. It works, and it stays optional: where
§7.1 works there is no reason to switch, because cage is smaller, already installed, and
unchanged by any of this.

```bash
sudo apt install -y weston        # 14.0.2 on the reference board; kiosk-shell.so ships with it
```

Select it in a unit drop-in, which is what a service uses:

```
Environment=KIOSK_COMPOSITOR=weston
```

or on the command line, for a trial by hand:

```bash
chime-deck-kiosk --compositor weston --mode 1280x720
```

The environment variable is the one that matters: it supersedes the hardcoded `--cage` in
the unit's `ExecStart`, so the unit needs no edit beyond one `Environment=` line. Add it
with `systemctl --user edit chime-deck-kiosk.service`, then reload and restart as in
§7.1.

The wrapper then writes a private config file — a `mktemp -d` directory under `/tmp`,
removed on exit — containing exactly this:

```
[core]
idle-time=0
require-input=false
xwayland=false

[output]
name=HDMI-A-1
mode=1280x720
```

`idle-time=0` stops weston fading the panel to black after 300 s with no pointer to wake
it. `require-input=false` lets it start on a panel with no input device attached.
`xwayland=false` is explicit because weston 14's code default and its man page disagree.
The `mode=` line is omitted entirely when `KIOSK_MODE` is empty, which is the same start
with one line fewer: weston uses the display's own (EDID) mode.

The argv is:

```
weston --backend=drm --config=/tmp/chime-deck-weston.XXXXXX/weston.ini \
  --shell=kiosk-shell.so -- chromium --kiosk --app=http://127.0.0.1:8080/?kiosk=1 ...
```

the Chromium flags being the built-in list from §5, unchanged. `--backend=drm` is
explicit so a leaked `DISPLAY` cannot quietly select weston's x11 backend.

Three behaviours to know before relying on it:

- **The `.so` suffix is required.** In weston 14 a bare `--shell=kiosk-shell` becomes
  `kiosk-shell-shell.so` and weston refuses to start. The wrapper always passes
  `--shell=kiosk-shell.so`.
- **A wrong connector degrades, it does not break.** The connector is `KIOSK_OUTPUT`, or
  `HDMI-A-1` when that is unset. A `[output]` section whose `name=` matches no connector
  is silently ignored by weston, so the display falls back to its EDID mode instead of
  going dark.
- **A mode weston cannot find falls back to the preferred mode**, silently. A typo in
  `KIOSK_MODE` therefore costs the size you wanted, not the panel.

To find the real connector name on an unfamiliar board, let the compositor say it:

```bash
grep "DRM: head '" journalctl --user -u chime-deck-kiosk -b --no-pager
```

weston names each head it finds; the quoted name is the one for `KIOSK_OUTPUT`.

A successful start leaves lines like these in the journal, in this order:

```
generated weston.ini: [core];idle-time=0;require-input=false;xwayland=false;[output];name=HDMI-A-1;mode=1280x720;
connector HDMI-A-1 is connected
weston will modeset HDMI-A-1 to 1280x720 as its initial mode (config: /tmp/chime-deck-weston.XXXXXX/weston.ini)
supervising: weston --backend=drm --config=/tmp/chime-deck-weston.XXXXXX/weston.ini --shell=kiosk-shell.so -- chromium ...
kiosk ready: displaying 1280x720 on HDMI-A-1 (enabled, dpms On)
```

On the reference panel the ready line does not name a mode, because that kernel never
populates the sysfs `mode` attribute; it reads
`kiosk ready: displaying on HDMI-A-1 (enabled, dpms On; mode not reported by this
kernel)`. §7.6 explains why the observation is written that way.

A start that had to wait prints `connector HDMI-A-1 is connected (waited 3s)` instead.
With no `KIOSK_MODE` the modeset line reads `weston will use the display's own (EDID)
mode (config: ...)`, and the ready line names whatever mode the panel chose.

Verify the mode **without disturbing the box** — this is a file read, not a mode change:

```bash
cat /sys/class/drm/card0-HDMI-A-1/enabled     # enabled
cat /sys/class/drm/card0-HDMI-A-1/dpms        # On
```

`enabled` with `dpms` `On` is the kernel saying the output is driving a mode. It is the
check that works on this board's kernel, which never populates `mode` — an empty
`mode` file there proves nothing, and the older version of this page that read `mode` as
proof was wrong. Neither check needs a Wayland socket or `wlr-randr`: the kernel state
is the same under either compositor, which is why the wrapper's own observation uses it
first (§7.6).

If weston is absent, or its config cannot be written, the wrapper logs one warning and
falls back to cage at the display's own mode:

```
warning: KIOSK_COMPOSITOR=weston but weston is not installed (apt install weston); falling back to cage at the display's own (EDID) mode (KIOSK_MODE not applied here: set KIOSK_COMPOSITOR=cage to use the wlr-randr path instead)
```

`KIOSK_MODE` is deliberately dropped on that path. Falling back to cage and then
applying the mode would mix two mechanisms inside one start, and the fallback is meant
to be the boring path: cage at the EDID mode is an ugly panel, which is a great deal
better than no panel.

One more thing the journal can mislead about: weston exits when its client exits, and
**its exit status is not Chromium's**. A `browser exited (status=0, ran 3600s);
restarting in 1s` line therefore does not mean Chromium was healthy — read the browser's
own output, not the wrapper's summary.

**Rollback** is one kiosk restart and no reboot:

```bash
systemctl --user edit chime-deck-kiosk.service   # KIOSK_COMPOSITOR=cage, or delete the drop-in
systemctl --user daemon-reload
systemctl --user restart chime-deck-kiosk
```

No file is restored and nothing is undone by hand, because nothing on the weston path
touches `/boot` or the kernel command line: it is a wrapper flag, a temporary config file
and one compositor process. That is the whole difference from §7.2, whose rollback needs
a backup copy of `/boot/armbianEnv.txt` and a reboot.

### 7.6 What the wrapper observes at boot, and why it never acts on it

A supervisor loop that launches a compositor and notices only when the process exits
cannot tell a working panel from a dark one: a compositor painting nothing still runs.
So the wrapper looks. What it never does is let that look decide anything.

Three stages, in order, once per wrapper run, and only when the wrapper itself launches
a compositor (cage or weston — not on the Desktop image, where the desktop owns the
display and the browser is just a client on it):

1. **Advisory DRM-ownership check.** Before the connector wait, the wrapper looks for
   anything other than itself or `systemd-logind` holding
   `$KIOSK_DRM_DEVICE_ROOT/card[0-9]*`. If something does, it logs one warning naming
   the holder:

   ```
   warning: another display server is using /dev/dri/card0 (kwin_wayland pid 2835); the kiosk compositor cannot drive the display while it runs -- disable the desktop session and boot to multi-user.target (docs/kiosk.md section 0)
   ```

   It never refuses to start. The browser may legitimately be started inside a desktop
   session (§4), so this is a warning and nothing more. §0 is what it is warning you
   about.
2. **Wait for the connector.** Poll `$KIOSK_DRM_ROOT/card*-<connector>/status` for
   `connected` before launching anything, so the compositor's first modeset is not asked
   of a connector the kernel has not finished bringing up. The mechanism is unchanged;
   the default is now 60 s because this board's cold boot is contended.
3. **Launch, then observe once.** Start the compositor and the browser, then read the
   connector for up to `KIOSK_READY_WAIT` (default 60 s) and log what the kernel actually
   reports.

That observation has three sources, strongest available first:

| Order | What is read | When it settles the question |
| --- | --- | --- |
| 1 | sysfs `enabled` and `dpms` | `enabled` with `dpms` `On`, or `dpms` absent, means displaying; `enabled=disabled` or `dpms=Off` means the output says it is off. **This is the tier that works on the reference panel**, whose kernel never populates `mode` |
| 2 | `wlr-randr` reporting a `(current)` mode | Only when the probe itself can reach the compositor's Wayland socket (`WAYLAND_DISPLAY` set), and only on the cage path: weston is not wlroots, so `zwlr_output_management_v1` does not exist there |
| 3 | a non-empty sysfs `mode` | For kernels that populate it |

Absence of evidence is not failure: an attribute that is empty or unreadable is never
treated as "no display".

| Setting | Meaning | Default |
| --- | --- | --- |
| `KIOSK_CONNECT_WAIT` | Seconds to wait for `status=connected` before launching. `0` skips the wait | `60` |
| `KIOSK_READY_WAIT` | Seconds after launch to observe the display. `0` skips the observation | `60` |
| `KIOSK_DRM_DEVICE_ROOT` | Device directory the DRM-ownership check scans (tests point this elsewhere) | `/dev/dri` |
| `KIOSK_DRM_ROOT` | sysfs DRM class directory the connector probes read (tests and other kernels point this elsewhere) | `/sys/class/drm` |

Success is one line, quoting what was actually seen:

```
kiosk ready: displaying 1280x720 on HDMI-A-1 (enabled, dpms On)
```

and on the reference panel, whose kernel never reports `mode`, the same start reads:

```
kiosk ready: displaying on HDMI-A-1 (enabled, dpms On; mode not reported by this kernel)
```

Not seeing a display is also one line, a warning, and nothing else happens:

```
warning: HDMI-A-1 reports itself off (enabled=disabled dpms=On mode=none) after 60s; leaving the compositor running (docs/kiosk.md section 0 covers the usual cause)
warning: could not confirm the display came up (no /sys/class/drm/card*-HDMI-A-1 in sysfs) within 60s; leaving the compositor running
```

The first of those means the output itself says it is switched off; the parenthesised
part is what was read. The second means there was nothing readable to judge by — a
connector that is not in sysfs at all, or attributes that stay empty.

**The wrapper never acts on a display verdict.** It does not tear the compositor down,
it does not exit, and it does not restart the browser over an observation. Only a
compositor that has actually **exited** is restarted, by the same supervised loop it has
always used, with the `KIOSK_RESTART_DELAY`/`KIOSK_MAX_RESTART_DELAY` backoff (§5). So
`active` means "the wrapper is running and has said what it sees", not "the panel is
proven lit"; the `kiosk ready:` line is the panel check, and §10 says what to do with the
other two lines.

**Why report-only.** An earlier revision of this wrapper enforced the observation: it
tore the compositor down when the connector did not agree it was displaying, retried and
then exited non-zero. On this board that happened to a working panel — the desktop
session was holding the DRM master (§0), so the check failed while the kiosk was fine,
and the enforcement turned a dark-looking start into a dead one. A diagnostic must not
be able to switch the display off, so this one cannot.

**A healthy board pays none of the 60 s.** The first observation after launch usually
succeeds immediately; the bound is only how long an unusual cold boot is allowed to take
before the wrapper says something about it. Measured on the reference board after §0: a
cold boot logs the ready line within the first seconds of the compositor running.

**Why sysfs first, and `wlr-randr` only sometimes.** `wlr-randr` speaks
`zwlr_output_management_v1`, which weston does not implement, because weston is not a
wlroots compositor; a `wlr-randr` probe would work under cage and fail under weston. It
also needs the compositor's own Wayland socket, which a probe started by the wrapper does
not have unless `WAYLAND_DISPLAY` is set, and the socket number is not stable across
restarts (measured on the reference board: `wayland-1` before a kiosk restart,
`wayland-0` after, §7.1). The sysfs attributes are identical for both compositors, need
no socket, and are the state this kernel actually reports — on the reference panel
`enabled` and `dpms` describe the display while `mode` stays empty.

**`KIOSK_MODE` is irrelevant to whether the kiosk starts.** The observation never
compares the driven mode with `KIOSK_MODE`, and no start is repeated because the mode is
wrong. With `KIOSK_MODE` empty the panel comes up on every boot at its EDID mode, and
that is the acceptance bar here: the mode is a nicety, the dashboard is the point.

**What the observer does not prove.** It reports the connector's own account of itself,
not a picture. A cable in the wrong socket, a panel switched to another input, or a
browser showing an error page all still read as ready. Look at the screen.

## 8. Raspberry Pi HDMI and GPU notes

**Pi only.** These do not apply to the Radxa ZERO 3W / Armbian reference board.
On Raspberry Pi OS bookworm and later the file is `/boot/firmware/config.txt`
(older releases: `/boot/config.txt`). Reboot after editing.

```
hdmi_force_hotplug=1
hdmi_group=1
hdmi_mode=16
hdmi_drive=2
```

- `hdmi_force_hotplug=1` — assume a display is present even at boot.
- `hdmi_group=1` with `hdmi_mode=16` — 1080p60 from the legacy firmware table.
- `hdmi_drive=2` — normal HDMI mode, needed for audio.

With `vc4-kms-v3d` (the KMS driver) those legacy modes come from the display's
EDID, or from the kernel command line: `video=HDMI-A-1:1920x1080@60`.

## 9. Verify, and the two-hour test

Basic checks on the panel:

```bash
systemctl --user is-active chime-deck-kiosk
pgrep -af 'chromium.*--kiosk'
curl -fsS -o /dev/null http://127.0.0.1:8080/?kiosk=1 && echo reachable
```

The acceptance criterion is a two-hour run with no address bar, no
session-crash dialog and no screensaver. That is an **operator run**, not
something CI does. A bounded repeatable version:

```bash
# Run on the panel, from a desktop terminal or over ssh.
for i in $(seq 1 120); do
  printf '%s active=%s chromium=%s\n' \
    "$(date -u +%H:%M:%S)" \
    "$(systemctl --user is-active chime-deck-kiosk)" \
    "$(pgrep -fc chromium)"
  sleep 60
done
journalctl --user -u chime-deck-kiosk --since '2 hours ago' --no-pager \
  | grep 'browser exited' || echo 'no wrapper restarts'
```

The `chromium` count must stay stable and the journal must show no restarts.
Look at the screen during the run: no address bar, no "restore pages" bubble and
no screensaver.

## 10. Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Black screen | Display sleep or DPMS, or no HDMI hotplug. See §4 and §8. |
| Error page in the browser | Wrong URL, backend down, or too short `KIOSK_WAIT_FOR_URL`. Check `KIOSK_URL`, then `curl` the URL from the panel. |
| "Restore pages?" bubble | It should not appear: the wrapper passes `--disable-session-crashed-bubble` and a dedicated profile. Check that the unit runs `deploy/kiosk.sh`, not a bare Chromium. |
| cage: `Atomic commit failed: Permission denied` | Another compositor owns the DRM master — typically a Desktop image's session. cage is for the Lite image; disable the desktop session and boot to `multi-user.target` (§0). Measured on the reference ARM board with Plasma running. |
| Panel still at 1920x1080 after setting `KIOSK_MODE` | Check the journal for `will set the display mode inside cage`, then for `was not reported enabled` (no output came up within `KIOSK_MODE_WAIT`), `does not offer` / `could not set`, or `not installed` — see §7.1. A mode the panel does not advertise is refused; list what it does accept with `wlr-randr`, and raise `KIOSK_MODE_WAIT` on a slow panel. |
| cage: swapchain errors on panfrost | Observed on the ARM board. Retry with `WLR_RENDERER=pixman` (uncomment it in the unit). |
| Black screen with cage + `WLR_RENDERER=pixman` | Chromium's GPU path can present buffers the pixman compositor cannot sample. Pass software compositing through the wrapper: `chime-deck-kiosk --cage -- --disable-gpu` (or add `--disable-gpu` to the unit's `ExecStart`). Measured on the reference board: this is what made the window paint. |
| Unit inactive after reboot | Lingering is off (`loginctl enable-linger`) or no seat (see §5 and §6). |
| `warning: another display server is using /dev/dri/card0 (kwin_wayland pid 2835) ...` | A desktop session still holds the display, so the kiosk compositor cannot modeset. Do §0: `sudo systemctl set-default multi-user.target`, disable the display manager, cold boot. Confirm with `fuser -v /dev/dri/card0`. If you started the browser inside a desktop on purpose, this warning is expected and correct. |
| `warning: HDMI-A-1 reports itself off (enabled=disabled dpms=On mode=none) after 60s; leaving the compositor running` | The output says it is switched off. Almost always the DRM-ownership case in §0: check `fuser -v /dev/dri/card0`. Nothing was torn down, so the kiosk is still there to look at. |
| `warning: could not confirm the display came up (...) within 60s; leaving the compositor running` | Nothing readable to judge by: no `card*-<connector>` entry in sysfs, or attributes that stay empty. Check `ls /sys/class/drm/` and that `KIOSK_OUTPUT` names a connector that exists. This is absence of evidence, not evidence of absence — the compositor was left running. |
| Chromium sandbox error | Do not add `NoNewPrivileges=` or `--no-sandbox`; run the wrapper as shipped. |
| Unit `active` but the panel is dark | `active` does not mean "the panel is lit": the wrapper reports what it sees and keeps running. `journalctl --user -u chime-deck-kiosk -b --no-pager`, then read its lines: `kiosk ready:` (the wrapper saw a displaying output, so suspect the panel, the cable or screen blanking — §4, §8), `reports itself off` (the output says it is off — §0), `could not confirm` (nothing readable — check `ls /sys/class/drm/`), or `another display server is using` (§0). See §7.6. |
| weston starts but nothing ever appears on the panel | Check that the shell module loaded: a bare `--shell=kiosk-shell` makes weston 14 look for `kiosk-shell-shell.so` and refuse to start. Confirm the wrapper passes `--shell=kiosk-shell.so` (the `supervising:` line in the journal shows the exact argv) and that `kiosk-shell.so` exists in your weston package. See §7.5. |
