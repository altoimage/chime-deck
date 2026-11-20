"""Nothing credential-shaped may be committed.

A real Home Assistant long-lived token was nearly committed in a dev-only gallery page: it was
reduced to an `eyJhbGciOi` placeholder before it landed, but the near miss proved that a
convincing-looking dummy is indistinguishable from the real thing by eye, and that a reviewer
scanning for "does this look like a secret" will either miss a real one or panic at a fake.

So the rule is mechanical: no JWT, no PEM block, and no long high-entropy literal in the tree. A
deliberate fake must look fake.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Only files a git checkout would ship. The private notes are git-ignored and not scanned.
SCAN_SUFFIXES = {
    ".py",
    ".ts",
    ".js",
    ".svelte",
    ".json",
    ".md",
    ".yml",
    ".yaml",
    ".toml",
    ".sh",
    ".html",
    ".css",
    ".txt",
    ".example",
}
SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "data",
    "Project_state",
    ".tickets",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "test-results",
}

#: A JWT is three base64url segments. Two or more long segments is the signature of a real one.
# Fragments, not literals: this file must not itself contain credential-shaped text.
JWT = re.compile("e" + "yJ" + r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{10,})?")
PEM = re.compile(r"-{5}BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE" + " KEY-{5}")
# (the two halves are concatenated so this source does not match its own pattern)
#: `token = "…40+ chars…"` and friends: long opaque literals assigned to a secret-sounding name.
OPAQUE = re.compile(
    r"(?i)\b(?:token|secret|password|passwd|api[_-]?key|access[_-]?key)\b\s*[:=]\s*"
    r"['\"]([A-Za-z0-9_\-+/=]{40,})['\"]"
)


def _files() -> list[Path]:
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        found.append(path)
    return found


def test_no_jwt_or_private_key_is_committed() -> None:
    """A JWT-shaped literal or a PEM private key must never appear in a shipped file."""
    offenders = []
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern, label in ((JWT, "JWT"), (PEM, "private key"), (OPAQUE, "opaque secret")):
            for match in pattern.finditer(text):
                where = path.relative_to(ROOT)
                offenders.append(f"{where}: {label} near offset {match.start()}")
    assert not offenders, "credential-shaped content committed:\n  " + "\n  ".join(offenders)


def _jwt_shaped() -> str:
    """Build a token that looks real, without writing one into the source.

    A guard against credential-shaped literals must not contain one — including in pieces, since
    a crude scan matches a single base64 segment too. So the token is encoded from JSON here.
    """
    import base64
    import json

    def segment(payload: dict[str, object]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join(
        (
            segment({"alg": "HS256", "typ": "JWT"}),
            segment({"iss": "test-issuer", "iat": 1_700_000_000}),
            "not-a-real-signature-but-the-right-shape",
        )
    )


def test_the_scanner_would_catch_a_real_token() -> None:
    """A positive control: the scanner must fail on a real-shaped token, or it proves nothing."""
    assert JWT.search(_jwt_shaped()), "the JWT pattern does not match a real-shaped token"
    dummy = "placeholder-not-a-token"
    assert not JWT.search(dummy), "the pattern must not flag an obviously fake placeholder"
