"""Persisted daemon config.

Daemon-level choices that must outlive a restart, kept in a small JSON file in
the user's config dir: the startup app (launched on the robot's first wake-up)
and the selected audio devices. Persisting means a choice survives reboots and
app updates, stays per-user (not shared across OS accounts on one machine), and
can be set over the REST API instead of only via a CLI flag.

Every setting is a key in one file, so the read-modify-write in :func:`_set_str`
is serialised under ``_LOCK``: without it, two settings written concurrently
would lose one of the two.
"""

import json
import logging
import threading
from pathlib import Path

import platformdirs

logger = logging.getLogger(__name__)

_STARTUP_APP_KEY = "startup_app"
_AUDIO_INPUT_KEY = "selected_audio_input"
_AUDIO_OUTPUT_KEY = "selected_audio_output"

# Serialises read-modify-write: all settings share one file.
_LOCK = threading.Lock()


def _config_path() -> Path:
    """Path to the daemon config file in the user's config dir."""
    return Path(platformdirs.user_config_dir("reachy_mini")) / "daemon_config.json"


def _read() -> dict:  # type: ignore[type-arg]
    """Load the config dict, or {} if missing/unreadable (best-effort)."""
    path = _config_path()
    try:
        with path.open() as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Ignoring unreadable daemon config {path}: {e}")
        return {}


def _get_str(key: str) -> str | None:
    """Return a persisted string setting, or None if unset or not a string."""
    value = _read().get(key)
    return value if isinstance(value, str) else None


def _set_str(key: str, value: str | None) -> None:
    """Persist a string setting; a falsy value clears the key."""
    with _LOCK:
        config = _read()
        if value:
            config[key] = value
        else:
            config.pop(key, None)

        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            json.dump(config, f, indent=2)


def get_startup_app() -> str | None:
    """Return the persisted startup app name, or None if unset."""
    return _get_str(_STARTUP_APP_KEY)


def set_startup_app(name: str | None) -> None:
    """Persist the startup app name; a falsy name clears it."""
    _set_str(_STARTUP_APP_KEY, name)


def get_selected_audio_input() -> str | None:
    """Return the persisted audio input device name, or None for the default."""
    return _get_str(_AUDIO_INPUT_KEY)


def set_selected_audio_input(name: str | None) -> None:
    """Persist the audio input device name; a falsy name clears the selection."""
    _set_str(_AUDIO_INPUT_KEY, name)


def get_selected_audio_output() -> str | None:
    """Return the persisted audio output device name, or None for the default."""
    return _get_str(_AUDIO_OUTPUT_KEY)


def set_selected_audio_output(name: str | None) -> None:
    """Persist the audio output device name; a falsy name clears the selection.

    Only the display name is stored: it round-trips through ``find_audio_device``
    to a PipeWire ``node.name``. An ALSA card index is deliberately never
    persisted — it shifts as USB devices come and go, so it is re-resolved from a
    live enumeration on each daemon start.
    """
    _set_str(_AUDIO_OUTPUT_KEY, name)
