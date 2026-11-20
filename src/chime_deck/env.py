"""Environment-variable lookup with one-release aliases for the old prefix.

Every setting this project reads from the environment is named
``CHIMEDECK_<SUFFIX>``. The rename from *Super Doorbell* to *ChimeDeck*
(HSD-l84l) therefore renamed the variables too, which would silently break an
installation that is already running: the owner's deployed systemd unit still
sets ``SUPER_DOORBELL_DATA_DIR``, and a process that ignored it would fall back
to a fresh ``./data`` and orphan the live database. Data loss that happens
without an error message is the worst possible upgrade, so the old names are
still honoured for exactly one release.

Precedence is new-over-old, and an old name always logs a deprecation warning
naming its replacement, so the operator learns about the change from the log
rather than from a missing database.

**Removing the alias is a two-part edit:** delete :data:`LEGACY_ENV_NAMES` and
the fallback branch in :func:`env_value` that reads :data:`LEGACY_PREFIX`. Those
are the only places that know about the old names; every caller already goes
through :func:`env_value`.

This module imports nothing from the project on purpose. It is imported by
modules under ``chime_deck/store/``, and those may not import
:mod:`chime_deck.config` (see the import-cycle rule in
:mod:`chime_deck.store.settings_registry`), so the helper that ``store`` needs
has to be free of any project dependency.

Owning ticket: HSD-l84l.
"""

from __future__ import annotations

import logging
import os
from typing import Final

__all__ = [
    "LEGACY_ENV_NAMES",
    "LEGACY_PREFIX",
    "PREFIX",
    "env_value",
]

_LOGGER = logging.getLogger(__name__)

PREFIX: Final = "CHIMEDECK_"
"""Prefix of the canonical environment variables this build writes and reads."""

LEGACY_PREFIX: Final = "SUPER_DOORBELL_"
"""Prefix of the pre-rename variables, honoured for one release only."""

LEGACY_ENV_NAMES: Final[tuple[str, ...]] = (
    "HOST",
    "PORT",
    "LOG_LEVEL",
    "LOG_FILE",
    "DATA_DIR",
    "DATA",
)
"""Suffixes whose ``SUPER_DOORBELL_`` form is still accepted.

Deleting this tuple together with the fallback branch in :func:`env_value` is
the whole removal; nothing else in the project refers to the old names.
"""


def env_value(suffix: str) -> str | None:
    """Return the configured value of one environment variable.

    The canonical ``CHIMEDECK_<suffix>`` variable wins whenever it is **present**,
    including when it is empty: ``CHIMEDECK_LOG_FILE=`` is how an operator
    switches a feature off, so an empty value must not fall through to the legacy
    name. When both spellings are set the new one wins and a warning says so.
    Otherwise the legacy ``SUPER_DOORBELL_<suffix>`` variable is used with a
    deprecation warning naming its replacement.

    :param suffix: variable name without a prefix, e.g. ``"DATA_DIR"``.
    :returns: the variable's value, or ``None`` when neither spelling is set.
    """
    current = f"{PREFIX}{suffix}"
    legacy = f"{LEGACY_PREFIX}{suffix}"
    legacy_present = legacy in os.environ

    if current in os.environ:
        if legacy_present:
            _LOGGER.warning(
                "both %s and %s are set; using %s, the new name wins",
                current,
                legacy,
                current,
            )
        return os.environ[current]

    if legacy_present:
        _LOGGER.warning(
            "%s is deprecated and will be removed in a future release; use %s instead",
            legacy,
            current,
        )
        return os.environ[legacy]

    return None
