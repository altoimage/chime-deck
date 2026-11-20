#!/usr/bin/env bash
#
# Prove that a built wheel really ships the built UI, and that an installed copy
# of that wheel serves the dashboard (not the placeholder page) at "/".
#
# Why this exists (HSD-in8q): pyproject.toml maps web/dist into the wheel at
# chime_deck/static with a hatch force-include. If that mapping breaks, the
# wheel still builds, still installs and still starts -- it merely serves the
# placeholder page in src/chime_deck/app.py, and the unit suite cannot see
# that, because it runs from a checkout where web/dist exists. So the release
# workflow runs this against the very artifact it is about to publish, in a venv
# whose cwd is a temp directory, so nothing from the checkout can be on sys.path.
#
# Every step is bounded on purpose (a wedged network install, a server that never
# binds, or a server that ignores SIGTERM must not be able to hang the run):
#   * INSTALL_TIMEOUT_S bounds `uv venv` and `uv pip install`, both of which
#     reach the network;
#   * the readiness poll is capped BOTH in attempts (100) and in wall-clock
#     seconds (START_TIMEOUT_S), and it aborts the moment the server process is
#     gone, so a crash is reported instead of polled;
#   * every curl carries --max-time;
#   * teardown sends SIGTERM, polls a capped number of 0.1s slices, then SIGKILLs,
#     and reaps the PID only once it is known to be gone -- never a bare `wait`.
# Exit status 124 means a bound expired (normally the outer `timeout` this script
# is invoked under): report the wheel as unverified, do not re-run in a loop.

set -euo pipefail

readonly REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

# 3.11 is the supported floor (pyproject requires-python) and what the Dockerfile
# builds against, so the wheel is installed on the interpreter users get.
readonly PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
readonly INSTALL_TIMEOUT_S="${INSTALL_TIMEOUT_S:-240}"
readonly START_TIMEOUT_S="${START_TIMEOUT_S:-60}"
# Empty means "bind port 0 and ask the kernel for a free ephemeral port".
readonly SMOKE_PORT="${SMOKE_PORT:-}"

# Both bounds are needed: the attempt cap keeps a live-but-silent server from
# being polled for ever, and the wall-clock deadline keeps a slow curl (each one
# may burn its own --max-time 1) from stretching the cap past START_TIMEOUT_S.
readonly READY_ATTEMPTS=100
readonly READY_INTERVAL_S=0.1
# Teardown budget: 50 * 0.1s = 5s for SIGTERM to be honoured before SIGKILL.
readonly TERM_ATTEMPTS=50
readonly TERM_INTERVAL_S=0.1
readonly CURL_TIMEOUT_S=5

usage() {
    cat <<USAGE
usage: ./scripts/wheel-smoke.sh <wheel-file-or-dist-dir>

Install a built wheel into a throwaway venv and prove that
  * the wheel ships chime_deck/static/index.html inside site-packages (not
    from the checkout, which is why the checks run from a temp cwd);
  * an installed copy serves that dashboard at "/" and /api/health reports the
    wheel's own version; and
  * the server log says it served the static UI from site-packages, never the
    placeholder page.

ARGUMENTS
  <wheel-file-or-dist-dir>   a .whl file, or a directory holding exactly one
                             .whl (a bare dist/ after \`uv build\`). Zero or more
                             than one wheel in the directory is an error (exit 2).

ENVIRONMENT
  PYTHON_VERSION=3.11       interpreter the venv is created with.
  INSTALL_TIMEOUT_S=240     bound on \`uv venv\` and on \`uv pip install\` (both
                            are network operations).
  START_TIMEOUT_S=60        wall-clock bound on waiting for /api/health to
                            answer, on top of the hard cap of 100 attempts.
  SMOKE_PORT=               host port for the throwaway server. Empty picks a
                            free ephemeral port; set it only to debug by hand.

EXIT STATUS
  0    the wheel ships the UI and an installed copy served it.
  1    an assertion failed (the message names what the wheel did wrong).
  2    bad usage or the wheel argument does not resolve to exactly one .whl.
  124  a bound expired -- treat the wheel as unverified.
USAGE
}

die() {
    echo "wheel-smoke: FAIL: $*" >&2
    exit 1
}

# --- argument handling -------------------------------------------------------

if (($# != 1)); then
    echo "error: exactly one argument is required (a .whl or a directory of one)" >&2
    usage >&2
    exit 2
fi

case "$1" in
    -h | --help)
        usage
        exit 0
        ;;
esac

wheel_arg="$1"
if [[ -d "$wheel_arg" ]]; then
    wheels=()
    while IFS= read -r candidate; do
        wheels+=("$candidate")
    done < <(find "$wheel_arg" -maxdepth 1 -type f -name '*.whl' -print)
    if ((${#wheels[@]} == 0)); then
        echo "error: no *.whl in $wheel_arg (run 'uv build' first)" >&2
        exit 2
    fi
    if ((${#wheels[@]} > 1)); then
        echo "error: $wheel_arg holds ${#wheels[@]} wheels; name the one to smoke:" >&2
        printf '       %s\n' "${wheels[@]}" >&2
        exit 2
    fi
    wheel="${wheels[0]}"
elif [[ -f "$wheel_arg" ]]; then
    wheel="$wheel_arg"
else
    echo "error: no such wheel or directory: $wheel_arg" >&2
    exit 2
fi
wheel=$(cd -- "$(dirname -- "$wheel")" && pwd)/$(basename -- "$wheel")

# --- teardown ----------------------------------------------------------------
# Safe to run when nothing was started: server_pid stays empty until step 4.

workdir=$(mktemp -d)
server_pid=""
# "term" when SIGTERM was enough, "kill" when it had to be escalated. Reported
# rather than assumed, so a shutdown regression cannot hide behind a PASS line.
server_stopped_by=""

stop_server() {
    local attempt
    [[ -n "$server_pid" ]] || return 0
    if kill -0 "$server_pid" 2>/dev/null; then
        kill -TERM "$server_pid" 2>/dev/null || true
        for ((attempt = 0; attempt < TERM_ATTEMPTS; attempt++)); do
            kill -0 "$server_pid" 2>/dev/null || break
            sleep "$TERM_INTERVAL_S"
        done
        if kill -0 "$server_pid" 2>/dev/null; then
            server_stopped_by="kill"
            kill -KILL "$server_pid" 2>/dev/null || true
            for ((attempt = 0; attempt < TERM_ATTEMPTS; attempt++)); do
                kill -0 "$server_pid" 2>/dev/null || break
                sleep "$TERM_INTERVAL_S"
            done
        else
            server_stopped_by="term"
        fi
    fi
    # Only reap once the process is known to be gone, so this can never block.
    if ! kill -0 "$server_pid" 2>/dev/null; then
        wait "$server_pid" 2>/dev/null || true
    fi
}

cleanup() {
    # Stop the server and drop the temp tree, but print nothing on success: the
    # last line of a passing run has to be `wheel-smoke: OK`.
    trap - EXIT
    set +e
    stop_server
    rm -rf -- "$workdir"
}

trap cleanup EXIT

# --- 1. a clean venv installed from the wheel --------------------------------

echo "==> wheel:    $wheel"
echo "==> sha256:   $(sha256sum -- "$wheel" | cut -d' ' -f1)"
echo "==> venv:     $workdir/venv (python $PYTHON_VERSION, bound ${INSTALL_TIMEOUT_S}s)"
timeout "$INSTALL_TIMEOUT_S" uv venv --python "$PYTHON_VERSION" "$workdir/venv"
echo "==> installing the wheel (bound ${INSTALL_TIMEOUT_S}s)"
timeout "$INSTALL_TIMEOUT_S" uv pip install \
    --python "$workdir/venv/bin/python" --no-progress "$wheel"
venv_python="$workdir/venv/bin/python"
venv_script="$workdir/venv/bin/chime-deck"
[[ -x "$venv_script" ]] || die "the wheel did not install a chime-deck script at $venv_script"

# --- 2. the wheel ships the UI ----------------------------------------------
# Run from the temp workdir with stdin as the program, so neither the checkout
# nor its src/ can appear on sys.path: the only chime_deck importable is the
# installed one, which is what makes this an artifact check rather than a
# checkout check.

echo "==> checking that the wheel ships chime_deck/static"
(
    cd "$workdir"
    SMOKE_REPO_ROOT="$REPO_ROOT" "$venv_python" - <<'PY'
import importlib.metadata
import importlib.resources
import os
import pathlib
import sys

import chime_deck  # noqa: F401  (import proves the package resolves at all)

repo_root = pathlib.Path(os.environ["SMOKE_REPO_ROOT"]).resolve()
static = pathlib.Path(str(importlib.resources.files("chime_deck"))) / "static"
index = static / "index.html"
problems = []

if not index.is_file():
    problems.append(f"{index} is missing")
elif index.stat().st_size <= 500:
    problems.append(f"{index} is only {index.stat().st_size} bytes, so it is not the built UI")

if "site-packages" not in str(static):
    problems.append(f"{static} is not under site-packages, so it is not a packaged copy")

if static == repo_root or repo_root in static.parents:
    problems.append(f"{static} is inside the checkout at {repo_root}, so nothing was proven")

assets_dir = static / "assets"
if not assets_dir.is_dir() or not any(assets_dir.iterdir()):
    problems.append(f"{assets_dir} is missing or empty, so the JS/CSS bundle did not ship")

if problems:
    print("static-check: FAIL", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(
        "  The packaging defect is back: the wheel is not carrying web/dist at\n"
        "  chime_deck/static. Check pyproject.toml's\n"
        "  [tool.hatch.build.targets.wheel.force-include] mapping and that web/dist\n"
        "  exists before the build runs.",
        file=sys.stderr,
    )
    sys.exit(1)

print(f"STATIC_OK={static}")
print(f"VERSION={importlib.metadata.version('chime-deck')}")
PY
) >"$workdir/static-check.txt" || die "the wheel does not ship the built UI (see above)"

static_path=$(sed -n 's/^STATIC_OK=//p' "$workdir/static-check.txt")
wheel_version=$(sed -n 's/^VERSION=//p' "$workdir/static-check.txt")
[[ -n "$static_path" ]] || die "the static check printed no STATIC_OK line"
[[ -n "$wheel_version" ]] || die "the static check printed no VERSION line"
echo "PASS  the wheel ships chime_deck/static/index.html (from site-packages, not the checkout)"
echo "      static:  $static_path"
echo "      version: $wheel_version"

# --- 3. start the server from the installed copy -----------------------------

if [[ -n "$SMOKE_PORT" ]]; then
    port="$SMOKE_PORT"
else
    port=$("$venv_python" - <<'PY'
import socket

# Bind port 0 and let the kernel choose; the tiny race between closing this
# socket and the server binding is acceptable for a smoke test.
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)
fi
[[ "$port" =~ ^[0-9]+$ ]] || die "could not determine a port to bind (got '$port')"

echo "==> starting the installed server on 127.0.0.1:$port (cwd $workdir)"
(
    cd "$workdir"
    CHIMEDECK_DATA_DIR="$workdir/data" \
        CHIMEDECK_HOST=127.0.0.1 \
        CHIMEDECK_PORT="$port" \
        "$venv_script"
) >"$workdir/server.log" 2>&1 &
server_pid=$!

# --- 4. bounded readiness poll ----------------------------------------------
# kill -0 first: a server that died at startup (bad import, occupied port) must
# be reported from its log instead of being polled for the full budget.

echo "==> waiting for /api/health (max $READY_ATTEMPTS attempts, ${START_TIMEOUT_S}s wall clock)"
ready=0
deadline=$((SECONDS + START_TIMEOUT_S))
for ((attempt = 1; attempt <= READY_ATTEMPTS; attempt++)); do
    if ! kill -0 "$server_pid" 2>/dev/null; then
        echo "---- server.log ----" >&2
        cat "$workdir/server.log" >&2 || true
        die "the server exited before answering /api/health"
    fi
    if curl -fsS --max-time 1 "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then
        ready=1
        break
    fi
    if ((SECONDS >= deadline)); then
        break
    fi
    sleep "$READY_INTERVAL_S"
done
if ((ready == 0)); then
    echo "---- server.log (tail) ----" >&2
    tail -n 40 "$workdir/server.log" >&2 || true
    die "/api/health did not answer within ${START_TIMEOUT_S}s"
fi
echo "PASS  the installed server answers /api/health"

# --- 5. what the server actually serves --------------------------------------

fetch() {
    # fetch <url> <output-file>: echoes the HTTP status, body lands in the file.
    curl -sS --max-time "$CURL_TIMEOUT_S" -o "$2" -w '%{http_code}' "$1"
}

health_status=$(fetch "http://127.0.0.1:$port/api/health" "$workdir/health.json")
[[ "$health_status" == "200" ]] || die "/api/health returned HTTP $health_status, expected 200"

# Parsed rather than grepped: JSON whitespace and key order are not a contract.
if ! "$venv_python" - "$workdir/health.json" "$wheel_version" <<'PY'
import json
import sys

path, expected = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("status") != "ok":
    print(f"  status is {payload.get('status')!r}, expected 'ok'", file=sys.stderr)
    sys.exit(1)
if payload.get("version") != expected:
    print(
        f"  version is {payload.get('version')!r}, expected the wheel's {expected!r}",
        file=sys.stderr,
    )
    sys.exit(1)
PY
then
    die "/api/health did not report the wheel's version"
fi
echo "PASS  /api/health is ok version=$wheel_version"

index_status=$(fetch "http://127.0.0.1:$port/" "$workdir/index.html")
[[ "$index_status" == "200" ]] || die "/ returned HTTP $index_status, expected 200"
grep -q 'id="app"' "$workdir/index.html" ||
    die '/ does not contain the app mount point id="app"'
grep -q '/assets/' "$workdir/index.html" || die "/ references no /assets/ bundle"
# The placeholder sentence in src/chime_deck/app.py is only rendered when no
# built UI was found, so a match here is the packaging defect, not a style nit.
if grep -q 'has not been built yet' "$workdir/index.html"; then
    die "/ served the placeholder page: the wheel shipped no UI (web/dist -> chime_deck/static)"
fi
echo "PASS  / serves the dashboard markup (id=\"app\", /assets/ reference, no placeholder)"

asset_path=$(grep -o '/assets/[A-Za-z0-9._/-]*' "$workdir/index.html" | head -n 1)
[[ -n "$asset_path" ]] || die "/ has no /assets/ reference to fetch"
asset_status=$(fetch "http://127.0.0.1:$port$asset_path" "$workdir/asset.bin")
[[ "$asset_status" == "200" ]] || die "$asset_path returned HTTP $asset_status, expected 200"
asset_bytes=$(wc -c <"$workdir/asset.bin")
((asset_bytes > 0)) || die "$asset_path returned an empty body"
echo "PASS  $asset_path is served ($asset_bytes bytes)"

# The log line is the unambiguous proof of WHICH copy served the page: the
# packaged path, not a stray web/dist left in the checkout.
if grep -q 'serving placeholder page' "$workdir/server.log"; then
    die "the server logged 'serving placeholder page', so no packaged UI was found"
fi
grep -q 'serving static UI from' "$workdir/server.log" ||
    die "the server log has no 'serving static UI from' line"
grep 'serving static UI from' "$workdir/server.log" |
    grep -q 'site-packages/chime_deck/static' ||
    die "the server did not serve from site-packages/chime_deck/static"
echo "PASS  the server log says it served static UI from site-packages/chime_deck/static"

# --- 6. bounded teardown ----------------------------------------------------

echo "==> stopping the server (SIGTERM, then a capped poll, then SIGKILL)"
stop_server
server_pid=""
case "$server_stopped_by" in
    term) echo "PASS  the server stopped on SIGTERM within 5s" ;;
    kill) die "the server ignored SIGTERM for 5s and had to be SIGKILLed" ;;
    *) die "the server was already gone at teardown, so shutdown was not proven" ;;
esac

echo "wheel-smoke: OK"
