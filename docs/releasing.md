# Releasing ChimeDeck

A release is one pushed tag. Everything else — the multi-arch images, the wheel, the sdist,
the GitHub release — is produced by `.github/workflows/release.yml` from that tag, and
published only after every check in this repository passes.

This page is how to cut a release and how to consume one. The workflow itself is commented
line by line; read it when you need the *why* behind a step.

---

## 1. When it runs

| Trigger | What happens |
| --- | --- |
| `push` of a tag matching `v*` | A real release: checks, images pushed to GHCR, GitHub release created |
| `workflow_dispatch` on a **tag** ref | The same real release, again. This is how a release that failed half way is retried without deleting and re-pushing the tag |
| `workflow_dispatch` on a **branch** ref | A dry run: every check, the wheel install-and-serve smoke and both image smokes run, but nothing is pushed and no release is created |

**It does not run on every push, and that is a deliberate design decision.** Each run builds
`linux/amd64` and `linux/arm64` for two Docker targets, and the arm64 half is emulated on an
amd64 runner — tens of minutes of CI per commit. While the project is still taking shape, a
per-commit container build would be an expensive status check that everyone learns to ignore.
A per-commit *gate* (lint, types, tests, frontend) is a separate ticket; this workflow is the
release act only.

The dry run is the thing to use while preparing a release: dispatch it on your branch, and
you get the whole pipeline's verdict without touching the registry.

---

## 2. Pre-flight

Work through this list before you tag. Every item exists because skipping it has a specific,
ugly failure mode.

**1. Bump `version` in `pyproject.toml` so it equals the tag.**

```bash
# v0.2.0 in the tag means this line must read 0.2.0
grep '^version' pyproject.toml
```

The workflow compares the two the moment it starts and **fails with both values in the
message** if they disagree. That is not pedantry: the wheel version, the
`org.opencontainers.image.version` label on both images, and the release tag all come from
these two numbers. A mismatch silently ships images labelled `0.1.0` under a `v0.2.0` tag, and
the label is the only thing an operator can inspect after the fact.

**2. Rebuild and commit `web/dist`.**

```bash
cd web && npm ci && npm run build && cd ..
git add web/dist
```

`web/dist` is committed — that is what lets a fresh clone serve the dashboard with no Node.js.
A release packages the *committed* bundle, so a stale one ships last week's UI under this
week's version, and nothing at run time would notice.

**3. Run the frontend checks, which include the stale-dist check.**

```bash
cd web && npm run verify          # ends with check:dist-stale; this is run 3 of the workflow
cd .. && ./scripts/test-integration.sh   # optional: the integration tier, in Docker
```

`check:dist-stale` rebuilds `web/dist` in the working tree and fails if the result differs from
what is committed. That rebuild is the whole point: a check that only compared `git status`
would pass on a bundle nobody had rebuilt, which is the exact bug. It is the automated version
of step 2, and it runs in the workflow on a fresh checkout — so if it is going to fail there,
it will fail locally for the same reason.

**4. Tag and push.**

```bash
git tag v0.2.0
git push origin v0.2.0
```

The tag is what matters. Pushing the tag is pushing the release.

---

## 3. What the run produces

### Image tags

All on the same repository, `ghcr.io/<owner>/chime-deck`:

| Tag | Target | Platforms | Notes |
| --- | --- | --- | --- |
| `<version>` e.g. `0.2.0` | `runtime` | `linux/amd64`, `linux/arm64` | The default image; no local TTS engine |
| `latest` | `runtime` | same | Moved **only** for a plain `X.Y.Z` version, never for a pre-release such as `0.2.0-rc1` |
| `<version>-piper` | `piper` | same | The default image plus `piper-tts==1.8.0` (GPL-3.0-or-later) |
| `piper` | `piper` | same | The floating tag for the same image |

Every one of those is a **manifest list**, not a per-architecture tag: `docker pull` on your
Pi and `docker pull` on your NUC fetch the same tag and get the right architecture. `:piper` is
a shipped deliverable, not a convenience — it carries its own GPL notice in its image labels,
so the release publishes it on every run rather than leaving it to be rebuilt by hand.

Both images carry `org.opencontainers.image.version=<version>`, and the release smoke test
asserts that label **and** `/api/health` both report the version being released.

### Release assets

On the GitHub release page for the tag:

| Asset | What it is |
| --- | --- |
| `chime_deck-<version>-py3-none-any.whl` | The wheel, with the built UI inside at `chime_deck/static` |
| `chime_deck-<version>.tar.gz` | The sdist the wheel was built from |
| `docker-compose.yml` | The compose file from `deploy/`, the consumer-facing way to run the image |
| `SHA256SUMS` | SHA-256 of the wheel and the sdist |

The `Dockerfile` is deliberately **not** attached: it lives in the repository and its content
is fixed by the tag. The compose file *is* attached because it is the file an operator has to
edit (the `image:` line) to run the published image.

---

## 4. The one variable: where images are published

The image path is

```
ghcr.io/${github.repository_owner}/chime-deck      # owner lower-cased
```

so in the repository this project is developed in — owner `Insanemal` — images land at
`ghcr.io/insanemal/chime-deck`. The owner is lower-cased because GHCR rejects upper-case
repository path components outright.

To publish somewhere else — a different registry, or a different org — edit the two values
under `env:` at the top of `.github/workflows/release.yml`:

```yaml
env:
  REGISTRY: ghcr.io
  IMAGE_REPO: chime-deck
```

That is the only place in this repository where an image path is composed. Nothing else
hardcodes an org or a repository URL: HSD-40xt deliberately omitted the
`org.opencontainers.image.source`/`url` labels rather than inventing a URL that might be wrong,
and this workflow keeps that rule. If you fork this project, that `env:` block is your single
edit.

---

## 5. Consuming a release

```bash
docker pull ghcr.io/<owner>/chime-deck:0.2.0
docker run --rm -p 127.0.0.1:8080:8080 \
  -v "$PWD/data:/data" ghcr.io/<owner>/chime-deck:0.2.0
```

The container runs as uid/gid 1000 by default, so `./data` must be owned by uid 1000 on the
host (or rebuild with `--build-arg UID=$(id -u)`). Only `127.0.0.1:8080` is published, on
purpose: the app has no authentication.

The shipped `deploy/docker-compose.yml` **builds from the checkout**, because it cannot know
which owner you will pull from. To use the published image instead, replace `image:` with the
published tag and delete the `build:` block:

```yaml
    image: ghcr.io/<owner>/chime-deck:0.2.0
    # build:
    #   context: ..
    #   ...
```

Everything else in the file — the loopback port binding, the `/dev/snd` passthrough, the
`./data` bind mount and its UID caveat — is unchanged, and
[installing.md](installing.md) §11 explains why each of those is host-specific.

For the `:piper` tag, pull `ghcr.io/<owner>/chime-deck:0.2.0-piper` instead; the licence
notice travels in its labels (`docker image inspect --format '{{json .Config.Labels}}' <ref>`).

---

## 6. What gates the publish

Nothing is published until all of this is green, in this order:

| Job | Proves |
| --- | --- |
| `checks` | `ruff check`, `ruff format --check`, `mypy src`, `pytest` — the same four commands `AGENTS.md` lists |
| `frontend` | `npm run verify` (types, contrast, gallery, storyboard, unit tests, build, bundle checks) and `npm run check:dist-stale` (the committed bundle is the one this source produces) |
| `wheel` | `uv build`, then `chime_deck/static/index.html` is listed inside the wheel, then **an installed copy serves the dashboard**: a clean `uv venv`, install the wheel, boot the server from `site-packages` and assert `/` returns the app shell and `/assets/...` resolves |
| `images` | Both targets build for amd64 **and** arm64, and are pushed |
| `image-smoke` | The published tag is pulled and run: `/api/health` reports the release version, the image label agrees, `/` serves the app shell, the asset URL 200s; on the piper leg, `import piper` works inside the image |

The two smoke scripts are in this repository precisely so you can run the same proof locally
instead of trusting CI:

```bash
uv build
bash scripts/wheel-smoke.sh dist/                          # install-and-serve, ~1 minute
bash scripts/image-smoke.sh ghcr.io/<owner>/chime-deck:0.2.0
EXPECTED_VERSION=0.2.0 REQUIRE_PIPER=1 \
  bash scripts/image-smoke.sh ghcr.io/<owner>/chime-deck:0.2.0-piper
```

Both scripts are bounded: they fail rather than hang, tear down the server or container they
started, and end with `wheel-smoke: OK` / `image-smoke: OK`.

---

## 7. What it does not do

- **No signing.** Nothing is signed with cosign or signed by key; `SHA256SUMS` is a
  convenience, not a trust anchor.
- **No SBOM, no provenance attestation.** The images are not attested and carry no SBOM
  layer.
- **No nightly or edge builds.** Every published image comes from a tag a human pushed.
  There is no `:edge`, `:main` or `:nightly`.
- **No repo autotagging or version bumping.** The workflow reads `pyproject.toml`; it never
  writes it, never commits, and never creates a tag. The tag is the input.
- **No arm64 smoke on real hardware.** The arm64 half of each manifest is built and pushed, but
  the smoke test runs the amd64 image on the amd64 runner. A Pi pulling the arm64 manifest is
  the first time it boots on that architecture.

Release provenance and signable artefacts, if the project wants them, are their own ticket.

> **Gotcha — do not start a workflow file with `---`.** A YAML document marker at the top of
> `.github/workflows/*.yml` is valid YAML (PyYAML and `actionlint` both accept it), but GitHub will
> silently **not register the workflow**: it never appears in the Actions tab or the API, and
> `gh workflow run` answers `HTTP 404: workflow … not found on the default branch`. The file must
> begin with `name:`. This cost an afternoon once.
