"""The ported Home Assistant icon tables for D16's step 4.

Source of the domain table: Home Assistant frontend ``src/data/icons.ts``,
``FALLBACK_DOMAIN_ICONS`` (``dev``, blob ``5681e0e``), converted from the
``@mdi/js`` identifier names it uses to ``mdi:kebab-case``. The five domains
missing from that map (``binary_sensor``, ``switch``, ``lock``, ``cover`` and
``media_player``) come from Home Assistant core
``homeassistant/components/<domain>/icons.json``, key
``entity_component._.default``.

Source of the device-class table: each class's ``default`` entry in the same
core ``icons.json`` files (``binary_sensor`` blob ``94b26e7d``, ``sensor`` blob
``63bbcaaf``, ``cover`` blob ``fcc6df4c``). The ``sensor`` battery ranges and
state-specific (``state:``) icons are deliberately not ported, because D16's
step 4 keys on domain plus device class only.

Owning ticket: HSD-jwd3.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "D16_DEFAULT_ICON",
    "DEVICE_CLASS_ICONS",
    "FALLBACK_DOMAIN_ICONS",
    "fallback_icon",
]

D16_DEFAULT_ICON: Final = "mdi:help-circle"
"""D16 step 5: the generic icon when nothing else matched.

Home Assistant's own last resort is ``mdi:bookmark``; D16 mandates help-circle."""

FALLBACK_DOMAIN_ICONS: Final[dict[str, str]] = {
    "ai_task": "mdi:star-four-points",
    "air_quality": "mdi:air-filter",
    "alert": "mdi:alert",
    "automation": "mdi:robot",
    "battery": "mdi:battery",
    "binary_sensor": "mdi:radiobox-blank",
    "calendar": "mdi:calendar",
    "climate": "mdi:thermostat",
    "configurator": "mdi:cog",
    "conversation": "mdi:forum-outline",
    "counter": "mdi:counter",
    "cover": "mdi:window-open",
    "date": "mdi:calendar",
    "datetime": "mdi:calendar-clock",
    "demo": "mdi:home-assistant",
    "device_tracker": "mdi:account",
    "door": "mdi:door-open",
    "garage_door": "mdi:garage-open",
    "gate": "mdi:gate",
    "google_assistant": "mdi:google-assistant",
    "group": "mdi:google-circles-communities",
    "homeassistant": "mdi:home-assistant",
    "homekit": "mdi:home-automation",
    "humidity": "mdi:water-percent",
    "illuminance": "mdi:brightness-6",
    "image": "mdi:image",
    "image_processing": "mdi:image-filter-frames",
    "infrared": "mdi:led-on",
    "input_boolean": "mdi:toggle-switch",
    "input_button": "mdi:button-pointer",
    "input_datetime": "mdi:calendar-clock",
    "input_number": "mdi:ray-vertex",
    "input_select": "mdi:format-list-bulleted",
    "input_text": "mdi:form-textbox",
    "lawn_mower": "mdi:robot-mower",
    "light": "mdi:lightbulb",
    "lock": "mdi:lock",
    "media_player": "mdi:cast",
    "moisture": "mdi:water",
    "motion": "mdi:motion-sensor",
    "notify": "mdi:comment-alert",
    "number": "mdi:ray-vertex",
    "occupancy": "mdi:home-account",
    "persistent_notification": "mdi:bell",
    "person": "mdi:account",
    "plant": "mdi:flower",
    "power": "mdi:flash",
    "proximity": "mdi:apple-safari",
    "radio_frequency": "mdi:radio-tower",
    "remote": "mdi:remote",
    "scene": "mdi:palette",
    "schedule": "mdi:calendar-clock",
    "script": "mdi:script-text",
    "select": "mdi:format-list-bulleted",
    "sensor": "mdi:eye",
    "simple_alarm": "mdi:bell",
    "siren": "mdi:bullhorn",
    "stt": "mdi:microphone-message",
    "sun": "mdi:white-balance-sunny",
    "switch": "mdi:toggle-switch-variant",
    "temperature": "mdi:thermometer",
    "template": "mdi:code-braces",
    "text": "mdi:form-textbox",
    "time": "mdi:clock",
    "timer": "mdi:timer-outline",
    "todo": "mdi:clipboard-list",
    "tts": "mdi:speaker-message",
    "vacuum": "mdi:robot-vacuum",
    "wake_word": "mdi:chat-sleep",
    "weather": "mdi:weather-partly-cloudy",
    "window": "mdi:window-closed",
    "zone": "mdi:map-marker-radius",
}

DEVICE_CLASS_ICONS: Final[dict[str, dict[str, str]]] = {
    "binary_sensor": {
        "battery": "mdi:battery",
        "battery_charging": "mdi:battery",
        "carbon_monoxide": "mdi:smoke-detector",
        "cold": "mdi:thermometer",
        "connectivity": "mdi:close-network-outline",
        "door": "mdi:door-closed",
        "garage_door": "mdi:garage",
        "gas": "mdi:check-circle",
        "glass_break": "mdi:glass-fragile",
        "heat": "mdi:thermometer",
        "light": "mdi:brightness-5",
        "lock": "mdi:lock",
        "moisture": "mdi:water-off",
        "motion": "mdi:motion-sensor-off",
        "moving": "mdi:octagon",
        "occupancy": "mdi:home-outline",
        "opening": "mdi:square",
        "plug": "mdi:power-plug-off",
        "power": "mdi:power-plug-off",
        "presence": "mdi:home-outline",
        "problem": "mdi:check-circle",
        "running": "mdi:stop",
        "safety": "mdi:check-circle",
        "smoke": "mdi:smoke-detector-variant",
        "sound": "mdi:music-note-off",
        "tamper": "mdi:check-circle",
        "update": "mdi:package",
        "vibration": "mdi:crop-portrait",
        "window": "mdi:window-closed",
    },
    "sensor": {
        "absolute_humidity": "mdi:water-opacity",
        "apparent_power": "mdi:flash",
        "aqi": "mdi:air-filter",
        "area": "mdi:texture-box",
        "atmospheric_pressure": "mdi:thermometer-lines",
        "battery": "mdi:battery-unknown",
        "blood_glucose_concentration": "mdi:spoon-sugar",
        "carbon_dioxide": "mdi:molecule-co2",
        "carbon_monoxide": "mdi:molecule-co",
        "conductivity": "mdi:sprout-outline",
        "current": "mdi:current-ac",
        "data_rate": "mdi:transmission-tower",
        "data_size": "mdi:database",
        "date": "mdi:calendar",
        "distance": "mdi:arrow-left-right",
        "duration": "mdi:progress-clock",
        "energy": "mdi:lightning-bolt",
        "energy_storage": "mdi:car-battery",
        "frequency": "mdi:sine-wave",
        "gas": "mdi:meter-gas",
        "humidity": "mdi:water-percent",
        "illuminance": "mdi:brightness-5",
        "irradiance": "mdi:sun-wireless",
        "moisture": "mdi:water-percent",
        "monetary": "mdi:cash",
        "nitrogen_dioxide": "mdi:molecule",
        "nitrogen_monoxide": "mdi:molecule",
        "nitrous_oxide": "mdi:molecule",
        "ozone": "mdi:molecule",
        "ph": "mdi:ph",
        "pm1": "mdi:molecule",
        "pm10": "mdi:molecule",
        "pm25": "mdi:molecule",
        "pm4": "mdi:molecule",
        "power": "mdi:flash",
        "power_factor": "mdi:angle-acute",
        "precipitation": "mdi:weather-rainy",
        "precipitation_intensity": "mdi:weather-pouring",
        "pressure": "mdi:gauge",
        "radon": "mdi:radioactive",
        "reactive_energy": "mdi:lightning-bolt",
        "reactive_power": "mdi:flash",
        "signal_strength": "mdi:wifi",
        "sound_pressure": "mdi:ear-hearing",
        "speed": "mdi:speedometer",
        "sulphur_dioxide": "mdi:molecule",
        "temperature": "mdi:thermometer",
        "temperature_delta": "mdi:thermometer",
        "timestamp": "mdi:clock",
        "uptime": "mdi:clock-start",
        "volatile_organic_compounds": "mdi:molecule",
        "volatile_organic_compounds_parts": "mdi:molecule",
        "voltage": "mdi:sine-wave",
        "volume": "mdi:car-coolant-level",
        "volume_flow_rate": "mdi:pipe-valve",
        "volume_storage": "mdi:storage-tank",
        "water": "mdi:water",
        "weight": "mdi:weight",
        "wind_direction": "mdi:compass-rose",
        "wind_speed": "mdi:weather-windy",
    },
    "cover": {
        "blind": "mdi:blinds-horizontal",
        "curtain": "mdi:curtains",
        "damper": "mdi:circle",
        "door": "mdi:door-open",
        "garage": "mdi:garage-open",
        "gate": "mdi:gate-open",
        "shade": "mdi:roller-shade",
        "shutter": "mdi:window-shutter-open",
        "window": "mdi:window-open",
    },
}


def fallback_icon(domain: str, device_class: str | None) -> str | None:
    """Return the D16 step-4 icon for *domain* and *device_class*, or ``None``.

    The device class wins over the domain: a ``cover`` with ``device_class="garage"``
    is a garage, not a generic cover. A missing or unknown class falls back to the
    domain entry.

    :param domain: entity domain, e.g. ``"binary_sensor"``.
    :param device_class: device class from the entity state, or ``None``.
    :returns: an ``mdi:`` icon name, or ``None`` when neither table has an entry.
    """
    if device_class is not None:
        class_icons = DEVICE_CLASS_ICONS.get(domain)
        if class_icons is not None:
            icon = class_icons.get(device_class)
            if icon is not None:
                return icon
    return FALLBACK_DOMAIN_ICONS.get(domain)
