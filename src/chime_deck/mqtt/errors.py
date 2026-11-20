"""Typed errors raised by the MQTT bridge.

Every failure this package raises on purpose derives from :class:`MqttError`, so
a caller can catch the whole family with one ``except``. The classes carry no
payload beyond their message: the message is written for a user looking at the
Settings page, and it is scrubbed on the way into a log line.

Owning ticket: HSD-kslf.
"""

from __future__ import annotations

__all__ = ["MqttConfigurationError", "MqttError"]


class MqttError(Exception):
    """Base class for everything the MQTT package raises."""


class MqttConfigurationError(MqttError):
    """The stored MQTT settings cannot be used to build a bridge.

    This is not a transient condition: the user has to change a setting. The
    message names the offending setting key and what to do about it, and it
    never contains the value of ``mqtt.password`` — not even when the password
    is the setting that is wrong, because the stored value of a secret is not
    printable.
    """
