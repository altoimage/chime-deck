#!/usr/bin/env bash
#
# Run the integration tier somewhere the real binaries exist.
#
# Design reference: Project_state/design/07-testing.md
#   §2  the integration container
#   §3  what each target cannot tell you (read this before quoting a result)
#   §6  running the suite on the ARM board
#   §7  the commands
#
# Three modes:
#   --container    x86_64 container, real espeak-ng/libmpv/ffmpeg, --ao=null  (default)
#   --board        the ARM board over ssh: real aarch64, real timing, one HDMI sink
#   --arm64-image  build the image for linux/arm64 under QEMU: correctness only
#
# Every docker/ssh invocation is bounded, so a hung build or a wedged ssh can
# never outlive this script. Exit status 124 means the bound expired: report that
# tier as unverified and do not simply re-run it hoping for a different result.
# `timeout` kills the `docker compose` client, not the container it started, so
# after a 124 check `docker compose -f docker/compose.test.yml ps` for an orphan
# before re-running.
#
# Both the container and the board install the optional piper extra and bake one
# checksum-verified voice model, so the real Piper integration test can run where
# the machine has the RAM.

set -euo pipefail

readonly REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
readonly COMPOSE_FILE="$REPO_ROOT/docker/compose.test.yml"
readonly SERVICE=integration
readonly DEFAULT_TIMEOUT_S=900
readonly ARM64_TIMEOUT_S_DEFAULT=1800

# Both bounds are overridable, because a cold arm64 QEMU build and a warm x86
# test run do not deserve the same number. The rsync bound is overridable too, so
# a slow link does not have to mean a wider bound for the whole run.
readonly TIMEOUT_S="${TIMEOUT_S:-$DEFAULT_TIMEOUT_S}"
readonly ARM64_TIMEOUT_S="${ARM64_TIMEOUT_S:-$ARM64_TIMEOUT_S_DEFAULT}"
readonly RSYNC_TIMEOUT_S="${RSYNC_TIMEOUT_S:-120}"

# The piper integration test reads these. The board tier installs the optional
# extra and bakes one voice model, so the harness owns both flags; `:-` keeps
# `set -u` happy when an operator sets only one of them.
readonly REAL_PIPER="${CHIMEDECK_TEST_REAL_PIPER:-0}"
readonly PIPER_FORCE="${CHIMEDECK_TEST_PIPER_FORCE:-0}"
# The real-sink test makes an audible noise, so it stays off unless the operator
# asks for it by name. The container tier never gets it: a container has no audio
# device, and "played" there still is not "heard" (design 07 §3).
readonly REAL_SINK="${CHIMEDECK_TEST_REAL_SINK:-0}"
readonly PIPER_VOICE=nl_BE-nathalie-x_low
readonly PIPER_FIXTURE_DIR=tests/fixtures/piper-voices

# Board defaults from design 07 §1/§6.
readonly BOARD_HOST="${CHIMEDECK_BOARD_HOST:-}"   # required for --board; there is no default host
readonly BOARD_USER="${CHIMEDECK_BOARD_USER:-mal}"
readonly BOARD_DIR="${CHIMEDECK_BOARD_DIR:-sd-test}"

usage() {
    cat <<USAGE
usage: ./scripts/test-integration.sh [MODE] [--] [pytest args...]

MODE (default: --container)
  --container    build the `integration` target of the root Dockerfile and run
                 the suite in it.
                 x86_64, real espeak-ng, real libmpv, real ffmpeg, playback to
                 --ao=null. So "it plays" means "it decoded and played to a null
                 sink" -- no audible output, no systemd/PipeWire user session,
                 and the container sees the host's RAM. ~1 minute.
  --board        rsync this tree to the board and run the suite there over ssh.
                 Real aarch64, real timing, but a single HDMI sink, so "played"
                 still is not "heard". The real-sink test skips unless
                 CHIMEDECK_TEST_REAL_SINK=1 is set; that run makes a short
                 noise through the board's HDMI sink and proves the sink reached
                 RUNNING, never that anyone heard it. Installs the optional piper
                 extra and bakes one checksum-verified voice model there. Install
                 ffmpeg and libmpv2 on the board for the audio-format tier;
                 without libmpv the real-libmpv tests skip there. The default
                 --board run still skips the piper test (the board is below the
                 D22 threshold) unless CHIMEDECK_TEST_PIPER_FORCE=1.
  --arm64-image  build the root Dockerfile's `integration` target for
                 linux/arm64 with docker buildx build --platform linux/arm64.
                 Correctness only: numbers under QEMU are not performance
                 claims. Now also pulls and bakes the piper wheels and voice
                 model inside ARM64_TIMEOUT_S.
  -h, --help     this text.

The panel/hardware tier (audible output, touch, latency, systemd, kiosk) is out
of scope for this script and is verified by hand on the panel.

The board is the only Python 3.14 tier, and it resolves numpy 2.5.x through the
>=3.12 marker, so its numbers are timing evidence, not a reproduction of the
container's stack.

Timeouts: container/board = ${TIMEOUT_S}s (override with TIMEOUT_S), arm64 image
= ${ARM64_TIMEOUT_S}s (override with ARM64_TIMEOUT_S), rsync = ${RSYNC_TIMEOUT_S}s
(override with RSYNC_TIMEOUT_S). Exit status 124 means the
bound expired -- report that tier as unverified; do not re-run it in a loop.

Anything after the mode flags (or after a bare --) is passed verbatim to pytest:
  ./scripts/test-integration.sh --board -- -k audio_formats -x
USAGE
}

mode=""
while (($# > 0)); do
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        --container | --board | --arm64-image)
            if [[ -n "$mode" ]]; then
                echo "error: only one mode may be given (got $mode and $1)" >&2
                exit 2
            fi
            mode="$1"
            shift
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "error: unknown option: $1" >&2
            echo "       run ./scripts/test-integration.sh --help" >&2
            exit 2
            ;;
        *)
            break
            ;;
    esac
done
if [[ -z "$mode" ]]; then
    mode="--container"
fi

run_container() {
    # The full command is spelled out on purpose. `docker compose run <svc> <cmd>`
    # replaces the image CMD instead of appending to it, so leaning on CMD or
    # entrypoint merging would silently mangle the pytest arguments.
    #
    # -T because the caller may have no TTY; --rm so a failed run leaves nothing
    # behind. The exit code propagates (set -e, and timeout returns 124 when the
    # bound expired).
    echo "==> building $COMPOSE_FILE (bound ${TIMEOUT_S}s)"
    timeout --foreground "$TIMEOUT_S" docker compose -f "$COMPOSE_FILE" build
    echo "==> running the integration suite in the container (bound ${TIMEOUT_S}s)"
    timeout --foreground "$TIMEOUT_S" docker compose -f "$COMPOSE_FILE" run --rm -T \
        "$SERVICE" uv run --no-sync pytest -q "$@"
}

run_board() {
    if [ -z "$BOARD_HOST" ]; then
        echo "error: --board needs a host: set CHIMEDECK_BOARD_HOST=<address-or-hostname>" >&2
        echo "       there is deliberately no default, so this script never addresses someone's" >&2
        echo "       appliance or a placeholder on a network it does not own." >&2
        return 2
    fi
    local target="${BOARD_USER}@${BOARD_HOST}"
    local remote_dir="~/${BOARD_DIR}"
    # The remote work must finish inside the outer ssh bound, so give it a little
    # less room than the ssh call itself has.
    local inner_timeout_s
    if ((TIMEOUT_S > 120)); then
        inner_timeout_s=$((TIMEOUT_S - 60))
    else
        inner_timeout_s="$TIMEOUT_S"
    fi
    local quoted_args
    if (($# > 0)); then
        quoted_args=$(printf '%q ' "$@")
    else
        quoted_args=""
    fi
    # Expanded locally and quoted, so the remote shell receives a plain value
    # even when an operator sets an unusual one (the same pattern as quoted_args).
    local quoted_real_piper
    quoted_real_piper=$(printf '%q' "$REAL_PIPER")
    local quoted_piper_force
    quoted_piper_force=$(printf '%q' "$PIPER_FORCE")
    local quoted_real_sink
    quoted_real_sink=$(printf '%q' "$REAL_SINK")

    echo "==> rsync $REPO_ROOT/ -> ${target}:${remote_dir}/"
    # No --delete on purpose: the board directory is a shared target, and a file
    # deleted locally must not silently disappear from it. The cost is that a
    # *deleted* test can linger there -- a stale file, not a stale claim, and any
    # report from this mode says so.
    timeout "$RSYNC_TIMEOUT_S" rsync -a \
        -e "ssh -o BatchMode=yes -o ConnectTimeout=8" \
        --exclude .git \
        --exclude Project_state \
        --exclude .tickets \
        --exclude .venv \
        --exclude __pycache__ \
        --exclude .pytest_cache \
        --exclude .mypy_cache \
        --exclude .ruff_cache \
        --exclude data \
        --exclude node_modules \
        --exclude tests/fixtures/piper-voices \
        "$REPO_ROOT/" "${target}:${remote_dir}/"

    echo "==> running the integration suite on $target (bound ${TIMEOUT_S}s)"
    # The remote side is bounded too, so a wedged remote process cannot sit there
    # after this script exits. MPV_AO=null keeps the playback tests off any real
    # sink; CHIMEDECK_TEST_REAL_MPV is only set when libmpv is actually there.
    timeout --foreground "$TIMEOUT_S" ssh \
        -o ConnectTimeout=8 \
        -o BatchMode=yes \
        -o ServerAliveInterval=15 \
        "$target" \
        "set -e
         export PATH=\"\$HOME/.local/bin:\$PATH\"
         cd \"\$HOME/${BOARD_DIR}\"
         export MPV_AO=null
         export CHIMEDECK_TEST_REAL_PIPER=${quoted_real_piper}
         export CHIMEDECK_TEST_PIPER_FORCE=${quoted_piper_force}
         export CHIMEDECK_TEST_REAL_SINK=${quoted_real_sink}
         if ldconfig -p | grep -q 'libmpv\.so'; then
             export CHIMEDECK_TEST_REAL_MPV=1
         else
             echo \"note: the board has no libmpv, so the real-libmpv tests will skip there (design 07 §4 rule 4)\"
         fi
         # The explicit sync owns the venv and installs the optional piper extra.
         timeout ${inner_timeout_s} uv sync --frozen --extra piper
         # The model persists on the board because the rsync has no --delete, so
         # --force-redownload plus the checksums make each run deterministic.
         timeout ${inner_timeout_s} uv run --no-sync python -m piper.download_voices ${PIPER_VOICE} --force-redownload --data-dir ${PIPER_FIXTURE_DIR}
         printf '%s  %s\n' 4a00803b60caecad30ea612bcd9f9344 ${PIPER_FIXTURE_DIR}/${PIPER_VOICE}.onnx | md5sum -c -
         printf '%s  %s\n' 5307e9c8398ab02060611bb9c2e0f137 ${PIPER_FIXTURE_DIR}/${PIPER_VOICE}.onnx.json | md5sum -c -
         # --no-sync, not --frozen: the explicit sync above owns the venv, and a
         # reconciling run would uninstall the extra this tier just installed.
         # (No backticks in this block: it is a double-quoted string, so the local
         # shell would treat them as command substitution.)
         timeout ${inner_timeout_s} uv run --no-sync pytest -q ${quoted_args}"
}

run_arm64_image() {
    # Correctness only. QEMU timings say nothing about the board's performance;
    # the board is the honest aarch64 measurement (design 07 §1).
    #
    # qemu-aarch64 binfmt is already registered on the dev host, so this needs
    # nothing installed to work.
    echo "==> building the arm64 image under QEMU (bound ${ARM64_TIMEOUT_S}s)"
    (cd -- "$REPO_ROOT" && timeout --foreground "$ARM64_TIMEOUT_S" docker buildx build \
        --platform linux/arm64 -f Dockerfile --target integration .)
}

case "$mode" in
    --container) run_container "$@" ;;
    --board) run_board "$@" ;;
    --arm64-image)
        if (($# > 0)); then
            echo "error: --arm64-image builds an image and runs nothing; it takes no args" >&2
            exit 2
        fi
        run_arm64_image
        ;;
esac
