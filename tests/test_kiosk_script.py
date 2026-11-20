"""The kiosk wrapper's cold-boot-safe display logic, driven as a real process.

Tier: **process-level integration**, fully offline. Each test spawns the real
``deploy/kiosk.sh`` under ``bash`` with a fake ``PATH`` (``cage``, ``weston``,
``chromium``, ``wlr-randr``) and a fake sysfs DRM root, and asserts on four
pieces of evidence: the fake ``wlr-randr`` invocation log, the fake
compositor's launch log (one line per launch, so restart counts are provable),
the fake weston's ``weston.ini`` snapshot, and the wrapper's own stderr.
"It did not retry", "it did not loop" and "it waited the bound" are therefore
proven by call counts and wall-clock bounds, not by reading the shell code and
hoping.

The fake sysfs root (``$KIOSK_DRM_ROOT``, a per-test directory) is written by
the fake compositor stubs. That is the only way to model the fact the wrapper
keys on: ``status``/``enabled``/``dpms``/``mode`` under ``card*-<connector>``.
The fake *device* root (``$KIOSK_DRM_DEVICE_ROOT``, also per-test) is what the
DRM-ownership scan looks at, so no test ever touches the real ``/dev/dri``.

The wrapper is report-only: no test here may assert a teardown, an ``attempt
N/M`` line or a non-zero exit caused by a display verdict. A compositor that
exits is restarted; everything else is a log line.

What this deliberately does NOT prove:

* a real cold boot against a real DRM driver. The boot race is modelled by
  files in a temp directory (and by an empty ``wlr-randr`` listing), not by an
  actual connector coming up; only the owner's reboot cycles on the panel can
  prove the real thing.
* anything about real ``weston``: the stub only honours ``--config=``, models
  the modeset by writing the fake sysfs files, and ``exec``s the client after
  ``--`` the way weston's autolaunch does. If the real weston rejects the
  generated ini, or refuses ``--shell=kiosk-shell.so``, this file cannot see it.
* that the embedded ``sh -c`` snippet runs under **dash**. The panel's
  ``/bin/sh`` is dash; this dev host has neither dash nor busybox (``/bin/sh``
  is bash), so strict-POSIX-ness is guarded statically in
  :func:`test_mode_snippet_is_strict_posix` instead of by execution.
* anything about ``cage``, ``weston``, Chromium or ``wlr-randr`` themselves:
  all four are shell stubs a few lines long.

Owning ticket: HSD-r47e.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
_KIOSK_SH: Final = _REPO_ROOT / "deploy" / "kiosk.sh"
_BASH: Final = shutil.which("bash") or "/bin/bash"

_MODE_HEREDOC_END: Final = "KIOSK_MODE_SCRIPT"
_EVIDENCE_TIMEOUT_S: Final = 15.0
_EXIT_TIMEOUT_S: Final = 10.0
_POLL_INTERVAL_S: Final = 0.02

# Long enough that a second supervised iteration cannot happen inside a test.
_RESTART_DELAY_S: Final = 600
_REQUESTED_MODE: Final = "1280x720"
_OUTPUT_1: Final = "HDMI-A-1"
_OUTPUT_2: Final = "HDMI-A-2"
_APP_URL: Final = "http://127.0.0.1:9/?kiosk=1"

# What the fake compositor writes into the fake sysfs ``mode`` file once it has
# modeset its output.
_READY_MODE: Final = "1920x1080"

# The display half of a fake compositor. `cage` and `weston` share it: what the
# wrapper observes after a launch is sysfs, not who wrote it.
_FAKE_READINESS_BLOCK: Final = """\
if [ -n "${FAKE_COMPOSITOR_EXIT_AFTER:-}" ]; then
  # A compositor that dies during the observation window: the supervisor must
  # report the exit and restart it, never a display verdict.
  sleep "$FAKE_COMPOSITOR_EXIT_AFTER"
  exit 1
fi
if [ -n "${FAKE_COMPOSITOR_READY_AFTER:-}" ]; then
  sleep "$FAKE_COMPOSITOR_READY_AFTER"
  drm_dir=${FAKE_DRM_DIR:?}/card0-${FAKE_CONNECTOR:?}
  mkdir -p "$drm_dir"
  printf 'connected\\n' > "$drm_dir/status"
  printf 'enabled\\n' > "$drm_dir/enabled"
  printf '%s\\n' "${FAKE_COMPOSITOR_DPMS:-On}" > "$drm_dir/dpms"
  printf '%s\\n' "${FAKE_COMPOSITOR_MODE:-1920x1080}" > "$drm_dir/mode"
fi
"""

# One log line per launch, timestamped (so a test can prove the retry delay from
# the wrapper's own timeline) with embedded newlines flattened (the cage mode
# path passes a multi-line `sh -c` script).
_FAKE_LAUNCH_LOG: Final = """\
stamp=$(date +%s.%N)
log_line=$(printf '%s\\n' "$*" | tr '\\n' ' ')
printf '%s %s\\n' "$stamp" "$log_line" >> "${FAKE_COMPOSITOR_LOG:?}"
"""

_FAKE_CAGE: Final = (
    """#!/bin/sh
# Fake `cage`: records every launch, models the modeset in the fake sysfs root,
# then forwards everything after `--` to the one client it supervises.
"""
    + _FAKE_LAUNCH_LOG
    + _FAKE_READINESS_BLOCK
    + """\
shift
exec "$@"
"""
)

_FAKE_WESTON: Final = (
    """#!/bin/sh
# Fake `weston`: records the launch, snapshots the `--config=` file it was
# handed, models the modeset in the fake sysfs root, then `exec`s the client
# found after `--` the way weston's autolaunch does.
"""
    + _FAKE_LAUNCH_LOG
    + """\
for arg in "$@"; do
  case $arg in
    --config=*)
      config=${arg#--config=}
      printf 'CONFIG %s\\n' "$config" >> "${FAKE_WESTON_LOG:?}"
      if [ -n "${FAKE_WESTON_INI_COPY:-}" ]; then
        cp "$config" "$FAKE_WESTON_INI_COPY"
      fi
      ;;
  esac
done
"""
    + _FAKE_READINESS_BLOCK
    + """\
while [ "$#" -gt 0 ] && [ "$1" != '--' ]; do shift; done
if [ "$#" -gt 0 ]; then shift; fi
exec "$@"
"""
)

_FAKE_BROWSER: Final = """#!/bin/sh
# Records its argv, then sleeps so the wrapper stays inside one supervised run.
# ``FAKE_BROWSER_EXIT_AFTER`` models a client that dies, which ends the
# supervised run (a real cage exits with its only client).
printf '%s\\n' "$*" >> "$FAKE_BROWSER_LOG"
if [ -n "${FAKE_BROWSER_EXIT_AFTER:-}" ]; then
  sleep "$FAKE_BROWSER_EXIT_AFTER"
  exit 0
fi
exec sleep 30
"""

_FAKE_WLR_RANDR: Final = """#!/bin/sh
# Fake `wlr-randr` 0.4.1: renders a canned listing from the environment, keeps a
# current-mode state file, and appends every invocation to $FAKE_WLR_LOG.
log=${FAKE_WLR_LOG:?}
state=${FAKE_WLR_STATE:?}

if [ "$#" -eq 0 ]; then
  printf 'LIST\\n' >> "$log"
else
  printf '%s\\n' "$*" >> "$log"
fi

if [ ! -f "$state" ]; then
  printf '%s' "${FAKE_WLR_DEFAULT_CURRENT:-1920x1080}" > "$state"
fi
current=$(cat "$state")

if [ "$#" -gt 0 ]; then
  if [ "${FAKE_WLR_APPLY_FAIL:-0}" = 1 ]; then
    printf 'wlr-randr: connector %s: Atomic commit failed: Invalid argument\\n' "$2" >&2
    exit 1
  fi
  printf '%s' "$4" > "$state"
  exit 0
fi

seen=$(grep -c '^LIST$' "$log")
if [ "$seen" -le "${FAKE_WLR_EMPTY_FIRST:-0}" ]; then
  exit 0
fi

render() {
  name=$1
  enabled=$2
  printf '%s "Fake panel %s"\\n' "$name" "$name"
  printf '  Physical size: 800x450 mm\\n'
  printf '  Enabled: %s\\n' "$enabled"
  if [ "$enabled" != yes ]; then
    return 0
  fi
  printf '  Modes:\\n'
  for mode in $3; do
    if [ "$mode" = "$current" ]; then
      printf '    %s px, 60.000000 Hz (current)\\n' "$mode"
    else
      printf '    %s px, 60.000000 Hz\\n' "$mode"
    fi
  done
}

render "${FAKE_WLR_OUT_1_NAME:-HDMI-A-1}" "${FAKE_WLR_OUT_1_ENABLED:-yes}" \\
  "${FAKE_WLR_OUT_1_MODES:-1920x1080}"
if [ -n "${FAKE_WLR_OUT_2_NAME:-}" ]; then
  render "${FAKE_WLR_OUT_2_NAME}" "${FAKE_WLR_OUT_2_ENABLED:-yes}" \\
    "${FAKE_WLR_OUT_2_MODES:-1920x1080}"
fi
exit 0
"""


def _read_if_exists(path: Path) -> str:
    """Return a file's text, or the empty string when it was never created.

    :param path: file to read.
    """
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_executable(path: Path, body: str) -> None:
    """Write a stub script and make it executable.

    :param path: script path inside the fake ``PATH`` directory.
    :param body: full script text, including the ``#!/bin/sh`` line.
    """
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _lines_with(text: str, needle: str) -> list[str]:
    """Return the lines of ``text`` that contain ``needle``.

    :param text: captured process output.
    :param needle: substring to look for.
    """
    return [line for line in text.splitlines() if needle in line]


@dataclass(frozen=True)
class KioskRun:
    """One supervised run of the wrapper and the files it produced."""

    process: subprocess.Popen[bytes]
    started_at: float
    browser_log: Path
    wlr_log: Path
    compositor_log: Path
    weston_log: Path
    weston_ini_copy: Path
    stderr_path: Path
    stdout_path: Path

    def stderr_text(self) -> str:
        """Return everything the wrapper and its children wrote to stderr."""
        return _read_if_exists(self.stderr_path)

    def stdout_text(self) -> str:
        """Return everything the wrapper and its children wrote to stdout."""
        return _read_if_exists(self.stdout_path)

    def browser_lines(self) -> list[str]:
        """Return the fake browser's recorded argv lines (one per exec)."""
        return [line for line in _read_if_exists(self.browser_log).splitlines() if line]

    def wlr_calls(self) -> list[str]:
        """Return the fake ``wlr-randr`` invocation log, in call order."""
        return [line for line in _read_if_exists(self.wlr_log).splitlines() if line]

    def mode_calls(self) -> list[str]:
        """Return only the ``--output ... --mode ...`` calls (the atomic commits)."""
        return [call for call in self.wlr_calls() if "--mode" in call]

    def listing_count(self) -> int:
        """Return how many times the fake was invoked with no arguments."""
        return sum(1 for call in self.wlr_calls() if call == "LIST")

    def compositor_launches(self) -> list[str]:
        """Return one argv line per compositor launch, in launch order.

        The fake ``cage`` and ``weston`` append exactly one line per exec, so
        ``len(...)`` is the number of start attempts the wrapper made.
        """
        return [argv for _, argv in self._launch_records()]

    def launch_times(self) -> list[float]:
        """Return the wall-clock time of each compositor launch.

        The fake compositors stamp their launch line, so the gap between two
        launches measures the wrapper's own retry delay rather than the test's.
        """
        return [stamp for stamp, _ in self._launch_records()]

    def _launch_records(self) -> list[tuple[float, str]]:
        """Return ``(timestamp, argv)`` for every compositor launch."""
        records: list[tuple[float, str]] = []
        for line in _read_if_exists(self.compositor_log).splitlines():
            if not line:
                continue
            stamp, _, argv = line.partition(" ")
            records.append((float(stamp), argv))
        return records

    def weston_configs(self) -> list[str]:
        """Return the ``--config=`` paths the fake ``weston`` was handed."""
        prefix = "CONFIG "
        return [
            line[len(prefix) :]
            for line in _read_if_exists(self.weston_log).splitlines()
            if line.startswith(prefix)
        ]

    def weston_ini_text(self) -> str:
        """Return the snapshot of the generated ``weston.ini``, or ``""``."""
        return _read_if_exists(self.weston_ini_copy)

    def exited_within(self, timeout: float = _EXIT_TIMEOUT_S) -> int:
        """Wait, bounded, for the wrapper to exit by itself; return its status.

        No test should expect an exit caused by a display verdict (the wrapper
        is report-only); this stays available for the paths that really do
        exit, and for proving that one does not.

        :param timeout: wall-clock bound on the wait, in seconds.
        """
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"the wrapper did not exit within {timeout}s\n"
                f"--- stderr ---\n{self.stderr_text()}\n"
                f"--- stdout ---\n{self.stdout_text()}"
            )


class KioskHarness:
    """A fake ``PATH`` plus a bounded launcher for the real kiosk wrapper."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        with_wlr_randr: bool = True,
        with_weston: bool = False,
    ) -> None:
        """Create the stub directory and the log/state paths.

        :param tmp_path: per-test directory holding the stubs, logs and profile.
        :param with_wlr_randr: omit the stub to model ``wlr-randr`` not installed.
        :param with_weston: install a fake ``weston`` to model it being installed.
        """
        self.tmp_path = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.browser_log = tmp_path / "browser.log"
        self.wlr_log = tmp_path / "wlr-randr.log"
        self.wlr_state = tmp_path / "wlr-randr.state"
        self.compositor_log = tmp_path / "compositor.log"
        self.weston_log = tmp_path / "weston.log"
        self.weston_ini_copy = tmp_path / "weston.ini.copy"
        # Never the real /sys: the wrapper must be pointed at a directory this
        # harness owns, whether or not a test presets anything in it.
        self.drm_root = tmp_path / "drm"
        # Never the real /dev/dri either: the DRM-ownership scan is pointed here.
        self.drm_device_root = tmp_path / "dri"
        self._runs: list[KioskRun] = []
        self._holders: list[subprocess.Popen[bytes]] = []
        (tmp_path / "home").mkdir()
        _write_executable(self.bin / "cage", _FAKE_CAGE)
        _write_executable(self.bin / "chromium", _FAKE_BROWSER)
        if with_weston:
            _write_executable(self.bin / "weston", _FAKE_WESTON)
        if with_wlr_randr:
            _write_executable(self.bin / "wlr-randr", _FAKE_WLR_RANDR)

    def preset_current_mode(self, mode: str) -> None:
        """Seed the fake compositor's current mode before the wrapper starts.

        :param mode: ``WIDTHxHEIGHT`` the fake reports as current.
        """
        self.wlr_state.write_text(mode, encoding="utf-8")

    def preset_sysfs(
        self,
        *,
        status: str = "connected",
        enabled: str | None = None,
        dpms: str | None = None,
        mode: str | None = None,
        connector: str = _OUTPUT_1,
    ) -> Path:
        """Create the fake sysfs connector directory the wrapper observes.

        :param status: contents of the ``status`` file (``connected`` or not).
        :param enabled: contents of ``enabled``, or ``None`` to leave it absent.
        :param dpms: contents of ``dpms``, or ``None`` to leave it absent (the
            kernel on the reference board reports it, so most tests set it).
        :param mode: contents of ``mode``, or ``None`` to leave it absent (the
            "nothing is modeset" state, and the state the reference kernel
            keeps even while it is driving the panel).
        :param connector: connector name, i.e. the ``card0-<connector>`` suffix.
        """
        return self.preset_connector_dir(
            connector=connector,
            status=status,
            enabled=enabled,
            dpms=dpms,
            mode=mode,
        )

    def preset_connector_dir(
        self,
        *,
        connector: str = _OUTPUT_1,
        status: str | None = None,
        enabled: str | None = None,
        dpms: str | None = None,
        mode: str | None = None,
    ) -> Path:
        """Pre-create the connector directory with only the chosen attributes.

        The reference board's ``card0-HDMI-A-1`` exists from boot, so a test
        can model "the attributes appeared later" by writing only some of them
        (an empty string writes an empty file, ``None`` leaves it absent).

        :param connector: connector name, i.e. the ``card0-<connector>`` suffix.
        :param status: contents of ``status``, or ``None`` to leave it absent.
        :param enabled: contents of ``enabled``, or ``None`` to leave it absent.
        :param dpms: contents of ``dpms``, or ``None`` to leave it absent.
        :param mode: contents of ``mode``, or ``None`` to leave it absent.
        """
        card_dir = self.drm_root / f"card0-{connector}"
        card_dir.mkdir(parents=True, exist_ok=True)
        attributes = {
            "status": status,
            "enabled": enabled,
            "dpms": dpms,
            "mode": mode,
        }
        for name, value in attributes.items():
            if value is not None:
                (card_dir / name).write_text(f"{value}\n", encoding="utf-8")
        return card_dir

    def start_drm_holder(self, *, card: str = "card0") -> subprocess.Popen[bytes]:
        """Create a fake card node and hold it open from a bounded stub process.

        Models another display server (``kwin_wayland``) that owns the DRM
        device. The stub opens the node and sleeps; the harness reaps it.

        :param card: name of the fake DRM node inside the fake device root.
        """
        device = self.drm_device_root / card
        self.drm_device_root.mkdir(parents=True, exist_ok=True)
        device.touch()
        holder = subprocess.Popen(
            [_BASH, "-c", 'exec 8<"$1"; sleep 30', "drm-holder", str(device)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._holders.append(holder)
        return holder

    def start(self, **kiosk_env: str) -> KioskRun:
        """Spawn the wrapper in its own session and return the run handle.

        :param kiosk_env: extra environment entries; unknown keys go straight to
            the child, so ``FAKE_*`` stubs are configured the same way as
            ``KIOSK_*`` settings.
        """
        stdout_path = self.tmp_path / "stdout.log"
        stderr_path = self.tmp_path / "stderr.log"
        started_at = time.monotonic()
        # Files, not pipes: a pipe nobody drains could deadlock the test.
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = subprocess.Popen(
                [_BASH, str(_KIOSK_SH)],
                env=self._env(kiosk_env),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
        run = KioskRun(
            process=process,
            started_at=started_at,
            browser_log=self.browser_log,
            wlr_log=self.wlr_log,
            compositor_log=self.compositor_log,
            weston_log=self.weston_log,
            weston_ini_copy=self.weston_ini_copy,
            stderr_path=stderr_path,
            stdout_path=stdout_path,
        )
        self._runs.append(run)
        return run

    def stop(self) -> None:
        """Terminate every process group and holder this harness started."""
        for run in self._runs:
            _terminate(run.process)
        self._runs.clear()
        for holder in self._holders:
            _reap_helper(holder)
        self._holders.clear()

    def _env(self, extra: Mapping[str, str]) -> dict[str, str]:
        """Return the child environment: no inherited kiosk config, no display.

        The two display waits are off by default so a test that is not about a
        wait never pays for one; the tests that do exercise a wait set the
        bound themselves.

        :param extra: per-test overrides applied last.
        """
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("KIOSK_", "FAKE_"))
        }
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_CACHE_HOME"):
            env.pop(key, None)
        env.update(
            {
                "HOME": str(self.tmp_path / "home"),
                "PATH": os.pathsep.join([str(self.bin), env.get("PATH", "")]),
                "FAKE_BROWSER_LOG": str(self.browser_log),
                "FAKE_WLR_LOG": str(self.wlr_log),
                "FAKE_WLR_STATE": str(self.wlr_state),
                "FAKE_COMPOSITOR_LOG": str(self.compositor_log),
                "FAKE_WESTON_LOG": str(self.weston_log),
                "FAKE_WESTON_INI_COPY": str(self.weston_ini_copy),
                "FAKE_DRM_DIR": str(self.drm_root),
                "FAKE_CONNECTOR": _OUTPUT_1,
                "KIOSK_CAGE": "1",
                "KIOSK_URL": _APP_URL,
                "KIOSK_WAIT_FOR_URL": "0",
                "KIOSK_USER_DATA_DIR": str(self.tmp_path / "profile"),
                "KIOSK_SCREEN_BLANKING": "0",
                "KIOSK_HEALTHY_AFTER": str(_RESTART_DELAY_S),
                "KIOSK_RESTART_DELAY": str(_RESTART_DELAY_S),
                "KIOSK_MAX_RESTART_DELAY": str(_RESTART_DELAY_S),
                "KIOSK_CONNECT_WAIT": "0",
                "KIOSK_READY_WAIT": "0",
                "KIOSK_DRM_ROOT": str(self.drm_root),
                "KIOSK_DRM_DEVICE_ROOT": str(self.drm_device_root),
            }
        )
        env.update(extra)
        return env


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM the run's process group, then SIGKILL it if it is still alive.

    ``start_new_session=True`` makes the child's pid its process-group id, so
    the whole supervised tree (wrapper, ``sh -c`` snippet, ``sleep 30``) goes
    away together and nothing survives the test.

    :param process: the wrapper process spawned by the harness.
    """
    pgid = process.pid
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    try:
        process.wait(timeout=_EXIT_TIMEOUT_S)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    process.wait(timeout=_EXIT_TIMEOUT_S)


def _await_browser(run: KioskRun, *, timeout: float = _EVIDENCE_TIMEOUT_S) -> float:
    """Wait until the fake browser records its argv; return seconds since spawn.

    The wrapper runs the browser only after the display-mode logic has finished,
    so once this returns, the whole ``wlr-randr`` call sequence is on disk.

    :param run: the wrapper run to observe.
    :param timeout: wall-clock bound on the wait, in seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run.browser_lines():
            return time.monotonic() - run.started_at
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"the fake browser was not exec'd within {timeout}s\n"
        f"--- stderr ---\n{run.stderr_text()}\n"
        f"--- stdout ---\n{run.stdout_text()}"
    )


def _await_log(
    run: KioskRun,
    needle: str,
    *,
    timeout: float = _EVIDENCE_TIMEOUT_S,
) -> list[str]:
    """Wait, bounded, until the wrapper logs a line containing ``needle``.

    Needed because the observation is logged *after* exec'ing the browser (and
    after the observation window), so waiting for the browser alone is not
    enough.

    :param run: the wrapper run to observe.
    :param needle: substring the awaited log line must contain.
    :param timeout: wall-clock bound on the wait, in seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = _lines_with(run.stderr_text(), needle)
        if matches:
            return matches
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"the wrapper did not log {needle!r} within {timeout}s\n"
        f"--- stderr ---\n{run.stderr_text()}\n"
        f"--- stdout ---\n{run.stdout_text()}"
    )


def _start_connector_flip(status_file: Path, *, delay: float) -> subprocess.Popen[bytes]:
    """Start a bounded helper that reports the connector connected after ``delay``.

    Models a cold boot where the connector settles a moment after the unit
    starts. It launches, writes one file and exits; it never waits on anything.

    :param status_file: the fake sysfs ``status`` file to rewrite.
    :param delay: seconds to wait before writing, in seconds.
    """
    return subprocess.Popen(
        [
            _BASH,
            "-c",
            'sleep "$1"; printf "connected\\n" > "$2"',
            "connector-flip",
            str(delay),
            str(status_file),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _await_holder(
    holder: subprocess.Popen[bytes],
    device: Path,
    *,
    timeout: float = _EXIT_TIMEOUT_S,
) -> None:
    """Wait, bounded, until ``holder`` has the fake DRM node open.

    The wrapper scans ``/proc/*/fd`` for the node, so the test must not start
    the wrapper until the holder is really holding it.

    :param holder: the stub process that owns the node.
    :param device: the fake card node it opens.
    :param timeout: wall-clock bound on the wait, in seconds.
    """
    fd_dir = Path(f"/proc/{holder.pid}/fd")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for fd in fd_dir.glob("*"):
            with suppress(OSError):
                if Path(os.readlink(fd)) == device:
                    return
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"process {holder.pid} never opened {device}")


def _await_launches(
    run: KioskRun,
    count: int,
    *,
    timeout: float = _EVIDENCE_TIMEOUT_S,
) -> None:
    """Wait, bounded, until the fake compositor records ``count`` launches.

    :param run: the wrapper run to observe.
    :param count: number of compositor launches to wait for.
    :param timeout: wall-clock bound on the wait, in seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(run.compositor_launches()) >= count:
            return
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"the wrapper made {len(run.compositor_launches())} launches, not {count}, "
        f"within {timeout}s\n"
        f"--- stderr ---\n{run.stderr_text()}\n"
        f"--- stdout ---\n{run.stdout_text()}"
    )


def _help_text() -> str:
    """Return the wrapper's ``--help`` output."""
    result = subprocess.run(
        [_BASH, str(_KIOSK_SH), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=_EXIT_TIMEOUT_S,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _documented_default(help_text: str, name: str) -> str:
    """Return the default ``--help`` documents for an environment variable.

    :param help_text: the wrapper's ``--help`` output.
    :param name: environment variable name, e.g. ``KIOSK_READY_WAIT``.
    """
    match = re.search(rf"{name}.*?default ([0-9]+)", help_text, re.DOTALL)
    assert match is not None, f"--help documents no default for {name}"
    return match.group(1)


def _reap_helper(helper: subprocess.Popen[bytes], *, timeout: float = _EXIT_TIMEOUT_S) -> None:
    """Wait, bounded, for a helper to exit, killing it if it overruns.

    :param helper: the process started by :func:`_start_connector_flip`.
    :param timeout: wall-clock bound on the wait, in seconds.
    """
    try:
        helper.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    helper.kill()
    helper.wait(timeout=timeout)


def _supervising_argv(stderr: str) -> str:
    """Return the wrapper's ``supervising:`` argv line with ``%q`` undone.

    :param stderr: the wrapper's captured stderr.
    """
    for line in stderr.splitlines():
        _, separator, rest = line.partition("supervising:")
        if separator:
            return rest.replace("\\ ", " ").strip()
    return ""


def _warnings(stderr: str) -> list[str]:
    """Return the display-mode warning lines the snippet wrote.

    :param stderr: the wrapper's captured stderr.
    """
    return [line for line in stderr.splitlines() if line.startswith("kiosk: warning:")]


def _require_bash() -> None:
    """Skip when the wrapper's interpreter is missing."""
    if shutil.which("bash") is None:
        pytest.skip("deploy/kiosk.sh is a bash script and bash is not installed")


@pytest.fixture
def kiosk(tmp_path: Path) -> Iterator[KioskHarness]:
    """A fake ``PATH`` (including ``wlr-randr``) that is torn down afterwards.

    :param tmp_path: per-test directory for the stubs, logs and profile.
    """
    _require_bash()
    harness = KioskHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.stop()


@pytest.fixture
def kiosk_without_wlr_randr(tmp_path: Path) -> Iterator[KioskHarness]:
    """A fake ``PATH`` without a ``wlr-randr`` stub.

    :param tmp_path: per-test directory for the stubs, logs and profile.
    """
    _require_bash()
    harness = KioskHarness(tmp_path, with_wlr_randr=False)
    try:
        yield harness
    finally:
        harness.stop()


@pytest.fixture
def kiosk_with_weston(tmp_path: Path) -> Iterator[KioskHarness]:
    """A fake ``PATH`` with a fake ``weston`` installed alongside cage.

    :param tmp_path: per-test directory for the stubs, logs and profile.
    """
    _require_bash()
    harness = KioskHarness(tmp_path, with_weston=True)
    try:
        yield harness
    finally:
        harness.stop()


@pytest.fixture
def kiosk_without_weston(tmp_path: Path) -> Iterator[KioskHarness]:
    """A fake ``PATH`` with no ``weston`` stub, to model it not being installed.

    :param tmp_path: per-test directory for the stubs, logs and profile.
    """
    _require_bash()
    harness = KioskHarness(tmp_path, with_weston=False)
    try:
        yield harness
    finally:
        harness.stop()


@pytest.mark.integration
@pytest.mark.slow
def test_ready_output_applies_the_mode_exactly_once(kiosk: KioskHarness) -> None:
    """An enabled output that advertises the mode gets one atomic commit."""
    run = kiosk.start(
        KIOSK_MODE=_REQUESTED_MODE,
        KIOSK_OUTPUT=_OUTPUT_1,
        FAKE_WLR_OUT_1_NAME=_OUTPUT_1,
        FAKE_WLR_OUT_1_MODES="1920x1080 1280x720",
    )
    _await_browser(run)

    assert run.wlr_calls()[0] == "LIST"
    assert run.mode_calls() == [f"--output {_OUTPUT_1} --mode {_REQUESTED_MODE}"]
    assert run.listing_count() == 2, "one readiness probe plus one verification read"
    stderr = run.stderr_text()
    assert f"kiosk: display mode is now {_REQUESTED_MODE} on {_OUTPUT_1}" in stderr
    browser = run.browser_lines()
    assert len(browser) == 1
    assert "--kiosk" in browser[0]
    assert f"--app={_APP_URL}" in browser[0]


@pytest.mark.integration
@pytest.mark.slow
def test_cold_boot_race_waits_then_applies_once(kiosk: KioskHarness) -> None:
    """Three empty listings (the connector is not up yet) come before the commit."""
    run = kiosk.start(
        KIOSK_MODE=_REQUESTED_MODE,
        KIOSK_OUTPUT=_OUTPUT_1,
        FAKE_WLR_EMPTY_FIRST="3",
        FAKE_WLR_OUT_1_NAME=_OUTPUT_1,
        FAKE_WLR_OUT_1_MODES="1920x1080 1280x720",
    )
    _await_browser(run)

    calls = run.wlr_calls()
    assert calls[:3] == ["LIST", "LIST", "LIST"], "the race must be waited out, not assumed away"
    commits = [index for index, call in enumerate(calls) if "--mode" in call]
    assert commits == [4], f"one commit, after the three empty polls: {calls}"
    assert calls[4] == f"--output {_OUTPUT_1} --mode {_REQUESTED_MODE}"
    assert calls[3] == "LIST", "the fourth probe is the first one that saw the output"
    assert run.listing_count() == 5, "four readiness probes plus one verification read"
    assert calls[-1] == "LIST", "the successful commit is verified by a fresh read"
    assert f"kiosk: display mode is now {_REQUESTED_MODE} on {_OUTPUT_1}" in run.stderr_text()
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_output_never_appears_warns_once_and_starts_the_browser(kiosk: KioskHarness) -> None:
    """KIOSK_MODE_WAIT=1 means five polls, no commit, one warning, and a browser."""
    run = kiosk.start(
        KIOSK_MODE=_REQUESTED_MODE,
        KIOSK_OUTPUT=_OUTPUT_1,
        KIOSK_MODE_WAIT="1",
        FAKE_WLR_OUT_1_NAME=_OUTPUT_1,
        FAKE_WLR_OUT_1_ENABLED="no",
    )
    elapsed = _await_browser(run)

    assert run.listing_count() == 5, "polls = wait seconds * 4 + 1 immediate probe"
    assert run.mode_calls() == []
    warnings = [line for line in _warnings(run.stderr_text()) if "was not reported enabled" in line]
    assert len(warnings) == 1
    assert _OUTPUT_1 in warnings[0]
    assert elapsed >= 0.9, f"the wrapper must actually wait the bound, took {elapsed:.3f}s"
    assert len(run.browser_lines()) == 1, "the browser starts anyway"


@pytest.mark.integration
@pytest.mark.slow
def test_mode_not_advertised_warns_with_the_available_list(kiosk: KioskHarness) -> None:
    """A mode the output does not offer is never attempted."""
    run = kiosk.start(
        KIOSK_MODE=_REQUESTED_MODE,
        KIOSK_OUTPUT=_OUTPUT_1,
        FAKE_WLR_OUT_1_NAME=_OUTPUT_1,
        FAKE_WLR_OUT_1_MODES="1920x1080 1024x768",
    )
    _await_browser(run)

    assert run.mode_calls() == []
    warnings = [line for line in _warnings(run.stderr_text()) if "does not offer" in line]
    assert len(warnings) == 1
    assert _REQUESTED_MODE in warnings[0]
    assert "1024x768" in warnings[0]
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_rejected_commit_is_never_retried(kiosk: KioskHarness) -> None:
    """A rejected atomic commit produces exactly one ``--mode`` call, ever."""
    run = kiosk.start(
        KIOSK_MODE=_REQUESTED_MODE,
        KIOSK_OUTPUT=_OUTPUT_1,
        FAKE_WLR_APPLY_FAIL="1",
        FAKE_WLR_OUT_1_NAME=_OUTPUT_1,
        FAKE_WLR_OUT_1_MODES="1920x1080 1280x720",
    )
    _await_browser(run)

    assert run.mode_calls() == [f"--output {_OUTPUT_1} --mode {_REQUESTED_MODE}"]
    assert run.listing_count() == 1, "no re-read after a failed commit"
    stderr = run.stderr_text()
    assert f"could not set {_REQUESTED_MODE} on {_OUTPUT_1}" in stderr
    assert "display mode is now" not in stderr
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skipif(
    shutil.which("wlr-randr") is not None,
    reason="a real wlr-randr is on PATH, so 'not installed' cannot be simulated",
)
def test_missing_wlr_randr_warns_at_wrapper_level(
    kiosk_without_wlr_randr: KioskHarness,
) -> None:
    """Without ``wlr-randr`` the wrapper drops the mode and runs a plain cage argv."""
    run = kiosk_without_wlr_randr.start(KIOSK_MODE=_REQUESTED_MODE)
    _await_browser(run)

    assert not kiosk_without_wlr_randr.wlr_log.exists(), "nothing may be invoked at all"
    assert "wlr-randr is not installed" in run.stderr_text()
    argv = _supervising_argv(run.stderr_text())
    assert argv.startswith("cage -- chromium "), argv
    assert "sh -c" not in argv
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_first_enabled_output_is_derived_when_output_is_unset(kiosk: KioskHarness) -> None:
    """With no KIOSK_OUTPUT, the first *enabled* connector wins (not the first listed)."""
    run = kiosk.start(
        KIOSK_MODE=_REQUESTED_MODE,
        FAKE_WLR_OUT_1_NAME=_OUTPUT_1,
        FAKE_WLR_OUT_1_ENABLED="no",
        FAKE_WLR_OUT_1_MODES="1920x1080 1280x720",
        FAKE_WLR_OUT_2_NAME=_OUTPUT_2,
        FAKE_WLR_OUT_2_ENABLED="yes",
        FAKE_WLR_OUT_2_MODES="1920x1080 1280x720",
    )
    _await_browser(run)

    assert run.mode_calls() == [f"--output {_OUTPUT_2} --mode {_REQUESTED_MODE}"]
    assert f"kiosk: display mode is now {_REQUESTED_MODE} on {_OUTPUT_2}" in run.stderr_text()
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_already_at_the_requested_mode_is_not_touched(kiosk: KioskHarness) -> None:
    """No needless atomic commit when the display is already where we want it."""
    kiosk.preset_current_mode(_REQUESTED_MODE)
    run = kiosk.start(
        KIOSK_MODE=_REQUESTED_MODE,
        KIOSK_OUTPUT=_OUTPUT_1,
        FAKE_WLR_OUT_1_NAME=_OUTPUT_1,
        FAKE_WLR_OUT_1_MODES="1920x1080 1280x720",
    )
    _await_browser(run)

    assert run.mode_calls() == []
    assert run.listing_count() == 1
    stderr = run.stderr_text()
    assert (
        f"kiosk: display mode is already {_REQUESTED_MODE} on {_OUTPUT_1}; not touching it"
        in stderr
    )
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_no_mode_keeps_the_supervised_argv_unchanged(kiosk: KioskHarness) -> None:
    """The no-KIOSK_MODE path is still ``cage -- chromium ...``, with no sh layer."""
    run = kiosk.start()
    _await_browser(run)

    assert not kiosk.wlr_log.exists(), "wlr-randr must not be invoked without a mode"
    stderr = run.stderr_text()
    argv = _supervising_argv(stderr)
    assert argv.startswith("cage -- chromium "), argv
    assert "sh -c" not in argv
    assert "will set the display mode" not in stderr
    browser = run.browser_lines()
    assert len(browser) == 1
    assert "--kiosk" in browser[0]


@pytest.mark.integration
@pytest.mark.slow
def test_explicit_output_beats_the_derived_one(kiosk: KioskHarness) -> None:
    """KIOSK_OUTPUT naming the second connector is honoured, not overridden."""
    run = kiosk.start(
        KIOSK_MODE=_REQUESTED_MODE,
        KIOSK_OUTPUT=_OUTPUT_2,
        FAKE_WLR_OUT_1_NAME=_OUTPUT_1,
        FAKE_WLR_OUT_1_ENABLED="yes",
        FAKE_WLR_OUT_1_MODES="1920x1080 1280x720",
        FAKE_WLR_OUT_2_NAME=_OUTPUT_2,
        FAKE_WLR_OUT_2_ENABLED="yes",
        FAKE_WLR_OUT_2_MODES="1920x1080 1280x720",
    )
    _await_browser(run)

    assert run.mode_calls() == [f"--output {_OUTPUT_2} --mode {_REQUESTED_MODE}"]
    assert f"--output {_OUTPUT_1} --mode" not in "\n".join(run.mode_calls())
    assert f"kiosk: display mode is now {_REQUESTED_MODE} on {_OUTPUT_2}" in run.stderr_text()


# --- Stage 1: the pre-start connector wait -------------------------------


@pytest.mark.integration
@pytest.mark.slow
def test_connector_never_connected_warns_once_then_starts(kiosk: KioskHarness) -> None:
    """A connector that never comes up burns the bound, warns once, starts anyway.

    The floor is 2.8 s of a 3 s bound: the wrapper counts polls rather than
    comparing ``$SECONDS``, which on bash 5.3 flips on wall-clock second
    boundaries and used to expire an N-second bound anywhere in ``[N - 1, N]``.
    """
    kiosk.preset_sysfs(status="disconnected", enabled="disabled")
    run = kiosk.start(
        KIOSK_CONNECT_WAIT="3",
        KIOSK_READY_WAIT="0",
    )
    elapsed = _await_browser(run)

    expired = _lines_with(run.stderr_text(), "did not report connected within")
    assert len(expired) == 1, run.stderr_text()
    assert f"connector {_OUTPUT_1} did not report connected within 3s" in expired[0]
    assert "starting the compositor anyway" in expired[0]
    assert elapsed >= 2.8, f"the wrapper must actually wait the bound, took {elapsed:.3f}s"
    assert elapsed < 6.0, f"the wrapper overshot the bound, took {elapsed:.3f}s"
    assert len(run.browser_lines()) == 1, "the browser starts anyway"


@pytest.mark.integration
@pytest.mark.slow
def test_connector_already_connected_does_not_wait(kiosk: KioskHarness) -> None:
    """An already-connected connector costs one log line and no wait."""
    kiosk.preset_sysfs(status="connected", enabled="disabled")
    run = kiosk.start(
        KIOSK_CONNECT_WAIT="15",
        KIOSK_READY_WAIT="0",
    )
    elapsed = _await_browser(run)

    connected = _lines_with(run.stderr_text(), f"connector {_OUTPUT_1} is connected")
    assert len(connected) == 1, run.stderr_text()
    assert elapsed < 5.0, f"an already-connected connector must not cost 15s, took {elapsed:.3f}s"
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_connector_connecting_after_a_moment_is_not_a_warning(kiosk: KioskHarness) -> None:
    """A connector that reports connected mid-wait is waited for, not warned about."""
    status_file = kiosk.preset_sysfs(status="disconnected", enabled="disabled") / "status"
    helper = _start_connector_flip(status_file, delay=0.4)
    try:
        run = kiosk.start(
            KIOSK_CONNECT_WAIT="15",
            KIOSK_READY_WAIT="0",
        )
        elapsed = _await_browser(run)
    finally:
        _reap_helper(helper)

    stderr = run.stderr_text()
    connected = _lines_with(stderr, f"connector {_OUTPUT_1} is connected")
    assert len(connected) == 1, stderr
    assert _lines_with(stderr, "did not report connected within") == []
    assert elapsed < 5.0, f"the wait must end as soon as the connector reports, took {elapsed:.3f}s"
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_connector_wait_applies_on_the_weston_path(kiosk_with_weston: KioskHarness) -> None:
    """The pre-start wait is compositor-independent: it runs before the launch."""
    kiosk_with_weston.preset_sysfs(status="connected", enabled="disabled")
    run = kiosk_with_weston.start(
        KIOSK_COMPOSITOR="weston",
        KIOSK_CONNECT_WAIT="1",
        KIOSK_READY_WAIT="0",
    )
    elapsed = _await_browser(run)

    stderr = run.stderr_text()
    connected = _lines_with(stderr, f"connector {_OUTPUT_1} is connected")
    assert len(connected) == 1, stderr
    launches = run.compositor_launches()
    assert len(launches) == 1
    assert "--backend=drm" in launches[0]
    ini = run.weston_ini_text()
    assert "[output]" in ini
    assert f"name={_OUTPUT_1}" in ini
    assert elapsed < 5.0


@pytest.mark.integration
@pytest.mark.slow
def test_connector_wait_is_skipped_when_there_is_no_sysfs(kiosk: KioskHarness) -> None:
    """No sysfs entry to read means one warning and no 15 s burn."""
    kiosk.drm_root.mkdir()
    run = kiosk.start(
        KIOSK_CONNECT_WAIT="15",
        KIOSK_READY_WAIT="0",
    )
    elapsed = _await_browser(run)

    skipped = _lines_with(run.stderr_text(), "skipping the connector wait")
    assert len(skipped) == 1, run.stderr_text()
    assert f"card*-{_OUTPUT_1}/status" in skipped[0]
    assert elapsed < 5.0, f"an unprobeable connector must not burn the bound, took {elapsed:.3f}s"
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_connector_wait_of_zero_logs_nothing(kiosk: KioskHarness) -> None:
    """KIOSK_CONNECT_WAIT=0 disables the wait entirely, even for a dark connector."""
    kiosk.preset_sysfs(status="disconnected", enabled="disabled")
    run = kiosk.start(
        KIOSK_CONNECT_WAIT="0",
        KIOSK_READY_WAIT="0",
    )
    _await_browser(run)

    # " connector " with spaces: the credential-free user-data-dir path contains
    # the test's own name, e.g. .../test_connector_wait_of_zero_logs0/profile.
    assert _lines_with(run.stderr_text(), " connector ") == []
    assert len(run.browser_lines()) == 1


# --- The post-launch display observation (report-only) -------------------


def _message(line: str) -> str:
    """Return a wrapper log line without its leading UTC timestamp.

    :param line: one line of the wrapper's stderr.
    """
    _, _, message = line.partition(" ")
    return message


@pytest.mark.integration
@pytest.mark.slow
def test_mode_empty_but_enabled_and_dpms_on_is_reported_ready(kiosk: KioskHarness) -> None:
    """THE BOARD: ``mode`` stays empty, yet ``enabled``+``dpms`` prove the panel is up.

    The reference kernel never populates ``mode``. The withdrawn gate called
    that a failed start and tore a working compositor down; the observation
    must report it as ready on the first poll, with the shipped 60 s bound and
    nothing torn down.
    """
    kiosk.preset_sysfs(status="connected", enabled="enabled", dpms="On", mode="")
    run = kiosk.start(KIOSK_READY_WAIT="60")  # the shipped default
    elapsed = _await_browser(run)
    ready = _await_log(run, "kiosk ready:")

    assert [_message(line) for line in ready] == [
        f"kiosk ready: displaying on {_OUTPUT_1} "
        "(enabled, dpms On; mode not reported by this kernel)"
    ], run.stderr_text()
    stderr = run.stderr_text()
    assert len(run.compositor_launches()) == 1, "a working panel is never relaunched"
    assert len(run.browser_lines()) == 1
    assert run.process.poll() is None, "the wrapper must still be supervising"
    assert _lines_with(stderr, "warning") == [], stderr
    assert _lines_with(stderr, "attempt") == [], stderr
    assert _lines_with(stderr, "tearing") == [], stderr
    assert elapsed < 5.0, f"a ready display must be reported at once, took {elapsed:.3f}s"


@pytest.mark.integration
@pytest.mark.slow
def test_mode_and_enabled_is_reported_ready(kiosk: KioskHarness) -> None:
    """All four attributes present: the ready line quotes the mode it saw."""
    kiosk.preset_sysfs(status="connected", enabled="enabled", dpms="On", mode=_READY_MODE)
    run = kiosk.start(KIOSK_READY_WAIT="5")
    _await_browser(run)
    ready = _await_log(run, "kiosk ready:")

    assert [_message(line) for line in ready] == [
        f"kiosk ready: displaying {_READY_MODE} on {_OUTPUT_1} (enabled, dpms On)"
    ], run.stderr_text()
    assert len(run.compositor_launches()) == 1
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_output_disabled_does_not_tear_down_the_compositor(kiosk: KioskHarness) -> None:
    """An output reported disabled is a warning, never a teardown or a restart."""
    kiosk.preset_sysfs(status="connected", enabled="disabled", dpms="Off")
    run = kiosk.start(KIOSK_READY_WAIT="1")
    warnings = _await_log(run, "reports itself off")

    assert len(warnings) == 1, run.stderr_text()
    message = _message(warnings[0])
    assert message == (
        f"warning: {_OUTPUT_1} reports itself off "
        "(enabled=disabled dpms=Off mode=none) after 1s; leaving the compositor "
        "running (docs/kiosk.md section 0 covers the usual cause)"
    ), message
    assert len(run.compositor_launches()) == 1, "nothing may be torn down"
    assert run.process.poll() is None, "the wrapper must still be supervising"
    assert len(run.browser_lines()) == 1, "the browser keeps running"
    assert _lines_with(run.stderr_text(), "browser exited") == [], "no restart"


@pytest.mark.integration
@pytest.mark.slow
def test_dpms_off_does_not_tear_down_the_compositor(kiosk: KioskHarness) -> None:
    """``dpms=Off`` alone is reported off, and the compositor is left running."""
    kiosk.preset_sysfs(status="connected", enabled="enabled", dpms="Off")
    run = kiosk.start(KIOSK_READY_WAIT="1")
    warnings = _await_log(run, "reports itself off")

    assert len(warnings) == 1, run.stderr_text()
    assert "(enabled=enabled dpms=Off mode=none)" in _message(warnings[0])
    assert len(run.compositor_launches()) == 1
    assert run.process.poll() is None
    assert len(run.browser_lines()) == 1
    assert _lines_with(run.stderr_text(), "browser exited") == []


@pytest.mark.integration
@pytest.mark.slow
def test_unreadable_attributes_do_not_tear_down_the_compositor(kiosk: KioskHarness) -> None:
    """No attribute files at all is inconclusive: one warning quoting what was read."""
    kiosk.preset_connector_dir()
    run = kiosk.start(KIOSK_READY_WAIT="1")
    warnings = _await_log(run, "could not confirm the display came up")

    assert len(warnings) == 1, run.stderr_text()
    message = _message(warnings[0])
    assert "status=unknown enabled=unknown dpms=unknown mode=none" in message
    assert "within 1s; leaving the compositor running" in message
    assert len(run.compositor_launches()) == 1
    assert run.process.poll() is None
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_compositor_that_exits_is_restarted_by_the_supervisor(kiosk: KioskHarness) -> None:
    """Only an exited compositor is restarted, after the injected 1 s delay."""
    kiosk.preset_connector_dir()
    run = kiosk.start(
        FAKE_COMPOSITOR_EXIT_AFTER="0.1",
        KIOSK_RESTART_DELAY="1",
        KIOSK_MAX_RESTART_DELAY="1",
    )
    _await_launches(run, 2)

    launches = run.launch_times()
    assert len(launches) == 2, run.stderr_text()
    gap = launches[1] - launches[0]
    assert gap >= 0.9, f"the restart delay was not honoured: launches {gap:.3f}s apart"
    assert gap <= 5.0, f"the restart delay overshot: launches {gap:.3f}s apart"
    stderr = run.stderr_text()
    assert len(_lines_with(stderr, "browser exited")) == 1, stderr
    assert _lines_with(stderr, "tearing") == [], "a display verdict must not tear anything down"
    assert _lines_with(stderr, "attempt") == [], stderr
    assert "error:" not in stderr, "an exited compositor is restarted, never fatal"


@pytest.mark.integration
@pytest.mark.slow
def test_compositor_that_dies_during_the_observation_is_restarted_promptly(
    kiosk: KioskHarness,
) -> None:
    """A compositor that dies mid-observation is restarted at once, not after 60 s.

    ``kill -0`` still answers for an exited-but-unreaped child, so the shipped
    60 s observation window would otherwise sit out its whole bound before the
    supervisor noticed: 60 s of dark panel where the deployed wrapper needs 1 s.
    A dead compositor is the supervisor's business, so no display verdict is
    logged either.
    """
    kiosk.preset_connector_dir()
    run = kiosk.start(
        FAKE_COMPOSITOR_EXIT_AFTER="0.3",
        KIOSK_READY_WAIT="60",  # the shipped default
        KIOSK_RESTART_DELAY="1",
        KIOSK_MAX_RESTART_DELAY="1",
    )
    _await_launches(run, 2)

    launches = run.launch_times()
    assert len(launches) == 2, run.stderr_text()
    gap = launches[1] - launches[0]
    assert gap <= 10.0, f"the observation window delayed the restart: launches {gap:.3f}s apart"
    stderr = run.stderr_text()
    assert len(_lines_with(stderr, "browser exited")) == 1, stderr
    assert _lines_with(stderr, "could not confirm the display came up") == [], stderr
    assert _lines_with(stderr, "reports itself off") == [], stderr


@pytest.mark.integration
@pytest.mark.slow
def test_attributes_appearing_late_are_reported_ready(kiosk: KioskHarness) -> None:
    """Empty attribute files filled in after 0.4 s are waited out and reported ready."""
    kiosk.preset_sysfs(status="", enabled="", dpms="", mode="")
    run = kiosk.start(
        FAKE_COMPOSITOR_READY_AFTER="0.4",
        FAKE_COMPOSITOR_DPMS="On",
        KIOSK_READY_WAIT="5",
    )
    _await_browser(run)
    ready = _await_log(run, "kiosk ready:")

    assert [_message(line) for line in ready] == [
        f"kiosk ready: displaying {_READY_MODE} on {_OUTPUT_1} (enabled, dpms On)"
    ], run.stderr_text()
    assert len(run.compositor_launches()) == 1
    assert "warning" not in run.stderr_text()


@pytest.mark.integration
@pytest.mark.slow
def test_wlr_randr_is_used_only_when_a_wayland_socket_is_reachable(
    kiosk: KioskHarness,
) -> None:
    """Inconclusive sysfs + a Wayland socket: the current mode comes from wlr-randr."""
    kiosk.preset_connector_dir()
    run = kiosk.start(
        WAYLAND_DISPLAY="wayland-0",
        FAKE_WLR_OUT_1_NAME=_OUTPUT_1,
        KIOSK_READY_WAIT="5",
    )
    _await_browser(run)
    ready = _await_log(run, "kiosk ready:")

    assert [_message(line) for line in ready] == [
        f"kiosk ready: displaying {_READY_MODE} on {_OUTPUT_1} (current mode from wlr-randr)"
    ], run.stderr_text()
    assert run.listing_count() == 1, "one listing, no mode change"
    assert run.mode_calls() == [], "the observation never changes the mode"
    assert len(run.compositor_launches()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_wlr_randr_is_not_used_without_a_wayland_socket(kiosk: KioskHarness) -> None:
    """No ``WAYLAND_DISPLAY``: not even one wlr-randr process is spawned."""
    kiosk.preset_connector_dir()
    run = kiosk.start(KIOSK_READY_WAIT="1")
    warnings = _await_log(run, "could not confirm the display came up")

    assert len(warnings) == 1, run.stderr_text()
    assert "status=unknown enabled=unknown dpms=unknown mode=none" in _message(warnings[0])
    assert run.wlr_calls() == [], "wlr-randr must not be assumed reachable"
    assert not kiosk.wlr_log.exists()


@pytest.mark.integration
@pytest.mark.slow
def test_drm_holder_warning_names_the_other_server_and_still_starts(
    kiosk: KioskHarness,
) -> None:
    """A process holding the DRM node gets one warning; the kiosk still starts."""
    holder = kiosk.start_drm_holder()
    _await_holder(holder, kiosk.drm_device_root / "card0")
    kiosk.preset_sysfs(status="connected", enabled="enabled", dpms="On")
    run = kiosk.start(KIOSK_READY_WAIT="0")
    _await_browser(run)

    warnings = _lines_with(run.stderr_text(), "another display server is using")
    assert len(warnings) == 1, run.stderr_text()
    message = _message(warnings[0])
    assert f"another display server is using {kiosk.drm_device_root}/card0" in message
    assert f"pid {holder.pid}" in message
    assert "docs/kiosk.md section 0" in message
    assert len(run.browser_lines()) == 1, "a warning never blocks the kiosk"
    assert run.process.poll() is None


@pytest.mark.integration
@pytest.mark.slow
def test_no_drm_warning_when_the_device_directory_is_absent(kiosk: KioskHarness) -> None:
    """No /dev/dri to scan (the harness never creates it): no warning at all."""
    run = kiosk.start(KIOSK_READY_WAIT="0")
    _await_browser(run)

    assert _lines_with(run.stderr_text(), "another display server is using") == []
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_observation_reports_the_weston_start(kiosk_with_weston: KioskHarness) -> None:
    """The observation works on the weston path too, and weston keeps its `--` autolaunch."""
    kiosk_with_weston.preset_connector_dir()
    run = kiosk_with_weston.start(
        FAKE_COMPOSITOR_READY_AFTER="0.4",
        FAKE_COMPOSITOR_DPMS="On",
        KIOSK_COMPOSITOR="weston",
        KIOSK_READY_WAIT="5",
    )
    _await_browser(run)
    ready = _await_log(run, f"kiosk ready: displaying {_READY_MODE} on {_OUTPUT_1}")

    assert len(ready) == 1, run.stderr_text()
    launches = run.compositor_launches()
    assert len(launches) == 1, "a ready weston is never relaunched"
    assert "--backend=drm" in launches[0]
    assert "--shell=kiosk-shell.so --" in launches[0]


@pytest.mark.integration
@pytest.mark.slow
def test_ready_wait_of_zero_observes_nothing(kiosk: KioskHarness) -> None:
    """KIOSK_READY_WAIT=0 disables the observation: no verdict line at all."""
    kiosk.preset_sysfs(status="connected", enabled="enabled", dpms="On")
    run = kiosk.start(KIOSK_READY_WAIT="0")
    _await_browser(run)

    stderr = run.stderr_text()
    assert "kiosk ready:" not in stderr
    assert _lines_with(stderr, "warning") == []
    assert len(run.compositor_launches()) == 1
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_unprobeable_sysfs_is_reported_once_without_retrying(kiosk: KioskHarness) -> None:
    """No sysfs entry to read is one inconclusive warning, not a retry loop."""
    run = kiosk.start(KIOSK_READY_WAIT="1")
    warnings = _await_log(run, "could not confirm the display came up")
    elapsed = time.monotonic() - run.started_at

    assert len(warnings) == 1, run.stderr_text()
    assert f"no {kiosk.drm_root}/card*-{_OUTPUT_1} in sysfs" in warnings[0]
    assert len(run.compositor_launches()) == 1, "an unprobeable display is not a failed start"
    assert elapsed < 5.0, f"an unprobeable display must not burn the bound, took {elapsed:.3f}s"
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_desktop_session_skips_the_display_observation(kiosk: KioskHarness) -> None:
    """No cage and no weston: there is no compositor of ours, so nothing to observe."""
    kiosk.preset_sysfs(status="connected", enabled="enabled", dpms="On")
    run = kiosk.start(
        KIOSK_CAGE="0",
        KIOSK_CONNECT_WAIT="0",
        KIOSK_READY_WAIT="15",
    )
    elapsed = _await_browser(run)

    stderr = run.stderr_text()
    skipped = _lines_with(stderr, "no compositor of our own to verify")
    assert len(skipped) == 1, stderr
    assert "skipping the display readiness check" in skipped[0]
    assert "kiosk ready:" not in stderr
    assert run.compositor_launches() == [], "no compositor is launched on this path"
    argv = _supervising_argv(stderr)
    assert argv.startswith("chromium --kiosk "), argv
    assert elapsed < 5.0, f"a desktop session must not burn the bound, took {elapsed:.3f}s"
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_restart_re_runs_the_cage_mode_snippet_once_per_launch(kiosk: KioskHarness) -> None:
    """A supervised restart re-runs the cage mode snippet exactly once per launch."""
    kiosk.preset_connector_dir()
    run = kiosk.start(
        KIOSK_MODE=_REQUESTED_MODE,
        KIOSK_OUTPUT=_OUTPUT_1,
        # The commit is rejected so it stays observable across launches; a
        # successful one would make the second launch a "not touching it" no-op.
        FAKE_WLR_APPLY_FAIL="1",
        FAKE_WLR_OUT_1_NAME=_OUTPUT_1,
        FAKE_WLR_OUT_1_MODES="1920x1080 1280x720",
        FAKE_BROWSER_EXIT_AFTER="0.1",
        KIOSK_RESTART_DELAY="1",
        KIOSK_MAX_RESTART_DELAY="1",
    )
    _await_launches(run, 2)

    # The second launch line is stamped before the snippet inside it runs, so
    # wait for its mode attempt too rather than racing it.
    deadline = time.monotonic() + _EVIDENCE_TIMEOUT_S
    while len(run.mode_calls()) < 2 and time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_S)

    launches = run.compositor_launches()
    assert len(launches) == 2, run.stderr_text()
    assert all("sh -c" in launch for launch in launches), launches
    assert len(run.mode_calls()) == len(launches), (
        "one mode attempt per compositor launch: the restart is the supervisor's, "
        f"not wlr-randr's ({run.mode_calls()})"
    )


@pytest.mark.integration
@pytest.mark.slow
def test_shipped_defaults_are_the_long_ones() -> None:
    """``--help`` documents 60/60, the new DRM device root, and no START_* knobs."""
    help_text = _help_text()

    assert _documented_default(help_text, "KIOSK_CONNECT_WAIT") == "60"
    assert _documented_default(help_text, "KIOSK_READY_WAIT") == "60"
    assert "KIOSK_DRM_DEVICE_ROOT" in help_text
    assert "KIOSK_START_ATTEMPTS" not in help_text
    assert "KIOSK_START_RETRY_DELAY" not in help_text


# --- The weston path and its fallbacks -----------------------------------


@pytest.mark.integration
@pytest.mark.slow
def test_weston_ini_carries_the_requested_mode_and_connector(
    kiosk_with_weston: KioskHarness,
) -> None:
    """The generated ini is the whole point: name plus mode, read at startup."""
    run = kiosk_with_weston.start(
        KIOSK_COMPOSITOR="weston",
        KIOSK_MODE=_REQUESTED_MODE,
        KIOSK_OUTPUT=_OUTPUT_2,
        FAKE_CONNECTOR=_OUTPUT_2,
        KIOSK_CONNECT_WAIT="0",
        KIOSK_READY_WAIT="0",
    )
    _await_browser(run)

    ini = run.weston_ini_text()
    assert f"name={_OUTPUT_2}" in ini, ini
    assert f"mode={_REQUESTED_MODE}" in ini, ini
    assert "idle-time=0" in ini
    assert "require-input=false" in ini
    assert "xwayland=false" in ini
    stderr = run.stderr_text()
    assert (
        "generated weston.ini: [core];idle-time=0;require-input=false;xwayland=false;"
        f"[output];name={_OUTPUT_2};mode={_REQUESTED_MODE};"
    ) in stderr, stderr
    config_paths = run.weston_configs()
    assert len(config_paths) == 1, stderr
    launches = run.compositor_launches()
    assert len(launches) == 1
    assert "--backend=drm" in launches[0]
    assert f"--config={config_paths[0]}" in launches[0]
    assert "--shell=kiosk-shell.so" in launches[0]
    assert "--shell=kiosk-shell.so -- chromium " in launches[0], launches[0]
    assert (
        f"weston will modeset {_OUTPUT_2} to {_REQUESTED_MODE} as its initial mode "
        f"(config: {config_paths[0]})"
    ) in stderr
    browser = run.browser_lines()
    assert len(browser) == 1
    assert "--kiosk" in browser[0]
    assert f"--app={_APP_URL}" in browser[0]


@pytest.mark.integration
@pytest.mark.slow
def test_weston_ini_omits_the_mode_when_kiosk_mode_is_empty(
    kiosk_with_weston: KioskHarness,
) -> None:
    """No KIOSK_MODE means no mode line: weston uses the display's EDID mode."""
    run = kiosk_with_weston.start(
        KIOSK_COMPOSITOR="weston",
        KIOSK_CONNECT_WAIT="0",
        KIOSK_READY_WAIT="0",
    )
    _await_browser(run)

    ini = run.weston_ini_text()
    assert "[output]" in ini, ini
    assert "mode=" not in ini, ini
    stderr = run.stderr_text()
    assert "weston will use the display's own (EDID) mode" in stderr
    assert len(run.compositor_launches()) == 1
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_weston_ini_defaults_the_connector_to_hdmi_a_1(
    kiosk_with_weston: KioskHarness,
) -> None:
    """With no KIOSK_OUTPUT the ini names the default connector, not an empty name."""
    run = kiosk_with_weston.start(
        KIOSK_COMPOSITOR="weston",
        KIOSK_CONNECT_WAIT="0",
        KIOSK_READY_WAIT="0",
    )
    _await_browser(run)

    ini = run.weston_ini_text()
    assert f"[output]\nname={_OUTPUT_1}\n" in ini, ini
    assert len(run.compositor_launches()) == 1
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skipif(
    shutil.which("weston") is not None,
    reason="a real weston is on PATH, so 'not installed' cannot be simulated",
)
def test_missing_weston_falls_back_to_cage_without_the_mode(
    kiosk_without_weston: KioskHarness,
) -> None:
    """No weston: one warning, and cage at EDID -- never the post-modeset mode change."""
    run = kiosk_without_weston.start(
        KIOSK_COMPOSITOR="weston",
        KIOSK_MODE=_REQUESTED_MODE,
        KIOSK_CAGE="1",
        KIOSK_CONNECT_WAIT="0",
        KIOSK_READY_WAIT="0",
    )
    _await_browser(run)

    stderr = run.stderr_text()
    warnings = _lines_with(stderr, "weston is not installed")
    assert len(warnings) == 1, stderr
    assert "falling back to cage at the display's own (EDID) mode" in warnings[0]
    argv = _supervising_argv(stderr)
    assert argv.startswith("cage -- chromium "), argv
    assert "sh -c" not in argv
    assert run.mode_calls() == [], "the fallback must not re-apply the mode through wlr-randr"
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_unwritable_tmpdir_falls_back_to_cage(kiosk_with_weston: KioskHarness) -> None:
    """A weston ini that cannot be created is a warning plus cage, never a dark panel."""
    missing_tmpdir = kiosk_with_weston.tmp_path / "no-such-tmpdir"
    run = kiosk_with_weston.start(
        KIOSK_COMPOSITOR="weston",
        KIOSK_MODE=_REQUESTED_MODE,
        KIOSK_CONNECT_WAIT="0",
        KIOSK_READY_WAIT="0",
        TMPDIR=str(missing_tmpdir),
    )
    _await_browser(run)

    stderr = run.stderr_text()
    warnings = _lines_with(stderr, "could not create a private directory for weston.ini")
    assert len(warnings) == 1, stderr
    argv = _supervising_argv(stderr)
    assert argv.startswith("cage -- chromium "), argv
    assert "sh -c" not in argv
    assert run.mode_calls() == []
    assert len(run.browser_lines()) == 1
    assert not missing_tmpdir.exists(), "mktemp must not have created the missing parent"


@pytest.mark.integration
@pytest.mark.slow
def test_unknown_compositor_warns_and_uses_cage(kiosk: KioskHarness) -> None:
    """A typo in KIOSK_COMPOSITOR is a warning, not a reason to leave the panel dark."""
    run = kiosk.start(
        KIOSK_COMPOSITOR="westonx",
        KIOSK_CONNECT_WAIT="0",
        KIOSK_READY_WAIT="0",
    )
    _await_browser(run)

    stderr = run.stderr_text()
    warnings = _lines_with(stderr, "unknown KIOSK_COMPOSITOR")
    assert len(warnings) == 1, stderr
    assert "westonx" in warnings[0]
    assert "using cage" in warnings[0]
    assert _supervising_argv(stderr).startswith("cage -- chromium ")
    assert len(run.compositor_launches()) == 1
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    "compositor_env",
    [{}, {"KIOSK_COMPOSITOR": "cage"}],
    ids=["unset", "cage"],
)
def test_cage_is_never_displaced_by_an_installed_weston(
    kiosk_with_weston: KioskHarness,
    compositor_env: dict[str, str],
) -> None:
    """A weston binary on PATH cannot pull the default path off cage."""
    run = kiosk_with_weston.start(
        KIOSK_MODE=_REQUESTED_MODE,
        KIOSK_CONNECT_WAIT="0",
        KIOSK_READY_WAIT="0",
        **compositor_env,
    )
    _await_browser(run)

    argv = _supervising_argv(run.stderr_text())
    assert argv.startswith(f"cage -- sh -c MODE_SCRIPT kiosk {_REQUESTED_MODE}"), argv
    assert run.weston_configs() == [], "the weston path must not be entered by accident"
    assert not kiosk_with_weston.weston_ini_copy.exists()
    assert len(run.browser_lines()) == 1


@pytest.mark.integration
@pytest.mark.slow
def test_the_generated_weston_config_is_removed_on_stop(kiosk_with_weston: KioskHarness) -> None:
    """The private ini directory does not survive the wrapper: no /tmp litter."""
    private_tmp = kiosk_with_weston.tmp_path / "tmpdir"
    private_tmp.mkdir()
    run = kiosk_with_weston.start(
        KIOSK_COMPOSITOR="weston",
        KIOSK_CONNECT_WAIT="0",
        KIOSK_READY_WAIT="0",
        TMPDIR=str(private_tmp),
    )
    _await_browser(run)

    created = list(private_tmp.glob("chime-deck-weston.*"))
    assert len(created) == 1, f"the ini directory must exist while running: {created}"
    kiosk_with_weston.stop()

    assert list(private_tmp.glob("chime-deck-weston.*")) == [], "the EXIT trap must clean up"


def _mode_snippet(source: str) -> str:
    """Return the text of the ``KIOSK_MODE_SCRIPT`` heredoc in ``kiosk.sh``.

    :param source: the whole ``deploy/kiosk.sh`` text.
    """
    lines = source.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if f"<<'{_MODE_HEREDOC_END}'" in line:
            start = index + 1
            break
    else:
        raise AssertionError(f"deploy/kiosk.sh has no <<'{_MODE_HEREDOC_END}' heredoc")
    for index in range(start, len(lines)):
        if lines[index].strip() == _MODE_HEREDOC_END:
            return "\n".join(lines[start:index])
    raise AssertionError(f"the {_MODE_HEREDOC_END} heredoc is never closed")


def test_mode_snippet_is_strict_posix() -> None:
    """The snippet must run under dash: no bashisms may creep into it.

    The panel's ``/bin/sh`` is dash, and this dev host has neither dash nor
    busybox (``/bin/sh`` here is bash), so executing the snippet would prove
    nothing about the panel. This static check is the only mechanical guard
    available for the constructs that would break it there.
    """
    snippet = _mode_snippet(_KIOSK_SH.read_text(encoding="utf-8"))

    assert "apply_mode" in snippet, "the heredoc must still hold the display-mode logic"
    assert "[[ " not in snippet and "[[" not in snippet
    assert "local " not in snippet
    assert "function " not in snippet
    assert "set -" not in snippet

    # `((` is fine only as part of a `$((...))` arithmetic expansion.
    offenders: list[str] = []
    index = snippet.find("((")
    while index != -1:
        if index == 0 or snippet[index - 1] != "$":
            offenders.append(snippet[max(0, index - 20) : index + 40])
        index = snippet.find("((", index + 2)
    assert offenders == [], f"bash arithmetic commands in a POSIX snippet: {offenders}"
