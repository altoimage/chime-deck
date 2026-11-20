#!/usr/bin/env bash
#
# chime-deck-kiosk -- supervise a full-screen Chromium showing the dashboard.
#
# Install (the wrapper is self-contained; a `uv tool install` backend has no
# checkout, so this must not assume a git-clone path):
#
#   sudo install -m 755 deploy/kiosk.sh /usr/local/bin/chime-deck-kiosk
#
# Usage:
#   chime-deck-kiosk [URL] [--url URL] [--command CMD] [--cage]
#                        [--compositor NAME] [--mode WxH] [-- CHROMIUM_ARGS...]
#
#   -h, --help   print this help and exit 0
#   URL          bare positional URL, same as --url
#   --url URL    dashboard URL to open
#   --command CMD
#                browser executable (default chromium)
#   --cage       run the browser inside `cage --` (a minimal Wayland kiosk)
#   --compositor NAME
#                cage (default) or weston; weston supersedes --cage
#   --mode WxH   pin the display mode from inside cage with wlr-randr (e.g.
#                1280x720); applied once and verified by a re-read, and a
#                failure leaves the panel at its own mode; ignored without
#                cage or wlr-randr
#   --           pass every remaining argument to the browser after the
#                built-in flags
#
# Environment (CLI beats env beats default):
#   KIOSK_URL               URL to show (default http://127.0.0.1:8080/?kiosk=1)
#   KIOSK_COMMAND           browser executable (default chromium)
#   KIOSK_CAGE=1            equivalent to --cage
#   KIOSK_MODE              display mode as WIDTHxHEIGHT, set inside cage with
#                           wlr-randr (default empty, e.g. 1280x720)
#   KIOSK_OUTPUT            connector to pin the mode on and to observe,
#                           e.g. HDMI-A-1 (default empty: the cage mode path
#                           derives it from wlr-randr, and the observation and
#                           the weston config use HDMI-A-1)
#   KIOSK_MODE_WAIT         seconds to wait for the output to report itself
#                           enabled before the single mode change (default 10,
#                           0 = one immediate probe)
#   KIOSK_RESTART_DELAY     first restart delay in seconds (default 1)
#   KIOSK_MAX_RESTART_DELAY backoff cap in seconds (default 60, >= base)
#   KIOSK_HEALTHY_AFTER     a run at least this long resets the backoff (default 60)
#   KIOSK_USER_DATA_DIR     dedicated Chromium profile
#                           (default ${XDG_CACHE_HOME:-$HOME/.cache}/chime-deck-kiosk)
#   KIOSK_SCREEN_BLANKING=1 disable X11 blanking/DPMS with xset (default 1)
#   KIOSK_WAIT_FOR_URL      seconds to wait for the URL before launching (default 30;
#                           0 disables the pre-flight)
#   KIOSK_COMPOSITOR        cage (default) or weston
#   KIOSK_CONNECT_WAIT      seconds to wait for the connector to report itself
#                           connected before starting (default 60, 0 disables)
#   KIOSK_READY_WAIT        seconds to wait for the connector to report the
#                           display is up after launching (default 60,
#                           0 disables)
#   KIOSK_DRM_ROOT          sysfs DRM class directory for the probes
#                           (default /sys/class/drm)
#   KIOSK_DRM_DEVICE_ROOT   DRM device directory scanned for other display
#                           servers holding the display (default /dev/dri)
#
# Why each Chromium flag: see the comments on chromium_flags below and
# docs/kiosk.md. The restart loop is deliberately dumb: one flag list, one
# supervised process, exponential backoff capped at KIOSK_MAX_RESTART_DELAY.
# A display that does not come up is reported once and then left alone: the
# wrapper never tears a compositor down and never exits because of a display
# verdict, so it cannot turn a working panel off.

set -euo pipefail

readonly DEFAULT_URL='http://127.0.0.1:8080/?kiosk=1'

usage() {
  cat <<'EOF'
Usage: chime-deck-kiosk [URL] [--url URL] [--command CMD] [--cage]
                            [--compositor NAME] [--mode WxH]
                            [-- CHROMIUM_ARGS...]

  -h, --help       show this help and exit
  URL              dashboard URL to open (same as --url URL)
  --url URL        dashboard URL to open
  --command CMD    browser executable (default: chromium)
  --cage           run the browser under `cage --`
  --compositor NAME
                   cage (default) or weston; weston supersedes --cage
  --mode WxH       ask the compositor for this display mode (e.g. 1280x720),
                   applied once and verified by a re-read; a failure leaves the
                   panel at its own mode
  --               pass the remaining arguments to the browser

Environment:
  KIOSK_URL, KIOSK_COMMAND, KIOSK_CAGE, KIOSK_COMPOSITOR, KIOSK_MODE,
  KIOSK_OUTPUT, KIOSK_MODE_WAIT, KIOSK_CONNECT_WAIT, KIOSK_READY_WAIT,
  KIOSK_RESTART_DELAY, KIOSK_MAX_RESTART_DELAY, KIOSK_HEALTHY_AFTER,
  KIOSK_USER_DATA_DIR, KIOSK_SCREEN_BLANKING, KIOSK_WAIT_FOR_URL,
  KIOSK_DRM_ROOT, KIOSK_DRM_DEVICE_ROOT

Why --mode: on some ARM boards (e.g. the Radxa ZERO 3W reference panel) a bare
compositor inherits the panel's EDID-preferred mode (1920x1080), which is
physically tiny and unusable on a 7-inch-class panel, so `--mode 1280x720` asks
the compositor for a smaller mode before the browser starts.

How --mode is applied: kiosk.sh waits (bounded by KIOSK_MODE_WAIT, default
10 s) for the output to report itself enabled, checks that the requested mode
is advertised, applies it ONCE, then verifies it by re-reading wlr-randr. A
rejected atomic commit is never retried -- retrying it is exactly what cost
this board its display at cold boot -- and the browser always starts, at the
panel's own mode if the change did not take.

Why --compositor weston: some drivers (the Radxa ZERO 3W reference panel, seen
three times) accept the compositor's FIRST modeset but reject any later mode
change and leave the panel dead, so setting the mode after the compositor is up
cannot work there. weston reads `[output] mode=` at startup, so it picks the
small mode as its initial modeset instead. --cage stays the default and its
behaviour is unchanged; weston is opt-in.

The weston path runs `weston --backend=drm --config=<generated weston.ini>
--shell=kiosk-shell.so -- chromium ...` with a private config file holding
[core] idle-time=0, require-input=false, xwayland=false and [output] name=<out>
plus mode=<KIOSK_MODE> (the mode line is omitted when KIOSK_MODE is empty, so
weston uses the display's EDID mode). The shell name needs its `.so` suffix:
weston 14 turns a bare `kiosk-shell` into `kiosk-shell-shell.so` and refuses to
start. If weston is not installed, or the config cannot be written, the wrapper
logs one warning and falls back to cage.

How a start is verified: at cold boot the unit can start before the HDMI
connector is ready, and a compositor that modesets then can end up running with
nothing on screen. So the wrapper waits (KIOSK_CONNECT_WAIT, default 60 s) for
the connector to report connected before launching, and after launching the
compositor it observes that connector (KIOSK_READY_WAIT, default 60 s) and
reports what it saw exactly once: `kiosk ready: displaying <mode> on
<connector> (<evidence>)` when the display is up, or one `warning: ...` when it
is not. The wrapper never tears a compositor down and never exits because of a
display verdict; only a compositor that has exited is restarted (with the
KIOSK_RESTART_DELAY backoff). If another display server owns the DRM device
the kiosk compositor cannot drive the display: the wrapper logs one warning
naming the holder (scanned under KIOSK_DRM_DEVICE_ROOT, default /dev/dri) and
keeps going -- see docs/kiosk.md section 0. `active` therefore means
`supervising`, not `displaying`. KIOSK_MODE never affects whether the kiosk
starts.
EOF
}

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)" "$*" >&2
}

fail_usage() {
  printf 'chime-deck-kiosk: %s\n' "$*" >&2
  printf 'Try `chime-deck-kiosk --help`.\n' >&2
  exit 2
}

check_integer() {
  local name=$1 value=$2 minimum=$3
  if [[ ! $value =~ ^[0-9]+$ ]]; then
    fail_usage "$name must be an integer number of seconds (got '$value')"
  fi
  if (( 10#$value < minimum )); then
    fail_usage "$name must be at least $minimum (got '$value')"
  fi
}

# --- Defaults from the environment, overridden by the CLI below ------------

url=${KIOSK_URL:-$DEFAULT_URL}
command_name=${KIOSK_COMMAND:-chromium}
mode=${KIOSK_MODE:-}
output=${KIOSK_OUTPUT:-}
mode_wait=${KIOSK_MODE_WAIT:-10}
cage_mode=0
if [[ ${KIOSK_CAGE:-0} == 1 ]]; then
  cage_mode=1
fi
restart_delay=${KIOSK_RESTART_DELAY:-1}
max_restart_delay=${KIOSK_MAX_RESTART_DELAY:-60}
healthy_after=${KIOSK_HEALTHY_AFTER:-60}
user_data_dir=${KIOSK_USER_DATA_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/chime-deck-kiosk}
screen_blanking=${KIOSK_SCREEN_BLANKING:-1}
wait_for_url=${KIOSK_WAIT_FOR_URL:-30}
compositor=${KIOSK_COMPOSITOR:-cage}
connect_wait=${KIOSK_CONNECT_WAIT:-60}
ready_wait=${KIOSK_READY_WAIT:-60}
drm_root=${KIOSK_DRM_ROOT:-/sys/class/drm}
drm_device_root=${KIOSK_DRM_DEVICE_ROOT:-/dev/dri}

browser_args=()
url_from_cli=0

while (( $# > 0 )); do
  case $1 in
    -h | --help)
      usage
      exit 0
      ;;
    --url)
      (( $# >= 2 )) || fail_usage '--url requires a value'
      url=$2
      url_from_cli=1
      shift 2
      ;;
    --command)
      (( $# >= 2 )) || fail_usage '--command requires a value'
      command_name=$2
      shift 2
      ;;
    --cage)
      cage_mode=1
      shift
      ;;
    --compositor)
      (( $# >= 2 )) || fail_usage '--compositor requires a value'
      compositor=$2
      shift 2
      ;;
    --mode)
      (( $# >= 2 )) || fail_usage '--mode requires a value'
      mode=$2
      shift 2
      ;;
    --)
      shift
      browser_args=("$@")
      break
      ;;
    -*)
      fail_usage "unknown option: $1"
      ;;
    *)
      if (( url_from_cli )); then
        fail_usage 'more than one URL given'
      fi
      url=$1
      url_from_cli=1
      shift
      ;;
  esac
done

# --- Validate -------------------------------------------------------------

[[ -n $url ]] || fail_usage 'the URL must not be empty'
[[ -n $command_name ]] || fail_usage 'the browser command must not be empty'
if [[ -n $mode && ! $mode =~ ^[0-9]+x[0-9]+$ ]]; then
  fail_usage "KIOSK_MODE must be WIDTHxHEIGHT (e.g. 1280x720), got '$mode'"
fi
if [[ -n $output && ! $output =~ ^[A-Za-z0-9._:-]+$ ]]; then
  fail_usage "KIOSK_OUTPUT must be a connector name (e.g. HDMI-A-1), got '$output'"
fi

# An unknown compositor is a typo in an opt-in setting, not a reason to leave
# the panel dark: warn and use the default. (A malformed KIOSK_MODE stays a
# hard error -- that one changes what we ask the hardware to do.)
if [[ $compositor != cage && $compositor != weston ]]; then
  log "warning: unknown KIOSK_COMPOSITOR '$compositor' (expected cage or weston); using cage"
  compositor=cage
fi

# One connector name, resolved once: the pre-start probe, the readiness probe
# and the generated weston.ini all use it. A [output] section whose name=
# matches no connector is silently ignored by weston, so a wrong default
# degrades to the EDID mode instead of breaking the display.
connector=${output:-HDMI-A-1}

check_integer KIOSK_RESTART_DELAY "$restart_delay" 1
check_integer KIOSK_MAX_RESTART_DELAY "$max_restart_delay" 1
check_integer KIOSK_HEALTHY_AFTER "$healthy_after" 0
check_integer KIOSK_WAIT_FOR_URL "$wait_for_url" 0
check_integer KIOSK_MODE_WAIT "$mode_wait" 0
check_integer KIOSK_CONNECT_WAIT "$connect_wait" 0
check_integer KIOSK_READY_WAIT "$ready_wait" 0

restart_delay=$((10#$restart_delay))
max_restart_delay=$((10#$max_restart_delay))
healthy_after=$((10#$healthy_after))
wait_for_url=$((10#$wait_for_url))
mode_wait=$((10#$mode_wait))
connect_wait=$((10#$connect_wait))
ready_wait=$((10#$ready_wait))

if (( max_restart_delay < restart_delay )); then
  fail_usage 'KIOSK_MAX_RESTART_DELAY must be greater than or equal to KIOSK_RESTART_DELAY'
fi

if ! command -v "$command_name" >/dev/null 2>&1; then
  printf 'chime-deck-kiosk: browser command not found: %s\n' "$command_name" >&2
  exit 1
fi
if (( cage_mode )) && ! command -v cage >/dev/null 2>&1; then
  printf 'chime-deck-kiosk: `cage` was requested but is not installed (apt install cage)\n' >&2
  exit 1
fi
# Never refuse to start the kiosk. KIOSK_MODE is cleared on the way out on
# purpose: falling back to cage *with* a mode would re-run the post-modeset
# wlr-randr change that cost the reference panel its display three times,
# whereas cage at the EDID mode is a visible panel.
#
# `cage_mode` is set so that a weston start still ends up under a compositor
# when the fallback fires, and so that the display observation still runs.
# Without it, `KIOSK_COMPOSITOR=weston` with no --cage would fall back to a bare
# browser with no display server at all.
fall_back_to_cage() {
  log "warning: $1; falling back to cage at the display's own (EDID) mode (KIOSK_MODE not applied here: set KIOSK_COMPOSITOR=cage to use the wlr-randr path instead)"
  compositor=cage
  mode=''
  if command -v cage >/dev/null 2>&1; then
    cage_mode=1
  else
    log 'warning: cage is not installed either; running the browser with no compositor'
  fi
}

if [[ $compositor == weston ]] && ! command -v weston >/dev/null 2>&1; then
  fall_back_to_cage 'KIOSK_COMPOSITOR=weston but weston is not installed (apt install weston)'
fi

# A display mode is a nicety, never a reason to keep the panel dark: drop it
# with a warning and carry on.
if [[ $compositor == cage && -n $mode ]]; then
  if ! (( cage_mode )); then
    log 'warning: KIOSK_MODE only applies to cage; ignoring it'
    mode=''
  elif ! command -v wlr-randr >/dev/null 2>&1; then
    log "warning: wlr-randr is not installed (apt install wlr-randr); ignoring KIOSK_MODE, the browser starts at the display's own (EDID) mode"
    mode=''
  fi
fi

if ! mkdir -p "$user_data_dir"; then
  printf 'chime-deck-kiosk: cannot create the profile directory: %s\n' "$user_data_dir" >&2
  exit 1
fi

# --- weston configuration (weston path only) ----------------------------
# A private temp dir, removed on exit (see the EXIT trap below). Nothing is
# created at all on the cage path.

weston_config_dir=''
weston_ini=''

cleanup_weston_config() {
  # No log line: this runs from an EXIT trap on every exit path, including
  # SIGTERM, where a message per stop is noise.
  if [[ -n $weston_config_dir ]]; then
    rm -rf -- "$weston_config_dir"
    weston_config_dir=''
  fi
}
trap cleanup_weston_config EXIT

if [[ $compositor == weston ]]; then
  if weston_config_dir=$(mktemp -d "${TMPDIR:-/tmp}/chime-deck-weston.XXXXXX"); then
    weston_ini=$weston_config_dir/weston.ini
    # idle-time=0: weston's default is a 300 s fade to black, and a kiosk panel
    # has no pointer to wake it. require-input=false: a panel with no input
    # device would otherwise make weston refuse to start. xwayland=false is
    # explicit because weston 14's code default and its man page disagree.
    if ! {
      printf '%s\n' '[core]'
      printf '%s\n' 'idle-time=0'
      printf '%s\n' 'require-input=false'
      printf '%s\n' 'xwayland=false'
      printf '%s\n' ''
      printf '%s\n' '[output]'
      printf 'name=%s\n' "$connector"
      if [[ -n $mode ]]; then
        printf 'mode=%s\n' "$mode"
      fi
    } >"$weston_ini" 2>/dev/null; then
      fall_back_to_cage "could not write $weston_ini"
    else
      log "generated weston.ini: $(grep -v '^$' "$weston_ini" | tr '\n' ';')"
    fi
  else
    weston_config_dir=''
    fall_back_to_cage 'could not create a private directory for weston.ini'
  fi
fi

# --- Built-in Chromium flags ---------------------------------------------
# --kiosk: no tabs, no toolbar.
# --app: no address bar or omnibox even if kiosk mode is somehow lost.
# --noerrdialogs: no modal error dialogs on the panel.
# --disable-infobars: kept per design 06 §5.1; a deprecated no-op in modern
#   Chromium, harmless.
# --disable-session-crashed-bubble: no "restore pages" bubble after a power cut.
# --disable-features: no translate bubble or Cast/Media Router chrome.
# --disable-pinch: a stray touch pinch must not zoom the dashboard.
# --overscroll-history-navigation=0: a swipe must not navigate back.
# --autoplay-policy: belt-and-braces; the app does not autoplay.
# --user-data-dir: a dedicated profile, never a real ~/.config/chromium.
# --no-first-run / --no-default-browser-check: no first-run wizard.
# --password-store=basic: no keyring prompt or dialog on a panel.
# --disable-component-update / --check-for-update-interval: no background
#   component updates mid-shift.
# Deliberately absent: --incognito (state is not kept, so the UI reloads
# cold), --no-sandbox (the sandbox stays on), --disable-gpu and
# --start-fullscreen (kiosk/app already fill the screen).
chromium_flags=(
  --kiosk
  "--app=$url"
  --noerrdialogs
  --disable-infobars
  --disable-session-crashed-bubble
  --disable-features=Translate,TranslateUI,MediaRouter
  --disable-pinch
  --overscroll-history-navigation=0
  --autoplay-policy=no-user-gesture-required
  "--user-data-dir=$user_data_dir"
  --no-first-run
  --no-default-browser-check
  --password-store=basic
  --disable-component-update
  --check-for-update-interval=31536000
)

# --- Process state and signal handling -----------------------------------

child_pid=''
sleeper_pid=''
stopping=0

# Bounded teardown of the supervised child: SIGTERM, up to 5 s, then SIGKILL.
# There is no unbounded wait anywhere in this script.
terminate_child() {
  if [[ -z $child_pid ]] || ! kill -0 "$child_pid" 2>/dev/null; then
    if [[ -n $child_pid ]]; then
      wait "$child_pid" 2>/dev/null || true
    fi
    child_pid=''
    return 0
  fi
  kill -TERM "$child_pid" 2>/dev/null || true
  local i
  for i in $(seq 1 50); do
    if ! kill -0 "$child_pid" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  if kill -0 "$child_pid" 2>/dev/null; then
    log 'child did not stop within 5s; sending SIGKILL'
    kill -KILL "$child_pid" 2>/dev/null || true
  fi
  wait "$child_pid" 2>/dev/null || true
  child_pid=''
}

stop() {
  if (( stopping )); then
    return 0
  fi
  stopping=1
  log 'stopping'
  terminate_child
  if [[ -n $sleeper_pid ]] && kill -0 "$sleeper_pid" 2>/dev/null; then
    kill "$sleeper_pid" 2>/dev/null || true
    wait "$sleeper_pid" 2>/dev/null || true
    sleeper_pid=''
  fi
  log 'stopped'
  exit 143
}
trap stop TERM INT

# --- Display power --------------------------------------------------------
# Runs on every start, so it survives a reboot (the xset settings are not
# persistent). Never fatal: a Wayland session has no xset target at all.

if [[ $screen_blanking == 1 ]]; then
  if [[ -z ${DISPLAY:-} ]]; then
    log 'DISPLAY is unset (Wayland/cage): skipping X11 blanking suppression; see docs/kiosk.md'
  elif ! command -v xset >/dev/null 2>&1; then
    log 'warning: xset not found; screen blanking/DPMS may kick in'
  else
    if ! xset s off >/dev/null 2>&1; then
      log 'warning: `xset s off` failed; the screen may blank'
    fi
    if ! xset -dpms >/dev/null 2>&1; then
      log 'warning: `xset -dpms` failed; the screen may power off'
    fi
    if ! xset s noblank >/dev/null 2>&1; then
      log 'warning: `xset s noblank` failed; the screen may blank'
    fi
  fi
fi

# --- Display observation probes -----------------------------------------
# Read-only sysfs, plus one `wlr-randr` listing at most when sysfs is
# inconclusive. The same observation works for cage and for weston (wlr-randr
# is a wlroots tool and weston is not wlroots, so under weston it is never
# consulted).
#
# Nothing here is an enforcement gate: the observation is reported once per
# wrapper run and never changes a mode, tears a compositor down or exits.

drm_dirs=()
for candidate in "$drm_root"/card*-"$connector"; do
  if [[ -d $candidate ]]; then
    drm_dirs+=("$candidate")
  fi
done

read_attr() {
  local value=''
  if [[ -r $1/$2 ]]; then
    IFS= read -r value <"$1/$2" || true
  fi
  value=${value//[[:space:]]/}
  printf '%s' "$value"
}

# Name every process that holds a DRM card node open, except ourselves and
# systemd-logind (which holds the device for seat management and never keeps
# DRM master). One entry per pid; capped at three with a `+N more` tail so the
# caller can log a single line. Empty output means nobody holds it.
drm_holders() {
  local link pid name
  local -a holders=()
  local -A seen=()
  [[ -d $drm_device_root ]] || return 0
  command -v find >/dev/null 2>&1 || return 0
  # One `find` for the whole scan. A readlink per open file measured 2.86 s on a
  # 16-core host with ~500 processes; matching the symlink targets in place
  # measured 0.017 s. This runs before the display comes up, so the forks matter.
  local links=''
  links=$(find /proc/[0-9]*/fd/ -maxdepth 1 -lname "$drm_device_root/card[0-9]*" 2>/dev/null) || true
  while IFS= read -r link; do
    [[ -n $link ]] || continue
    pid=${link#/proc/}
    pid=${pid%%/*}
    [[ $pid == "$$" ]] && continue
    [[ -n ${seen[$pid]:-} ]] && continue
    seen[$pid]=1
    name=$(read_attr "/proc/$pid" comm)
    [[ $name == systemd-logind ]] && continue
    holders+=("${name:-unknown} pid $pid")
  done <<< "$links"
  (( ${#holders[@]} )) || return 0
  local summary=${holders[0]} shown=1
  while (( shown < ${#holders[@]} && shown < 3 )); do
    summary="${summary}, ${holders[shown]}"
    shown=$((shown + 1))
  done
  if (( ${#holders[@]} > 3 )); then
    summary="${summary} (+$(( ${#holders[@]} - 3 )) more)"
  fi
  printf '%s' "$summary"
}

describe_connector() {
  if (( ${#drm_dirs[@]} == 0 )); then
    printf 'no %s/card*-%s in sysfs' "$drm_root" "$connector"
    return 0
  fi
  local status enabled dpms current
  status=$(read_attr "${drm_dirs[0]}" status)
  enabled=$(read_attr "${drm_dirs[0]}" enabled)
  dpms=$(read_attr "${drm_dirs[0]}" dpms)
  current=$(read_attr "${drm_dirs[0]}" mode)
  printf 'status=%s enabled=%s dpms=%s mode=%s' "${status:-unknown}" "${enabled:-unknown}" "${dpms:-unknown}" "${current:-none}"
}

# 0 = connector connected, 1 = still not connected when the bound expired,
# 2 = no sysfs entry to read at all (not probeable).
#
# The bound is a poll count, not a deadline: bash's SECONDS flips on wall-clock
# second boundaries rather than one second after this shell started, so
# `SECONDS >= started + bound` can give up almost a second early. Four polls
# per second is the same arithmetic the cage snippet uses above.
await_connector() {
  local bound=$1 started=$SECONDS status='' attempt=0
  local polls=$(( $1 * 4 + 1 ))
  if (( ${#drm_dirs[@]} == 0 )); then
    return 2
  fi
  while :; do
    attempt=$(( attempt + 1 ))
    status=$(read_attr "${drm_dirs[0]}" status)
    if [[ $status == connected ]]; then
      if (( SECONDS > started )); then
        log "connector $connector is connected (waited $((SECONDS - started))s)"
      else
        log "connector $connector is connected"
      fi
      return 0
    fi
    if (( attempt >= polls )); then
      return 1
    fi
    sleep 0.25
  done
}

# The current mode of one connector in a wlr-randr listing, or nothing.
current_mode_of() {
  awk -v want="$1" '
    /^[^ \t]/ { inblock = ($1 == want); next }
    !inblock { next }
    /^[ \t]+[0-9]+x[0-9]+[ \t]+px/ && /current/ {
      line = $0; sub(/^[ \t]+/, "", line); split(line, f, /[ \t]+/); print f[1]; exit
    }
  '
}

describe_ready() {
  local current=$1 how=$2
  if [[ -n $current ]]; then
    printf 'displaying %s on %s (%s)' "$current" "$connector" "$how"
  else
    printf 'displaying on %s (%s; mode not reported by this kernel)' "$connector" "$how"
  fi
}

# 0 = ready (prints what was observed), 1 = the output reports itself off,
# 2 = nothing conclusive. Every tier is read-only: this never changes a mode
# and never makes anything happen on the panel.
#
# Preference order, strongest evidence first:
#   a. sysfs `enabled` plus `dpms`. This is the tier that works on the
#      reference kernel, which never populates `mode`.
#   b. `wlr-randr`'s `(current)` mode, only under cage, only when a Wayland
#      socket is reachable, and only when `wlr-randr` exists.
#   c. a non-empty sysfs `mode`, for kernels that do populate it.
# `status` is descriptive only: `status=disconnected` alone is not "off".
observe_connector() {
  local status='' enabled='' dpms='' current='' listing='' wlr_mode=''
  if (( ${#drm_dirs[@]} == 0 )); then
    printf 'no %s/card*-%s in sysfs' "$drm_root" "$connector"
    return 2
  fi
  status=$(read_attr "${drm_dirs[0]}" status)
  enabled=$(read_attr "${drm_dirs[0]}" enabled)
  dpms=$(read_attr "${drm_dirs[0]}" dpms)
  current=$(read_attr "${drm_dirs[0]}" mode)
  if [[ $enabled == enabled && ( -z $dpms || $dpms == On ) ]]; then
    describe_ready "$current" "enabled, dpms ${dpms:-not reported by this kernel}"
    return 0
  fi
  if [[ $enabled == disabled || $dpms == Off ]]; then
    printf 'enabled=%s dpms=%s mode=%s' "${enabled:-unknown}" "${dpms:-unknown}" "${current:-none}"
    return 1
  fi
  if [[ $compositor == cage && -n ${WAYLAND_DISPLAY:-} ]] && command -v wlr-randr >/dev/null 2>&1; then
    listing=$(wlr-randr 2>/dev/null) || listing=''
    if [[ -n $listing ]]; then
      wlr_mode=$(printf '%s\n' "$listing" | current_mode_of "$connector")
    fi
    if [[ -n $wlr_mode ]]; then
      printf 'displaying %s on %s (current mode from wlr-randr)' "$wlr_mode" "$connector"
      return 0
    fi
  fi
  if [[ -n $current ]]; then
    printf 'displaying %s on %s (mode from sysfs)' "$current" "$connector"
    return 0
  fi
  printf 'status=%s enabled=%s dpms=%s mode=%s' "${status:-unknown}" "${enabled:-unknown}" "${dpms:-unknown}" "${current:-none}"
  return 2
}

# True while the supervised child is running. `kill -0` alone is not enough: an
# exited-but-unreaped child still answers to it, and the observation window
# would then sit out its whole bound before the loop could restart the
# compositor -- 60 s of dark panel where the deployed wrapper needs 1 s.
child_running() {
  local pid=${child_pid:-} stat_line='' state=''
  [[ -n $pid ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ -r /proc/$pid/stat ]] || return 0
  IFS= read -r stat_line <"/proc/$pid/stat" || true
  state=${stat_line##*) }
  [[ ${state%% *} != Z ]]
}

# Poll the connector up to the bound, returning the last verdict. Stops early
# when the supervised child has gone: its exit is the supervised loop's to
# report and restart, not a display verdict's.
wait_for_display() {
  local bound=$1 attempt=0 polls=$(( $1 * 2 + 1 )) seen='' verdict=0
  while :; do
    attempt=$(( attempt + 1 ))
    verdict=0
    seen=$(observe_connector) || verdict=$?
    if (( verdict == 0 )); then
      printf '%s' "$seen"
      return 0
    fi
    if ! child_running; then
      return 4
    fi
    if (( attempt >= polls )); then
      printf '%s' "$seen"
      return "$verdict"
    fi
    sleep 0.5
  done
}

# --- Advisory pre-flight: is another display server holding the DRM device? ---
# kwin_wayland (or any other DRM master) keeps the kiosk compositor from driving
# the display, which shows up as a dark panel and an `Atomic commit failed`
# loop. The browser may legitimately run inside a desktop, so this never blocks:
# it names the holder once and the wrapper carries on.

if [[ $compositor == weston ]] || (( cage_mode )); then
  holders=$(drm_holders)
  if [[ -n $holders ]]; then
    log "warning: another display server is using $drm_device_root/card0 ($holders); the kiosk compositor cannot drive the display while it runs -- disable the desktop session and boot to multi-user.target (docs/kiosk.md section 0)"
  fi
fi

# --- Bounded pre-start wait for the display ------------------------------
# A cold boot can start this unit before the HDMI connector has finished coming
# up, and a compositor that modesets against a connector that is still settling
# is the leading explanation for "unit active, panel dark". One wait, once per
# wrapper run: a restarted Chromium does not need it again.

if (( connect_wait > 0 )); then
  verdict=0
  await_connector "$connect_wait" || verdict=$?
  case $verdict in
    0) ;;
    2) log "warning: no $drm_root/card*-$connector/status to read; skipping the connector wait" ;;
    *) log "warning: connector $connector did not report connected within ${connect_wait}s; starting the compositor anyway" ;;
  esac
fi

# --- Bounded pre-flight ------------------------------------------------
# A boot race would otherwise leave Chromium on ERR_CONNECTION_REFUSED until
# its next reload. Poll once a second up to KIOSK_WAIT_FOR_URL seconds.

if (( wait_for_url > 0 )); then
  if ! command -v curl >/dev/null 2>&1; then
    log 'curl not found: skipping the URL pre-flight'
  else
    log "waiting up to ${wait_for_url}s for $url"
    reachable=0
    deadline=$((SECONDS + wait_for_url))
    while (( SECONDS < deadline )); do
      if curl -fsS -o /dev/null --max-time 2 "$url" 2>/dev/null; then
        reachable=1
        break
      fi
      sleep 1
    done
    if (( reachable )); then
      log 'URL is reachable; starting the browser'
    else
      log "URL not reachable after ${wait_for_url}s; starting anyway"
    fi
  fi
fi

# --- Supervised process ------------------------------------------------
# One argv for every mode. In cage mode `cage -- ...` owns the session and
# exits when its only client exits, so the loop restarts the whole thing.
# With a display mode, cage's client is this POSIX sh snippet: it sets the
# output mode and then `exec`s the browser, so cage still sees a single client
# and the "exit when the only client exits" behaviour is unchanged. No
# `set -e` on purpose: a failing wlr-randr must never stop the browser.
#   $1 mode, $2 output (may be empty), $3 wait seconds, $4 `--`, then the
#   browser argv.
#
# A quoted heredoc rather than a single-quoted string: the snippet is mostly
# awk programs, and escaping every internal `'` as '\'' would make it
# unreadable. `read -d ''` returns non-zero at EOF, hence `|| true` under set -e.
IFS= read -r -d '' MODE_SCRIPT <<'KIOSK_MODE_SCRIPT' || true
mode=$1
output=$2
wait_seconds=$3
shift 4

warn() { printf 'kiosk: warning: %s\n' "$*" >&2; }
info() { printf 'kiosk: %s\n' "$*" >&2; }

# Connector names whose block reports itself enabled, or carries a current mode.
enabled_outputs() {
  awk '
    /^[^ \t]/ { if (header != "" && enabled) print header; header = $1; enabled = 0; next }
    /^[ \t]+Enabled:[ \t]*yes/ { enabled = 1 }
    /^[ \t]+[0-9]+x[0-9]+[ \t]+px/ && /current/ { enabled = 1 }
    END { if (header != "" && enabled) print header }
  '
}

# Advertised modes of one connector, one per line, the current one suffixed " *".
advertised_modes() {
  awk -v want="$1" '
    /^[^ \t]/ { inblock = ($1 == want); next }
    !inblock { next }
    /^[ \t]+[0-9]+x[0-9]+[ \t]+px/ {
      line = $0
      sub(/^[ \t]+/, "", line)
      split(line, field, /[ \t]+/)
      if (line ~ /current/) { print field[1] " *" } else { print field[1] }
    }
  '
}

apply_mode() {
  target_mode=$1
  target_output=$2
  polls=$(( $3 * 4 + 1 ))          # 0.25 s per poll, plus one immediate probe

  attempt=0
  listing=''
  ready=''
  while [ "$attempt" -lt "$polls" ]; do
    attempt=$((attempt + 1))
    listing=$(wlr-randr 2>/dev/null) || listing=''
    if [ -n "$target_output" ]; then
      if printf '%s\n' "$listing" | enabled_outputs | grep -qxF -- "$target_output"; then
        ready=$target_output
      fi
    else
      ready=$(printf '%s\n' "$listing" | enabled_outputs | sed -n 1p)
    fi
    [ -n "$ready" ] && break
    [ "$attempt" -lt "$polls" ] && sleep 0.25
  done

  if [ -z "$ready" ]; then
    if [ -n "$target_output" ]; then
      warn "output $target_output was not reported enabled within ${wait_seconds}s; leaving the display mode alone"
    else
      warn "no enabled Wayland output appeared within ${wait_seconds}s; leaving the display mode alone"
    fi
    return 0
  fi

  modes=$(printf '%s\n' "$listing" | advertised_modes "$ready")
  if [ -z "$modes" ]; then
    warn "output $ready advertised no modes; leaving the display mode alone"
    return 0
  fi
  available=$(printf '%s\n' "$modes" | sed 's/ \*$//')
  current=$(printf '%s\n' "$modes" | sed -n 's/^\(.*\) \*$/\1/p' | sed -n 1p)

  if ! printf '%s\n' "$available" | grep -qxF -- "$target_mode"; then
    warn "output $ready does not offer $target_mode; leaving the display mode alone (available: $(printf '%s\n' "$available" | tr '\n' ' ' | sed 's/ *$//'))"
    return 0
  fi
  if [ "$current" = "$target_mode" ]; then
    info "display mode is already $target_mode on $ready; not touching it"
    return 0
  fi

  # Exactly one attempt. A rejected atomic commit is never re-issued: that is
  # what turned the boot race into a dead panel.
  if ! wlr-randr --output "$ready" --mode "$target_mode"; then
    warn "could not set $target_mode on $ready; continuing at the display's own mode"
    return 0
  fi

  if printf '%s\n' "$(wlr-randr 2>/dev/null)" | advertised_modes "$ready" | grep -qxF -- "$target_mode *"; then
    info "display mode is now $target_mode on $ready"
  else
    warn "output $ready does not report $target_mode as current after setting it; continuing"
  fi
  return 0
}

apply_mode "$mode" "$output" "$wait_seconds"
exec "$@"
KIOSK_MODE_SCRIPT
readonly MODE_SCRIPT

argv=()
if [[ $compositor == weston ]]; then
  # --backend=drm is explicit so a leaked DISPLAY cannot silently select
  # weston's x11 backend. --shell needs the `.so`: see the help text.
  argv=(weston --backend=drm "--config=$weston_ini" --shell=kiosk-shell.so -- "$command_name" "${chromium_flags[@]}" "${browser_args[@]}")
elif (( cage_mode )) && [[ -n $mode ]]; then
  argv=(cage -- sh -c "$MODE_SCRIPT" kiosk "$mode" "$output" "$mode_wait" -- "$command_name" "${chromium_flags[@]}" "${browser_args[@]}")
elif (( cage_mode )); then
  argv=(cage -- "$command_name" "${chromium_flags[@]}" "${browser_args[@]}")
else
  argv=("$command_name" "${chromium_flags[@]}" "${browser_args[@]}")
fi
if [[ $compositor == weston ]]; then
  if [[ -n $mode ]]; then
    log "weston will modeset $connector to $mode as its initial mode (config: $weston_ini)"
  else
    log "weston will use the display's own (EDID) mode (config: $weston_ini)"
  fi
  log "supervising: $(printf '%q ' "${argv[@]}")"
elif [[ -n $mode ]]; then
  log "will set the display mode inside cage: $mode${output:+ on output $output} (waiting up to ${mode_wait}s for the output)"
  # The snippet itself is long, so log a readable equivalent of the argv
  # rather than 1.5 KB of `sh -c` quoting on every restart.
  log "supervising: cage -- sh -c MODE_SCRIPT kiosk $(printf '%q ' "$mode" "$output" "$mode_wait" -- "$command_name" "${chromium_flags[@]}" "${browser_args[@]}")"
else
  log "supervising: $(printf '%q ' "${argv[@]}")"
fi

# The display observation only applies when this wrapper launched a
# compositor: on the desktop image the browser runs against an existing session,
# and there is nothing of ours to observe.
observe_display=0
if (( ready_wait > 0 )); then
  if [[ $compositor == weston ]] || (( cage_mode )); then
    observe_display=1
  else
    log 'no compositor of our own to verify (desktop session); skipping the display readiness check'
  fi
fi

delay=$restart_delay
observed=0
while :; do
  start=$SECONDS
  "${argv[@]}" &
  child_pid=$!

  # Report-only, once per wrapper run, after the first launch. Nothing in this
  # block may tear anything down, skip the wait below, or exit: a display
  # verdict is information, not a gate.
  if (( observed == 0 )) && (( observe_display )); then
    observed=1
    seen=''
    verdict=0
    seen=$(wait_for_display "$ready_wait") || verdict=$?
    case $verdict in
      0) log "kiosk ready: $seen" ;;
      4) ;;   # the compositor has gone; the loop below reports and restarts it
      1) log "warning: $connector reports itself off ($seen) after ${ready_wait}s; leaving the compositor running (docs/kiosk.md section 0 covers the usual cause)" ;;
      *) log "warning: could not confirm the display came up ($seen) within ${ready_wait}s; leaving the compositor running" ;;
    esac
  fi

  status=0
  wait "$child_pid" || status=$?
  child_pid=''
  run=$((SECONDS - start))
  if (( run >= healthy_after )); then
    delay=$restart_delay
  fi
  log "browser exited (status=$status, ran ${run}s); restarting in ${delay}s"
  # Interruptible sleep: SIGTERM interrupts `wait`, so the trap runs at once
  # instead of after the full delay.
  sleep "$delay" &
  sleeper_pid=$!
  wait "$sleeper_pid" || true
  sleeper_pid=''
  delay=$((delay * 2))
  if (( delay > max_restart_delay )); then
    delay=$max_restart_delay
  fi
done
