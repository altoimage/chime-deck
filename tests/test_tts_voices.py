"""Voice catalogue, downloader and job registry: every byte is injected.

Design 07 separates three tiers. These are **unit-tier** tests: an
``httpx.MockTransport`` serves every byte, ``tmp_path`` holds the voices
directory and a fake settings object answers the D22 threshold, so nothing
probes the ambient environment. The same tests therefore pass on a host without
the ``piper`` extra, in a container that has it, and on the ARM board.

The curated table is also checked against
``tests/golden/piper_voices_v1_0_0.json``, so an edited row cannot silently
claim an unverified model.

Owning ticket: HSD-p50k.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Final

import httpx
import pytest
import pytest_asyncio

from chime_deck.store import SETTINGS
from chime_deck.tts import Availability, PiperProbe
from chime_deck.tts import voices as voices_module
from chime_deck.tts.errors import VoiceError, VoiceErrorCode
from chime_deck.tts.piper import (
    DEFAULT_PIPER_MIN_RAM_MB,
    PIPER_MIN_RAM_SETTING,
    VOICE_ID_PATTERN,
)
from chime_deck.tts.voices import (
    DOWNLOAD_TIMEOUT_S,
    MAX_VOICE_BYTES,
    MIN_ONNX_BYTES,
    REDIRECT_LIMIT,
    STALE_TEMP_AGE_S,
    SUGGESTED_VOICES,
    VOICE_REPO_ROOT,
    InstalledVoice,
    SuggestedVoice,
    VoiceDownloadJob,
    VoiceDownloadRegistry,
    VoiceManager,
    VoiceState,
    install_url,
)

pytestmark = pytest.mark.asyncio

VOICE: Final = "en_GB-test-low"
"""A voice id that matches :data:`VOICE_ID_PATTERN` and needs no real model."""

GOLDEN_PATH: Final = Path(__file__).parent / "golden" / "piper_voices_v1_0_0.json"

BLOCK_CAP_S: Final = 5.0
"""Longest any blocking handler or poll may wait, so nothing outlives the suite."""

PLENTY_MB: Final = 4096
TOO_LITTLE_MB: Final = 900

MODEL_BYTES: Final = b"\x08" * (MIN_ONNX_BYTES + 4096)
"""A model body over the plausibility floor whose head is not HTML or JSON."""

CONFIG_DOCUMENT: Final[dict[str, object]] = {
    "audio": {"sample_rate": 16000},
    "espeak": {"voice": "en-gb", "id": "en-gb"},
    "num_symbols": 256,
    "num_speakers": 1,
    "phoneme_id_map": {"_": [0], " ": [1]},
}
"""Exactly the keys ``PiperConfig.from_dict`` indexes in piper-tts 1.8.0."""

CONFIG_BYTES: Final = json.dumps(CONFIG_DOCUMENT).encode("utf-8")

Handler = (
    Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]
)


def _md5(data: bytes) -> str:
    """Return the md5 hex digest of *data*.

    :param data: bytes to hash.
    """
    return hashlib.md5(data).hexdigest()


def _write_meminfo(path: Path, total_mb: int) -> Path:
    """Write a meminfo file the way Linux does, in kB.

    :param path: file to write.
    :param total_mb: value of ``MemTotal`` in megabytes.
    """
    path.write_text(
        f"MemTotal:       {total_mb * 1024} kB\n"
        f"MemFree:        {total_mb * 1024} kB\n"
        f"MemAvailable:   {total_mb * 1024} kB\n",
        encoding="utf-8",
    )
    return path


def _assert_whole_sentence(sentence: str) -> None:
    """Assert *sentence* reads as one actionable house-style sentence.

    :param sentence: message a user would see.
    """
    assert sentence == sentence.strip()
    assert len(sentence) > 30
    assert " " in sentence
    assert not sentence.endswith(".")


def _assert_nothing_published(tmp_path: Path, voices_dir: Path) -> None:
    """Assert no model, config or temporary directory survived a refusal.

    :param tmp_path: pytest temporary directory.
    :param voices_dir: directory the manager was pointed at.
    """
    assert list(tmp_path.rglob("*.onnx")) == []
    assert list(tmp_path.rglob("*.onnx.json")) == []
    assert list(tmp_path.rglob("*.tmp")) == []
    if voices_dir.exists():
        assert [entry.name for entry in voices_dir.iterdir()] == []


class FakeSettings:
    """A settings reader that answers from a dict, like the real store."""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        """Create the reader.

        :param values: overrides; anything else answers its registry default.
        """
        self.values: dict[str, object] = dict(values or {})

    async def get_setting(self, key: str) -> object:
        """Return the override for *key*, or its registry default.

        :param key: registered setting key.
        """
        if key in self.values:
            return self.values[key]
        return SETTINGS[key].default


class AllowAllProbe(PiperProbe):
    """A probe whose environment gate always passes, for the non-gate tests.

    Every other test would answer "yes" from the ambient host, which is exactly
    what these unit tests must not depend on.
    """

    def __init__(self) -> None:
        """Build a probe over a fake settings reader; nothing is read."""
        super().__init__(FakeSettings())

    async def installable(self) -> Availability:
        """Answer that a voice model could be installed here."""
        return Availability(True)


@dataclass(frozen=True, slots=True)
class Catalogue:
    """A one-voice catalogue the manager and the test transport agree on."""

    voice: SuggestedVoice
    model: bytes
    config: bytes

    @property
    def total(self) -> int:
        """Bytes the table promises for both files together."""
        return len(self.model) + len(self.config)


CatalogueFactory = Callable[..., Catalogue]


@pytest.fixture
def catalogue(monkeypatch: pytest.MonkeyPatch) -> CatalogueFactory:
    """Return a factory that installs a small, digest-correct voice row.

    :param monkeypatch: patcher that replaces the shipped table for one test.
    """

    def build(
        *,
        model: bytes = MODEL_BYTES,
        config: bytes = CONFIG_BYTES,
        voice_id: str = VOICE,
    ) -> Catalogue:
        """Patch the module table and describe the served bytes.

        :param model: body served for the ``.onnx`` request.
        :param config: body served for the ``.onnx.json`` request.
        :param voice_id: curated id the row carries.
        """
        voice = SuggestedVoice(
            id=voice_id,
            language="en_GB",
            quality="low",
            sample_rate_hz=16000,
            num_speakers=1,
            licence="CC0",
            licence_url="https://example.test/licence",
            file_onnx=f"en/en_GB/test/low/{voice_id}.onnx",
            onnx_bytes=len(model),
            onnx_md5=_md5(model),
            file_config=f"en/en_GB/test/low/{voice_id}.onnx.json",
            config_bytes=len(config),
            config_md5=_md5(config),
            file_model_card="en/en_GB/test/low/MODEL_CARD",
            model_card_bytes=1,
            model_card_md5="0" * 32,
        )
        built = Catalogue(voice=voice, model=model, config=config)
        monkeypatch.setattr(voices_module, "SUGGESTED_VOICES", (voice,))
        monkeypatch.setattr(voices_module, "_SUGGESTED_BY_ID", {voice_id: voice})
        return built

    return build


@pytest_asyncio.fixture
async def clients() -> AsyncIterator[list[httpx.AsyncClient]]:
    """Yield a list that tracks borrowed clients and closes them at teardown."""
    created: list[httpx.AsyncClient] = []
    yield created
    for client in created:
        await client.aclose()


def _client(handler: Handler, sink: list[httpx.AsyncClient]) -> httpx.AsyncClient:
    """Create a mock-transport client with the repository's default redirects.

    :param handler: MockTransport handler.
    :param sink: fixture list that closes clients after the test.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink.append(client)
    return client


def _manager(
    voices_dir: Path,
    *,
    probe: PiperProbe | None = None,
    client: httpx.AsyncClient | None = None,
    max_bytes: int = MAX_VOICE_BYTES,
) -> VoiceManager:
    """Create a manager over the fake gate, or the probe a gate test built.

    :param voices_dir: directory voice models are published into.
    :param probe: environment gate; ``None`` builds :class:`AllowAllProbe`.
    :param client: borrowed HTTP client, or ``None`` for an owned one.
    :param max_bytes: per-file byte cap.
    """
    return VoiceManager(
        AllowAllProbe() if probe is None else probe,
        voices_dir,
        client=client,
        timeout_s=DOWNLOAD_TIMEOUT_S,
        max_bytes=max_bytes,
    )


def _serve(built: Catalogue) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler that serves *built*'s two files.

    :param built: catalogue the transport and the manager share.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".onnx"):
            return httpx.Response(200, content=built.model)
        return httpx.Response(200, content=built.config)

    return handler


async def _await_job(
    registry: VoiceDownloadRegistry, job_id: str, *, timeout_s: float = BLOCK_CAP_S
) -> VoiceDownloadJob:
    """Poll one job until it is terminal, bounded by *timeout_s*.

    :param registry: registry holding the job.
    :param job_id: id returned by ``start``.
    :param timeout_s: longest the poll may take.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snapshot = registry.status(job_id)
        assert snapshot is not None
        if snapshot.state is not VoiceState.RUNNING:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish within {timeout_s} s")


def _golden_rows() -> list[dict[str, Any]]:
    """Return the golden fixture's voice rows."""
    document: dict[str, Any] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = document["voices"]
    return rows


async def test_the_shipped_table_equals_the_golden_fixture() -> None:
    """Every shipped row is byte-for-byte the pinned release's own numbers."""
    rows = _golden_rows()
    assert [voice.id for voice in SUGGESTED_VOICES] == [row["id"] for row in rows]
    by_id = {voice.id: voice for voice in SUGGESTED_VOICES}
    for row in rows:
        voice = by_id[row["id"]]
        assert voice.language == row["language"]
        assert voice.quality == row["quality"]
        assert voice.sample_rate_hz == row["sample_rate_hz"]
        assert voice.num_speakers == row["num_speakers"]
        assert voice.licence == row["license"]
        assert voice.licence_url == row["license_url"]
        assert voice.file_onnx == row["file_onnx"]
        assert voice.onnx_bytes == row["onnx_bytes"]
        assert voice.onnx_md5 == row["onnx_md5"]
        assert voice.file_config == row["file_config"]
        assert voice.config_bytes == row["config_bytes"]
        assert voice.config_md5 == row["config_md5"]
        assert voice.file_model_card == row["file_model_card"]
        assert voice.model_card_bytes == row["model_card_bytes"]
        assert voice.model_card_md5 == row["model_card_md5"]


async def test_every_row_has_a_valid_id_a_licence_and_its_language_prefix() -> None:
    """A row is only offered when its id, licence and language agree with itself."""
    for voice in SUGGESTED_VOICES:
        assert VOICE_ID_PATTERN.fullmatch(voice.id) is not None
        assert voice.language == voice.id.split("-", 1)[0]
        assert voice.licence.strip()
        assert voice.licence_url.startswith("http")
        assert install_url(voice, voice.file_onnx) == f"{VOICE_REPO_ROOT}/{voice.file_onnx}"
        assert voice.file_onnx.endswith(f"{voice.id}.onnx")
        assert voice.file_config.endswith(f"{voice.id}.onnx.json")


async def test_suggested_lists_small_qualities_before_medium() -> None:
    """The catalogue offers the cheapest voices first, so a Pi 4 is not led astray."""
    order = {quality: rank for rank, quality in enumerate(("x_low", "low", "medium"))}
    qualities = [voice.quality for voice in SUGGESTED_VOICES]
    assert qualities == sorted(qualities, key=lambda quality: order[quality])
    assert qualities.index("medium") > qualities.index("low")
    assert qualities.index("medium") > qualities.index("x_low")


async def test_installed_verifies_a_complete_model(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """A model and config that match the table are reported as verified."""
    built = catalogue()
    voices_dir = tmp_path / "voices"
    manager = _manager(voices_dir, client=_client(_serve(built), clients))
    await manager.download(VOICE)

    installed = manager.installed()

    assert len(installed) == 1
    voice: InstalledVoice = installed[0]
    assert voice.id == VOICE
    assert voice.bytes == built.voice.onnx_bytes
    assert voice.licence == built.voice.licence
    assert voice.licence_known is True
    assert voice.checksum_verified is True
    assert voice.problem is None
    assert voice.config_path is not None and voice.config_path.is_file()


async def test_installed_reports_a_missing_config(
    catalogue: CatalogueFactory, tmp_path: Path
) -> None:
    """A model without its config is listed with a problem, not hidden."""
    built = catalogue()
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    (voices_dir / f"{VOICE}.onnx").write_bytes(built.model)

    installed = _manager(voices_dir).installed()

    assert len(installed) == 1
    assert installed[0].config_path is None
    assert installed[0].problem is not None
    _assert_whole_sentence(installed[0].problem or "")


async def test_installed_reports_an_unparsable_config(
    catalogue: CatalogueFactory, tmp_path: Path
) -> None:
    """A config the engine could not index is a problem on the row."""
    built = catalogue()
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    (voices_dir / f"{VOICE}.onnx").write_bytes(built.model)
    (voices_dir / f"{VOICE}.onnx.json").write_text("{not json", encoding="utf-8")

    installed = _manager(voices_dir).installed()

    assert len(installed) == 1
    assert installed[0].config_path is not None
    assert installed[0].problem is not None
    assert "config" in (installed[0].problem or "")


async def test_installed_hides_hidden_models_and_keeps_a_hand_installed_one(
    tmp_path: Path,
) -> None:
    """A dot name is never listed; an unknown well-formed voice has no licence."""
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    (voices_dir / ".en_GB-hidden-low.onnx").write_bytes(b"\x08")
    (voices_dir / ".en_GB-hidden-low.onnx.json").write_text(
        json.dumps(CONFIG_DOCUMENT), encoding="utf-8"
    )
    (voices_dir / "en_GB-ghost-low.onnx").write_bytes(b"\x08" * 32)
    (voices_dir / "en_GB-ghost-low.onnx.json").write_text(
        json.dumps(CONFIG_DOCUMENT), encoding="utf-8"
    )

    installed = _manager(voices_dir).installed()

    assert [voice.id for voice in installed] == ["en_GB-ghost-low"]
    assert installed[0].licence is None
    assert installed[0].licence_known is False
    assert installed[0].checksum_verified is False
    assert installed[0].problem is None


async def test_download_publishes_both_files_and_keeps_the_client_open(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """A verified download publishes both files and never closes a borrowed client."""
    built = catalogue()
    calls: list[str] = []
    serve = _serve(built)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return serve(request)

    client = _client(handler, clients)
    voices_dir = tmp_path / "voices"
    manager = _manager(voices_dir, client=client)

    installed = await manager.download(VOICE)

    assert (voices_dir / f"{VOICE}.onnx").read_bytes() == built.model
    assert (voices_dir / f"{VOICE}.onnx.json").read_bytes() == built.config
    assert [entry.name for entry in voices_dir.iterdir() if entry.name.startswith(".")] == []
    assert installed.problem is None
    assert installed.checksum_verified is True
    assert installed.bytes == built.voice.onnx_bytes
    assert installed.onnx_path.is_file()
    assert client.is_closed is False

    again = await manager.download(VOICE)

    assert again.onnx_path.is_file()
    assert len(calls) == 4
    assert client.is_closed is False


async def test_download_publishes_the_config_before_the_model(
    catalogue: CatalogueFactory,
    clients: list[httpx.AsyncClient],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between the two publishes can never leave a model without its config."""
    built = catalogue()
    published: list[str] = []
    real_replace = os.replace

    def tracking_replace(source: Path, destination: Path) -> None:
        published.append(destination.name)
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", tracking_replace)
    manager = _manager(tmp_path / "voices", client=_client(_serve(built), clients))

    await manager.download(VOICE)

    assert published == [f"{VOICE}.onnx.json", f"{VOICE}.onnx"]


async def test_download_follows_redirects_to_the_final_body(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """A 302 then a 307 is followed, and the published bytes are the last response's."""
    built = catalogue()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path.endswith(f"/{built.voice.file_onnx}"):
            return httpx.Response(302, headers={"location": "/hop/one"})
        if path == "/hop/one":
            return httpx.Response(307, headers={"location": "/hop/two"})
        if path == "/hop/two":
            return httpx.Response(200, content=built.model)
        return httpx.Response(200, content=built.config)

    client = _client(handler, clients)
    voices_dir = tmp_path / "voices"
    manager = _manager(voices_dir, client=client)

    await manager.download(VOICE)

    assert len(seen) == 4
    assert seen[1] == "/hop/one"
    assert seen[2] == "/hop/two"
    assert (voices_dir / f"{VOICE}.onnx").read_bytes() == built.model


async def test_download_refuses_a_redirect_loop(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """More than REDIRECT_LIMIT hops is a transfer failure, never a corrupt model."""
    built = catalogue()
    hops = REDIRECT_LIMIT + 1
    state = {"hops": 0, "model_served": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["model_served"]:
            return httpx.Response(200, content=built.config)
        state["hops"] += 1
        if state["hops"] > hops:
            state["model_served"] = True
            return httpx.Response(200, content=built.model)
        return httpx.Response(302, headers={"location": f"/hop/{state['hops']}"})

    client = _client(handler, clients)
    voices_dir = tmp_path / "voices"
    manager = _manager(voices_dir, client=client)

    with pytest.raises(VoiceError) as caught:
        await manager.download(VOICE)

    assert caught.value.code is VoiceErrorCode.DOWNLOAD_FAILED
    assert "redirected" in str(caught.value)
    _assert_whole_sentence(str(caught.value))
    _assert_nothing_published(tmp_path, voices_dir)


async def test_download_rejects_a_truncated_body(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """A body shorter than its declared length is a corrupt download, not a model."""
    built = catalogue()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".onnx"):
            return httpx.Response(
                200,
                headers={"content-length": str(len(built.model))},
                content=built.model[:-10],
            )
        return httpx.Response(200, content=built.config)

    voices_dir = tmp_path / "voices"
    manager = _manager(voices_dir, client=_client(handler, clients))

    with pytest.raises(VoiceError) as caught:
        await manager.download(VOICE)

    assert caught.value.code is VoiceErrorCode.VOICE_CORRUPT
    _assert_whole_sentence(str(caught.value))
    assert caught.value.detail["bytes_expected"] == len(built.model)
    assert caught.value.detail["bytes_received"] == len(built.model) - 10
    _assert_nothing_published(tmp_path, voices_dir)


async def test_download_rejects_a_model_below_the_plausible_size(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """A tiny body is refused; note that it is non-zero on purpose.

    ``piper.download_voices`` treats a zero-byte model as "needs download", so a
    zero-byte fixture would test that self-healing rule instead of this one.
    """
    built = catalogue(model=b"\x08" * 4096)
    voices_dir = tmp_path / "voices"
    manager = _manager(voices_dir, client=_client(_serve(built), clients))

    with pytest.raises(VoiceError) as caught:
        await manager.download(VOICE)

    assert caught.value.code is VoiceErrorCode.VOICE_CORRUPT
    assert "too small" in str(caught.value)
    _assert_whole_sentence(str(caught.value))
    _assert_nothing_published(tmp_path, voices_dir)


async def test_download_rejects_a_byte_flipped_model(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """Same size, one flipped byte: the md5 check catches it and reports both digests."""
    built = catalogue()
    flipped = bytes([built.model[0] ^ 0xFF]) + built.model[1:]
    assert len(flipped) == len(built.model)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".onnx"):
            return httpx.Response(200, content=flipped)
        return httpx.Response(200, content=built.config)

    voices_dir = tmp_path / "voices"
    manager = _manager(voices_dir, client=_client(handler, clients))

    with pytest.raises(VoiceError) as caught:
        await manager.download(VOICE)

    assert caught.value.code is VoiceErrorCode.VOICE_CORRUPT
    assert caught.value.detail["expected_md5"] == _md5(built.model)
    assert caught.value.detail["actual_md5"] == _md5(flipped)
    _assert_whole_sentence(str(caught.value))
    _assert_nothing_published(tmp_path, voices_dir)


async def test_download_rejects_an_html_error_page_saved_as_a_model(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """A digest-matching body that starts like markup is still not a model."""
    page = b"<html><body>404 not found</body></html>"
    built = catalogue(model=page)
    voices_dir = tmp_path / "voices"
    manager = _manager(voices_dir, client=_client(_serve(built), clients))

    with pytest.raises(VoiceError) as caught:
        await manager.download(VOICE)

    assert caught.value.code is VoiceErrorCode.VOICE_CORRUPT
    assert "web page" in str(caught.value)
    _assert_whole_sentence(str(caught.value))
    _assert_nothing_published(tmp_path, voices_dir)


async def test_download_rejects_a_config_that_is_not_json(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """An unparsable config is a rejected download, not a KeyError at press time."""
    built = catalogue(config=b"this is not json at all")
    voices_dir = tmp_path / "voices"
    manager = _manager(voices_dir, client=_client(_serve(built), clients))

    with pytest.raises(VoiceError) as caught:
        await manager.download(VOICE)

    assert caught.value.code is VoiceErrorCode.VOICE_CORRUPT
    assert "config" in str(caught.value)
    _assert_whole_sentence(str(caught.value))
    _assert_nothing_published(tmp_path, voices_dir)


async def test_download_rejects_a_config_without_the_espeak_voice(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """The engine indexes ``espeak.voice``, so a config missing it is refused."""
    document = dict(CONFIG_DOCUMENT)
    document["espeak"] = {"id": "en-gb"}
    built = catalogue(config=json.dumps(document).encode("utf-8"))
    voices_dir = tmp_path / "voices"
    manager = _manager(voices_dir, client=_client(_serve(built), clients))

    with pytest.raises(VoiceError) as caught:
        await manager.download(VOICE)

    assert caught.value.code is VoiceErrorCode.VOICE_CORRUPT
    assert "espeak.voice" in str(caught.value)
    _assert_nothing_published(tmp_path, voices_dir)


async def test_download_rejects_a_config_without_the_phoneme_map(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """A config without ``phoneme_id_map`` cannot be loaded by piper."""
    document = dict(CONFIG_DOCUMENT)
    del document["phoneme_id_map"]
    built = catalogue(config=json.dumps(document).encode("utf-8"))
    voices_dir = tmp_path / "voices"
    manager = _manager(voices_dir, client=_client(_serve(built), clients))

    with pytest.raises(VoiceError) as caught:
        await manager.download(VOICE)

    assert caught.value.code is VoiceErrorCode.VOICE_CORRUPT
    assert "phoneme_id_map" in str(caught.value)
    _assert_nothing_published(tmp_path, voices_dir)


async def test_download_rejects_an_oversized_declared_length(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """A clearly oversized Content-Length is refused before the body is read."""
    catalogue()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "2048"}, content=b"x" * 2048)

    voices_dir = tmp_path / "voices"
    manager = _manager(voices_dir, client=_client(handler, clients), max_bytes=1024)

    with pytest.raises(VoiceError) as caught:
        await manager.download(VOICE)

    assert caught.value.code is VoiceErrorCode.VOICE_TOO_LARGE
    assert "larger than" in str(caught.value)
    _assert_nothing_published(tmp_path, voices_dir)


async def test_a_failed_redownload_leaves_the_installed_voice_untouched(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """A failed refresh never replaces a good model with a bad one."""
    built = catalogue()
    voices_dir = tmp_path / "voices"
    good = _manager(voices_dir, client=_client(_serve(built), clients))
    await good.download(VOICE)
    onnx = (voices_dir / f"{VOICE}.onnx").read_bytes()
    config = (voices_dir / f"{VOICE}.onnx.json").read_bytes()

    def bad_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".onnx"):
            return httpx.Response(200, content=built.model[:-1])
        return httpx.Response(200, content=built.config)

    broken = _manager(voices_dir, client=_client(bad_handler, clients))
    with pytest.raises(VoiceError) as caught:
        await broken.download(VOICE)

    assert caught.value.code is VoiceErrorCode.VOICE_CORRUPT
    assert (voices_dir / f"{VOICE}.onnx").read_bytes() == onnx
    assert (voices_dir / f"{VOICE}.onnx.json").read_bytes() == config
    assert [entry.name for entry in voices_dir.iterdir() if entry.name.startswith(".")] == []


async def test_download_refuses_below_the_memory_threshold(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """The D22 gate refuses the download with the sentence the engine would use."""
    catalogue()
    probe = PiperProbe(
        FakeSettings(),
        meminfo_path=_write_meminfo(tmp_path / "meminfo", total_mb=TOO_LITTLE_MB),
    )
    voices_dir = tmp_path / "voices"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("the gate must be asked before anything is fetched")

    manager = _manager(voices_dir, probe=probe, client=_client(handler, clients))
    try:
        with pytest.raises(VoiceError) as caught:
            await manager.download(VOICE)
    finally:
        probe.close()

    assert caught.value.code is VoiceErrorCode.VOICE_UNAVAILABLE
    sentence = str(caught.value)
    assert str(DEFAULT_PIPER_MIN_RAM_MB) in sentence
    assert str(TOO_LITTLE_MB) in sentence
    assert PIPER_MIN_RAM_SETTING in sentence
    assert not voices_dir.exists()


async def test_download_refuses_when_the_piper_extra_is_missing(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """An install without the extra is told the exact command that fixes it."""
    catalogue()

    def importer(_name: str) -> ModuleType:
        raise ModuleNotFoundError("No module named 'piper'")

    probe = PiperProbe(
        FakeSettings(),
        importer=importer,
        meminfo_path=_write_meminfo(tmp_path / "meminfo", total_mb=PLENTY_MB),
    )
    voices_dir = tmp_path / "voices"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("the gate must be asked before anything is fetched")

    manager = _manager(voices_dir, probe=probe, client=_client(handler, clients))
    try:
        assert await probe.prime() is False
        with pytest.raises(VoiceError) as caught:
            await manager.download(VOICE)
    finally:
        probe.close()

    assert caught.value.code is VoiceErrorCode.VOICE_UNAVAILABLE
    assert "`uv sync --extra piper`" in str(caught.value)
    assert not voices_dir.exists()


async def test_download_refuses_an_uncurated_voice(
    clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """A well-formed id outside the table has no digest, so it is refused unverified."""
    voices_dir = tmp_path / "voices"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an uncurated id must not reach the network")

    manager = _manager(voices_dir, client=_client(handler, clients))

    with pytest.raises(VoiceError) as caught:
        await manager.download("en_GB-nobody-low")

    assert caught.value.code is VoiceErrorCode.VOICE_NOT_SUGGESTED
    assert "suggested list" in str(caught.value)
    _assert_whole_sentence(str(caught.value))
    assert not voices_dir.exists()


@pytest.mark.parametrize(
    "hostile",
    ["../../etc/passwd", "en_US-x-medium/../../etc/passwd", "", "/etc/passwd", "a" * 100],
)
async def test_download_refuses_a_hostile_voice_id(
    hostile: str, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """No id that could name a path outside ``voices/`` is ever accepted."""
    voices_dir = tmp_path / "voices"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a hostile id must not reach the network")

    manager = _manager(voices_dir, client=_client(handler, clients))

    with pytest.raises(VoiceError) as caught:
        await manager.download(hostile)

    assert caught.value.code is VoiceErrorCode.INVALID_VOICE
    _assert_whole_sentence(str(caught.value))
    assert not voices_dir.exists()
    _assert_nothing_published(tmp_path, voices_dir)


async def test_progress_is_monotonic_and_ends_at_the_table_total(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """Progress never goes backwards and the last value is both files' total."""
    built = catalogue()
    updates: list[tuple[int, int | None]] = []
    manager = _manager(tmp_path / "voices", client=_client(_serve(built), clients))

    await manager.download(VOICE, on_progress=lambda done, total: updates.append((done, total)))

    done_values = [done for done, _total in updates]
    assert done_values == sorted(done_values)
    assert done_values[-1] == built.total
    assert all(total == built.total for _done, total in updates)


async def test_registry_runs_a_download_to_done(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """``start`` returns a running snapshot that finishes as ``done``."""
    built = catalogue()
    voices_dir = tmp_path / "voices"
    registry = VoiceDownloadRegistry(_manager(voices_dir, client=_client(_serve(built), clients)))

    job = await registry.start(VOICE)
    finished = await _await_job(registry, job.job_id)

    assert job.state is VoiceState.RUNNING
    assert job.bytes_total == built.total
    assert job.started_at.endswith("Z")
    assert finished.state is VoiceState.DONE
    assert finished.bytes_done == built.total
    assert finished.error is None
    assert finished.finished_at is not None
    assert (voices_dir / f"{VOICE}.onnx").is_file()


async def test_registry_marks_a_corrupt_model_as_rejected(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """A corrupt model is ``rejected`` — the user must pick a voice, not a network."""
    built = catalogue()
    flipped = bytes([built.model[0] ^ 0xFF]) + built.model[1:]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".onnx"):
            return httpx.Response(200, content=flipped)
        return httpx.Response(200, content=built.config)

    registry = VoiceDownloadRegistry(
        _manager(tmp_path / "voices", client=_client(handler, clients))
    )

    job = await registry.start(VOICE)
    finished = await _await_job(registry, job.job_id)

    assert finished.state is VoiceState.REJECTED
    assert finished.error is not None
    _assert_whole_sentence(finished.error)
    assert "actual_md5" in finished.detail
    assert finished.finished_at is not None


async def test_registry_marks_a_transport_error_as_failed(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """A refused connection is ``failed``, with a sentence about the network."""
    catalogue()

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    registry = VoiceDownloadRegistry(
        _manager(tmp_path / "voices", client=_client(handler, clients))
    )

    job = await registry.start(VOICE)
    finished = await _await_job(registry, job.job_id)

    assert finished.state is VoiceState.FAILED
    assert finished.error is not None
    assert "connect" in finished.error.lower() or "reach" in finished.error.lower()
    assert finished.detail["reason"] == "transport_error"


async def test_registry_refuses_a_duplicate_start(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """One download per voice at a time, refused with an actionable sentence."""
    built = catalogue()
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.wait_for(release.wait(), BLOCK_CAP_S)
        if request.url.path.endswith(".onnx"):
            return httpx.Response(200, content=built.model)
        return httpx.Response(200, content=built.config)

    registry = VoiceDownloadRegistry(
        _manager(tmp_path / "voices", client=_client(handler, clients))
    )
    job = await registry.start(VOICE)
    try:
        await asyncio.wait_for(started.wait(), BLOCK_CAP_S)
        with pytest.raises(VoiceError) as caught:
            await registry.start(VOICE)
        assert caught.value.code is VoiceErrorCode.VOICE_UNAVAILABLE
        assert "already downloading" in str(caught.value)
        assert caught.value.detail["reason"] == "already_running"
        release.set()
        assert (await _await_job(registry, job.job_id)).state is VoiceState.DONE
    finally:
        release.set()
        await registry.aclose()


async def test_registry_status_of_an_unknown_job_is_none(
    clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """An unknown id is ``None`` rather than an exception the route must catch."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no job was started, so nothing may be fetched")

    registry = VoiceDownloadRegistry(
        _manager(tmp_path / "voices", client=_client(handler, clients))
    )

    assert registry.status("nope") is None
    assert registry.jobs() == []


async def test_registry_keeps_only_the_newest_terminal_jobs(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """Retention is bounded, and the oldest finished job is the first to go."""
    built = catalogue()
    registry = VoiceDownloadRegistry(
        _manager(tmp_path / "voices", client=_client(_serve(built), clients)),
        max_jobs=2,
    )

    first = await registry.start(VOICE)
    await _await_job(registry, first.job_id)
    second = await registry.start(VOICE)
    await _await_job(registry, second.job_id)
    third = await registry.start(VOICE)
    await _await_job(registry, third.job_id)

    assert [job.job_id for job in registry.jobs()] == [second.job_id, third.job_id]
    assert registry.status(first.job_id) is None


async def test_registry_aclose_cancels_a_running_job_within_the_grace(
    catalogue: CatalogueFactory, clients: list[httpx.AsyncClient], tmp_path: Path
) -> None:
    """Shutdown cancels the transfer, records a sentence and is idempotent."""
    built = catalogue()
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.wait_for(release.wait(), BLOCK_CAP_S)
        return httpx.Response(200, content=built.model)

    registry = VoiceDownloadRegistry(
        _manager(tmp_path / "voices", client=_client(handler, clients))
    )
    job = await registry.start(VOICE)
    try:
        await asyncio.wait_for(started.wait(), BLOCK_CAP_S)
        await asyncio.wait_for(registry.aclose(), BLOCK_CAP_S)
    finally:
        release.set()

    stopped = registry.status(job.job_id)
    assert stopped is not None
    assert stopped.state is VoiceState.FAILED
    assert stopped.error is not None
    assert "stopped before" in stopped.error
    assert stopped.detail["reason"] == "closed"
    assert stopped.finished_at is not None

    await asyncio.wait_for(registry.aclose(), BLOCK_CAP_S)
    assert registry.status(job.job_id) == stopped


async def test_aclose_bounds_a_task_that_suppresses_cancellation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A download task that swallows ``CancelledError`` is abandoned after the grace.

    ``asyncio.gather`` inside ``asyncio.timeout`` would wait forever on such a
    task; :meth:`VoiceDownloadRegistry.aclose` uses :func:`asyncio.wait` with
    ``close_grace_s`` as its bound, warns that it gave up, and still marks the
    RUNNING snapshot ``FAILED``.

    :param tmp_path: pytest temporary directory.
    :param caplog: pytest log capture fixture.
    """
    registry = VoiceDownloadRegistry(_manager(tmp_path / "voices"))
    release = asyncio.Event()
    job_id = "stubborn-job"
    registry._jobs[job_id] = VoiceDownloadJob(
        job_id=job_id,
        voice=VOICE,
        state=VoiceState.RUNNING,
        bytes_done=0,
        bytes_total=None,
        error=None,
        detail=MappingProxyType({}),
        started_at="2026-01-01T00:00:00.000000Z",
        finished_at=None,
    )

    swallowing = asyncio.Event()

    async def stubborn() -> None:
        try:
            await asyncio.sleep(BLOCK_CAP_S * 2)
        except asyncio.CancelledError:
            # Signal that cancellation was delivered *and* swallowed, so the test
            # never has to guess whether the task had started yet.
            swallowing.set()
            await release.wait()

    task = asyncio.create_task(stubborn(), name="stubborn-voice-download")
    registry._tasks[job_id] = task
    close_task = asyncio.create_task(registry.aclose())
    try:
        with caplog.at_level(logging.WARNING, logger="chime_deck.tts.voices"):
            # Deterministic: wait until aclose has cancelled the task and the task
            # has swallowed it. Ordering was previously left to chance, so a task
            # cancelled before it started never swallowed anything and aclose
            # returned immediately (aarch64 board).
            await asyncio.wait_for(swallowing.wait(), 2.0)
            assert not close_task.done(), "aclose must be waiting, not returning early"
            await asyncio.wait_for(close_task, BLOCK_CAP_S + 2)
    finally:
        release.set()
        # The task was cancelled; having swallowed CancelledError to await
        # ``release``, it finishes by *raising* it. Either outcome proves it did
        # not hang, which is the point.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 2.0)
        assert task.done()

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "chime_deck.tts.voices" and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "did not stop within" in warnings[0]

    stopped = registry.status(job_id)
    assert stopped is not None
    assert stopped.state is VoiceState.FAILED
    assert stopped.error is not None
    assert "stopped before" in stopped.error
    assert stopped.detail["reason"] == "closed"


async def test_sweep_removes_only_old_temporary_directories(tmp_path: Path) -> None:
    """A directory a killed process left behind is swept; a live one is kept."""
    voices_dir = tmp_path / "voices"
    stale = voices_dir / ".stale.tmp"
    stale.mkdir(parents=True)
    fresh = voices_dir / ".fresh.tmp"
    fresh.mkdir()
    old = time.time() - STALE_TEMP_AGE_S - 60
    os.utime(stale, (old, old))

    removed = _manager(voices_dir).sweep_stale_temps()

    assert removed == 1
    assert not stale.exists()
    assert fresh.is_dir()
