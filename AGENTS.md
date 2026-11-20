# AGENTS.md — standing rules for every agent in this repo

Read this before doing anything. These rules apply to the main session, the scheduler, the
orchestrator, and every specialist sub-agent, in every ticket.

Project: **ChimeDeck** — a Linux service (x86_64 + aarch64) that shows Home Assistant
entity status on a local web UI and exposes MQTT chime buttons that play a sound or speak a
TTS phrase on the device's own speaker.

Context lives in `Project_state/` (git-ignored): `architecture.md`, `decisions.md`,
`glossary.md`, `design/01..06`. Read the relevant design doc before planning a ticket.

---

## HARD RULE 1 — ALWAYS INCLUDE A SANE TIMEOUT

**Every command that can block must be bounded.** This is not a style preference; an
unbounded wait has already cost this project a 26-minute stall of an entire ticket batch.

Applies to: servers, `uvicorn`, `pytest`, watchers, `uv`/`pip` installs, network calls,
MQTT/Home Assistant connections, subprocesses, and throwaway experiment scripts in `/tmp`.

Required in **both** places:

1. The bash tool's own `timeout` parameter.
2. A bound inside the command itself:
   - `timeout 20 <command>`
   - a bounded poll: `for i in $(seq 1 100); do ...; sleep 0.1; done`
   - a watchdog: `( timeout 8 bash -c "while kill -0 $PID 2>/dev/null; do sleep 0.1; done" )`

Forbidden, everywhere, no exceptions:

- `wait $PID` on a process with no bound — **this is the exact bug that caused the stall**.
- `while True` with no exit condition.
- An unbounded retry loop or a poll with no attempt cap.
- Starting a background process and relying on it to exit on its own.
- Any script that outlives the tool call it was launched from.

If a command times out, that is a **result**, not an error to be retried forever. Report it,
reduce the scope of the experiment, or change the approach. Never re-run the same unbounded
command hoping it behaves differently.

---

## HARD RULE 2 — Oracle gates on every ticket

Both gates are mandatory, in this order, for every ticket:

1. **Plan gate** — `@oracle` reviews and APPROVES the implementation plan **before any code
   is written**. If it returns changes/rejected, iterate until approved.
2. **Commit gate** — `@oracle` reviews and APPROVES the actual diff **before committing**.
   Never commit without it.

If an oracle dispatch fails with 400/429/5xx, re-dispatch that same gate **once** with a
fallback model and say so in the report. Never switch pre-emptively.

---

## HARD RULE 3 — Delegate, and never run hidden processes

- Discovery → `@explorer`. Implementation → `@fixer`. UI polish → `@designer`.
  External docs → `@librarian`. The orchestrator keeps planning, integration and
  verification.
- Launch agents **only** through the Agent tool. Never `pi` or a wrapper script through bash.
- Never background a headless process to do the work. Those run invisibly and are rejected.
- Verify every specialist's claim yourself. A summary describes intent, not outcome.

---

## HARD RULE 4 — Tests are fully offline

No test may require a live Home Assistant, a live MQTT broker, real audio hardware, real TTS
synthesis, a network call, or a browser download. Use fakes, mocks, `httpx.MockTransport`,
in-process brokers and temporary directories.

`filterwarnings = ["error"]` is on: a warning is a failure.

**Do not mutate this dev host** — there is no passwordless sudo here. When a test genuinely
needs a real system binary (`espeak-ng`, `libmpv`, `ffmpeg`), verify it in the container
harness (`./scripts/test-integration.sh`) or **on the reference ARM board** (the owner will
provide access; it has passwordless sudo, so install packages there freely), as an `integration`-marked
test that **skips cleanly** when the binary is absent. See
`Project_state/design/07-testing.md`.

The ARM board is a legitimate write target for package installs and platform work. This host
is not.

Report results per tier — unit (stubbed), integration (container or ARM board), hardware
(panel) — because "verified" means nothing without saying which.

**Never drive one running instance from two concurrent callers.** An owner acceptance pass was
invalidated because two parallel test batches hit the same server: one enabled quiet hours while
the other ran the dedupe and queue checks, so every press was recorded as
`suppressed_quiet_hours` and the results were meaningless. Either run state-mutating checks
serially against a single instance, or give each its own instance on its own port and data dir.

---

## HARD RULE 5 — Tickets and commits

- Tickets live in `.tickets/`, which is **git-ignored**. Do not try to commit them.
- Use `tk show <id>`, `tk start <id>`, `tk add-note <id> "..."`, `tk close <id>`.
- **One commit per ticket**, with the ticket id in the subject, e.g.
  `HSD-a2bv: scaffold project`.
- Close the ticket only after the commit exists and the suite is green. Add a note with the
  summary and the commit sha.
- Do not commit secrets, `data/`, local databases, or anything from `Project_state/`.

---

## HARD RULE 6 — Stay inside the ticket

Implement what the ticket and its acceptance criteria say. No opportunistic refactors, no
"while I'm here" features, no new dependencies without a reason in the ticket.

Keep the existing suite green at every step. Never regress a previous ticket.



---

## HARD RULE 9 — the record belongs to the owner, not to the agent doing the work

`Project_state/` is **git-ignored private documentation**, not a protected directory. Being untracked
has nothing to do with it; ownership does.

**Batch agents, specialists and orchestrators must NOT edit:**

- `Project_state/` — every file: `design/`, `decisions.md`, `architecture.md`, `glossary.md`,
  `environment.md`, `production.md`, the acceptance docs, the storyboards
- `AGENTS.md` and `.pi/agents/**`
- any ticket's **description** or **acceptance criteria**

**They DO use `.tickets/`** for `tk start`, `tk add-note`, `tk close`. The note is the report, and it
must be complete enough that the owner could redo the work from it.

**Instead of editing documentation**, the agent emits a *"documentation changes required"* section in
its report, mechanical enough to apply without re-deriving its reasoning:

```
file:      Project_state/design/02-mqtt-buttons.md
location:  section 1, topic plan
change:    super_doorbell/<instance>/availability  →  chime_deck/<instance>/availability
reason:    topic prefix renamed with the project
```

The main session applies those changes, or writes a ticket whose deliverable *is* the documentation
update. Such a ticket says so explicitly; nothing else may assume it.

**Why this rule exists, so it is not "simplified" away again:**

1. An agent editing the docs is grading its own homework. The value of the report is that someone
   else decides what the record says.
2. It stops an ADR being retroactively rewritten to justify what was built. Decisions are records of
   what was decided *at the time*, including the bad calls; supersede them, never edit them into
   looking prescient.
3. It stops an acceptance criterion being softened to match the work, when the criterion was the
   argument for the work.
4. It stops documentation drift accumulating in two directions at once.

This rule has been misinterpreted twice — once as "Project_state is read-only" (wrong: the owner may
always write it) and once as "agents should update the docs to match reality" (wrong: they report,
the owner records). Do not restate it more broadly than it is written here.

## HARD RULE 7 — the ARM board is tiny; don't hammer it

The reference panel is 4 cores / 967 MB and runs the doorbell *and* Chromium. Measured audio
start-up on it is **5.1 s**, and it blew a 5 s budget while an agent was concurrently doing an
`rsync`, a `uv sync`, a TLS handshake and two service restarts — i.e. the measurement was
ruined by the measurement.

- One heavy operation at a time (`rsync`, `uv sync`, `pytest`, `npm`, restarts, probes). Space
  them.
- Full `pytest` there is ~7 minutes of pegged CPU. Run it only when the question needs
  aarch64, and with nothing else in flight.
- Never restart its services "to see if that helps" — it disrupts a device someone is using and
  invalidates any timing just taken.
- Prefer reading (journal, API, a file) over running.
- **Never report a performance number taken while you were also loading the box.** If the owner
  is at the panel, ask them what is on screen instead of measuring it.

## HARD RULE 8 — the reference panel is IN PRODUCTION (2026-11-17)

The **reference panel** is a **live appliance**, not a test box. The owner put it into production
and said it is not to be touched without permission. Its address, access details and a full baseline
snapshot are kept in the private notes (`Project_state/production.md`) — that machine's details do not
belong in a public repository.

- **Read-only by default**: `systemctl --user status`, `journalctl --user-unit=…`, `curl` to the
  local API, `cat` of a sysfs file. Those are fine.
- **Anything that mutates requires explicit, specific owner approval**: restarting a unit,
  installing a package, editing `/boot/armbianEnv.txt`, changing systemd config, rebooting,
  deploying code.
- **Never reboot it to test something** unless the owner asked for that reboot.
- **Deployment is a deliberate act.** Verify on the dev host or in a container, then ask. Do not
  deploy as a side effect of finishing a ticket.
- **The deployed tree is not a git checkout and its versions are mixed** (application code from
  `65ce2aa`, kiosk wrapper from `5ccc543`). A naive redeploy reintroduces the wrapper that blanked
  the panel. Pin it deliberately.
- If something breaks, **restore the last known-good state first**, then report. Never iterate on
  a broken live appliance.
- **A desktop session must never be re-enabled on that board** — `kwin_wayland` owning
  `/dev/dri/card0` was the root cause of a day of misdiagnosed display failures (`design/06` §5.1b).

## Verification commands

Run these from the repo root, bounded:

```bash
timeout 120 uv run ruff check .
timeout 120 uv run ruff format --check .
timeout 180 uv run mypy src
timeout 300 uv run pytest
```

Skip a command only if it genuinely cannot apply yet, and say so explicitly in the report.

For anything needing a real system binary, the container harness is the answer:

```bash
timeout 600 ./scripts/test-integration.sh     # once it exists (see the harness ticket)
```

---

## Current phase scope

Unless a ticket says otherwise:

- The frontend (`web/`) does not exist yet. Static serving must degrade gracefully when
  `web/dist` is missing. `web/dist` is **not** git-ignored — it will be committed later.
- No Home Assistant client, MQTT bridge, audio engine or TTS engine until their tickets.
- No Docker, kiosk, systemd or release files until their tickets.
- No authentication (decision D20).
