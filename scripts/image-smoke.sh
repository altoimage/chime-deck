#!/usr/bin/env bash
#
# Prove that a published container image really serves the dashboard, and that
# its labels and /api/health carry the version it is published under.
#
# Why this exists (HSD-in8q): the image builds the UI from web/ at build time and
# installs the project with `uv sync --no-editable`, so the dashboard at "/" and
# the `org.opencontainers.image.version` label are both build-time facts that the
# integration tier cannot check -- it runs from a bind-mounted checkout. The
# release workflow runs this against the image it has just built or pulled, so
# "every published image is labelled `dev`" and "the wheel lost web/dist again"
# both fail the release instead of reaching users.
#
# Every step is bounded on purpose:
#   * docker run/rm/inspect all go through `timeout`, so a wedged daemon cannot
#     hang the release;
#   * the readiness poll is capped BOTH in attempts (60) and in wall-clock
#     seconds (START_TIMEOUT_S), and it aborts the moment the container stops, so
#     a crash is reported from `docker logs` instead of polled;
#   * every curl carries --max-time;
#   * the EXIT trap always runs `docker rm -f` and then verifies with
#     `docker inspect` that the container is gone -- including on failure.
# Exit status 124 means a bound expired: report the image as unverified and
# investigate, do not re-run in a loop hoping for a different answer.
#
# The port is published on 127.0.0.1 only: the app has no authentication (D20),
# so a smoke container must never be reachable from the network.

set -euo pipefail

# Set by the release workflow to the tag being published. Empty means "do not
# check versions", which is what a local build of :dev wants.
readonly EXPECTED_VERSION="${EXPECTED_VERSION:-}"
# 1 asserts `import piper` works inside the image, so the :piper tag cannot be
# published hollow.
readonly REQUIRE_PIPER="${REQUIRE_PIPER:-0}"
readonly START_TIMEOUT_S="${START_TIMEOUT_S:-120}"
# Empty means "ask the kernel for a free ephemeral host port".
readonly SMOKE_PORT="${SMOKE_PORT:-}"

readonly RUN_TIMEOUT_S=120
readonly RM_TIMEOUT_S=60
readonly VERSION_LABEL=org.opencontainers.image.version
# The image's own listen port (EXPOSE 8080, CHIMEDECK_HOST=0.0.0.0).
readonly CONTAINER_PORT=8080

# Both bounds are needed: the attempt cap keeps a running-but-silent container
# from being polled for ever, and the wall-clock deadline keeps slow curls (each
# may burn its own --max-time 1) from stretching the cap past START_TIMEOUT_S.
readonly READY_ATTEMPTS=60
readonly READY_INTERVAL_S=0.5
readonly CURL_TIMEOUT_S=5
readonly MIN_INDEX_BYTES=500

readonly CONTAINER_NAME="chime-deck-smoke-$$"
# Holds the fetched bodies; created up front so the single EXIT trap owns it.
tmpdir=$(mktemp -d)

usage() {
    cat <<USAGE
usage: ./scripts/image-smoke.sh <image-ref>

Run a container from <image-ref> on a loopback-only port and prove that
  * /api/health answers with status "ok";
  * "/" serves the dashboard (id="app", an /assets/ bundle that fetches, and NOT
    the "has not been built yet" placeholder page); and
  * when EXPECTED_VERSION is set, both /api/health's version and the image's
    ${VERSION_LABEL} label equal it.

The container is always removed, including on failure, and is verified gone.

ARGUMENTS
  <image-ref>                 a tag that exists locally or is pullable
                              (docker run pulls it).

ENVIRONMENT
  EXPECTED_VERSION=           when set, assert the label and /api/health version
                              equal it. The release workflow passes the release
                              tag.
  REQUIRE_PIPER=0             when 1, also assert \`import piper\` works inside
                              the image (checks the :piper tag is not hollow).
  START_TIMEOUT_S=120         wall-clock bound on waiting for /api/health, on top
                              of the hard cap of 60 attempts.
  SMOKE_PORT=                 loopback host port to publish. Empty picks a free
                              ephemeral port.

EXIT STATUS
  0    the image serves the dashboard (and matches EXPECTED_VERSION).
  1    an assertion failed (the message names what the image did wrong).
  2    bad usage.
  124  a bound expired -- treat the image as unverified.
USAGE
}

die() {
    echo "image-smoke: FAIL: $*" >&2
    exit 1
}

# --- argument handling -------------------------------------------------------

if (($# != 1)); then
    echo "error: exactly one argument is required (an image reference)" >&2
    usage >&2
    exit 2
fi

case "$1" in
    -h | --help)
        usage
        exit 0
        ;;
esac

readonly image="$1"

# --- teardown ----------------------------------------------------------------
# Runs on every exit path, including a failed assertion, and is safe to run when
# no container was ever created.

cleanup() {
    trap - EXIT
    set +e
    rm -rf -- "$tmpdir"
    echo "==> removing container $CONTAINER_NAME (bound ${RM_TIMEOUT_S}s)"
    timeout "$RM_TIMEOUT_S" docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1
    # Verify rather than trust the rm: a leaked container on a CI runner is a
    # published-image check that quietly tested the wrong thing next time.
    local attempt
    for ((attempt = 0; attempt < 20; attempt++)); do
        if ! timeout "$RM_TIMEOUT_S" docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.25
    done
    echo "image-smoke: WARNING: container $CONTAINER_NAME is still present" >&2
}

trap cleanup EXIT

# --- 1. a loopback-only port and a detached container ------------------------

if [[ -n "$SMOKE_PORT" ]]; then
    port="$SMOKE_PORT"
else
    port=$(python3 - <<'PY'
import socket

# Bind port 0 and let the kernel choose; the race between closing this socket and
# docker publishing the port is acceptable for a smoke test.
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)
fi
[[ "$port" =~ ^[0-9]+$ ]] || die "could not determine a host port to publish (got '$port')"

echo "==> image:     $image"
echo "==> container: $CONTAINER_NAME"
echo "==> port:      127.0.0.1:$port -> $CONTAINER_PORT (bound ${RUN_TIMEOUT_S}s)"
timeout "$RUN_TIMEOUT_S" docker run -d --name "$CONTAINER_NAME" \
    -p "127.0.0.1:$port:$CONTAINER_PORT" "$image" >/dev/null ||
    die "docker run failed for $image"

# --- 2. bounded readiness poll ----------------------------------------------
# Inspect first: a container that exited during startup (missing libmpv, bad
# config) must be reported from its logs instead of being polled for the budget.

echo "==> waiting for /api/health (max $READY_ATTEMPTS attempts, ${START_TIMEOUT_S}s wall clock)"
ready=0
deadline=$((SECONDS + START_TIMEOUT_S))
for ((attempt = 1; attempt <= READY_ATTEMPTS; attempt++)); do
    running=$(timeout "$RM_TIMEOUT_S" docker inspect -f '{{.State.Running}}' \
        "$CONTAINER_NAME" 2>/dev/null || true)
    if [[ "$running" != "true" ]]; then
        echo "---- docker logs ----" >&2
        timeout "$RM_TIMEOUT_S" docker logs "$CONTAINER_NAME" >&2 2>&1 || true
        die "the container stopped before answering /api/health"
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
    echo "---- docker logs (tail) ----" >&2
    timeout "$RM_TIMEOUT_S" docker logs --tail 40 "$CONTAINER_NAME" >&2 2>&1 || true
    die "/api/health did not answer within ${START_TIMEOUT_S}s"
fi
echo "PASS  the container answers /api/health"

# --- 3. what the container actually serves -----------------------------------

fetch() {
    # fetch <url> <output-file>: echoes the HTTP status, body lands in the file.
    curl -sS --max-time "$CURL_TIMEOUT_S" -o "$2" -w '%{http_code}' "$1"
}

health_status=$(fetch "http://127.0.0.1:$port/api/health" "$tmpdir/health.json")
[[ "$health_status" == "200" ]] || die "/api/health returned HTTP $health_status, expected 200"

# Parsed rather than grepped: JSON whitespace and key order are not a contract.
if ! python3 - "$tmpdir/health.json" "${EXPECTED_VERSION:-}" <<'PY'
import json
import sys

path, expected = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("status") != "ok":
    print(f"  status is {payload.get('status')!r}, expected 'ok'", file=sys.stderr)
    sys.exit(1)
if expected and payload.get("version") != expected:
    print(
        f"  /api/health reports version {payload.get('version')!r}, expected {expected!r}.\n"
        "  The release build must pass --build-arg CHIMEDECK_VERSION=$EXPECTED_VERSION.",
        file=sys.stderr,
    )
    sys.exit(1)
PY
then
    die "/api/health is not ok or does not report EXPECTED_VERSION"
fi
echo "PASS  /api/health is ok${EXPECTED_VERSION:+ version=$EXPECTED_VERSION}"

index_status=$(fetch "http://127.0.0.1:$port/" "$tmpdir/index.html")
[[ "$index_status" == "200" ]] || die "/ returned HTTP $index_status, expected 200"
(( $(wc -c <"$tmpdir/index.html") > MIN_INDEX_BYTES )) ||
    die "/ returned less than ${MIN_INDEX_BYTES} bytes, so it is not the built UI"
grep -q 'id="app"' "$tmpdir/index.html" || die "/ does not contain the app mount point id=\"app\""
grep -q '/assets/' "$tmpdir/index.html" || die "/ references no /assets/ bundle"
# The placeholder sentence in src/chime_deck/app.py is only rendered when no
# built UI was found, so a match here means the image shipped no UI.
if grep -q 'has not been built yet' "$tmpdir/index.html"; then
    die "/ served the placeholder page: the image shipped no UI (web/dist -> chime_deck/static)"
fi
echo "PASS  / serves the dashboard markup (id=\"app\", /assets/ reference, no placeholder)"

asset_path=$(grep -o '/assets/[A-Za-z0-9._/-]*' "$tmpdir/index.html" | head -n 1)
[[ -n "$asset_path" ]] || die "/ has no /assets/ reference to fetch"
asset_status=$(fetch "http://127.0.0.1:$port$asset_path" "$tmpdir/asset.bin")
[[ "$asset_status" == "200" ]] || die "$asset_path returned HTTP $asset_status, expected 200"
asset_bytes=$(wc -c <"$tmpdir/asset.bin")
((asset_bytes > 0)) || die "$asset_path returned an empty body"
echo "PASS  $asset_path is served ($asset_bytes bytes)"

# --- 4. the version label, when a version was promised ----------------------

if [[ -n "$EXPECTED_VERSION" ]]; then
    label_version=$(timeout "$RM_TIMEOUT_S" docker image inspect \
        --format "{{index .Config.Labels \"$VERSION_LABEL\"}}" "$image" 2>/dev/null || true)
    if [[ "$label_version" != "$EXPECTED_VERSION" ]]; then
        echo "  the image label $VERSION_LABEL is '$label_version'" >&2
        echo "  expected '$EXPECTED_VERSION' -- the release build must pass" >&2
        echo "  --build-arg CHIMEDECK_VERSION=\$EXPECTED_VERSION" >&2
        die "the image is not labelled with the version it is published under"
    fi
    echo "PASS  the $VERSION_LABEL label is $EXPECTED_VERSION"
fi

# --- 5. the optional piper tag is not hollow --------------------------------

if [[ "$REQUIRE_PIPER" == "1" ]]; then
    echo "==> checking that the image can import piper (bound ${RUN_TIMEOUT_S}s)"
    # The image puts its venv on PATH, so `python` is the venv interpreter.
    # The version comes from the distribution metadata, not piper.__version__:
    # piper-tts 1.8.0's package __init__ defines no __version__ attribute.
    timeout "$RUN_TIMEOUT_S" docker run --rm "$image" python -c \
        "import importlib.metadata as m, piper; print('piper', m.version('piper-tts'), 'at', piper.__file__)" ||
        die "REQUIRE_PIPER=1 but 'import piper' failed inside $image"
    echo "PASS  the image can import piper"
fi

echo "image-smoke: OK"
