"""Persisted daemon config.

A small JSON file in the user's config dir holding daemon-level choices that must
outlive a restart. The startup app (launched on the robot's first wake-up) lives
here today; more settings are on the way (e.g. the speaker-EQ gains in #1267).
Persisting means a choice survives reboots and app updates, stays per-user (not
shared across OS accounts on one machine), and can be set over the REST API
instead of only via a CLI flag.

Because several settings share one file, the read-modify-write in
:func:`_set_str` is serialised under ``_LOCK``: without it, two settings written
concurrently would lose one of the two. The write itself goes to a temp file
that is atomically renamed into place, so a power loss mid-write (the robot is
hard-powered-off routinely) leaves the previous config intact rather than a
truncated one.
"""

import json
import logging
import os
import threading
from pathlib import Path

import platformdirs

logger = logging.getLogger(__name__)

_KEY = "startup_app"
_EQ_KEY = "speaker_eq_gains"
# equalizer-10bands accepts per-band gains in [-24, +12] dB.
_EQ_GAIN_MIN, _EQ_GAIN_MAX = -24.0, 12.0


def _is_valid_gain(value: object) -> bool:
    """Return True for a real number within the equalizer dB range.

    The range comparison also rejects NaN and infinities (they compare False)
    and oversized ints (exact int/float compare, so no OverflowError) without
    converting the value.
    """
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and _EQ_GAIN_MIN <= value <= _EQ_GAIN_MAX
    )

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
    """Persist a string setting; a falsy value clears the key.

    Held under ``_LOCK`` so a concurrent write to a different key cannot clobber
    this one (the two would otherwise read the same base dict and race to write).
    """
    with _LOCK:
        config = _read()
        if value:
            config[key] = value
        else:
            config.pop(key, None)

        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and atomically rename: a crash mid-write then
        # leaves the old config in place, not a half-written one. fsync before
        # the rename so the bytes are durable before the rename that exposes them.
        tmp = path.with_name(f"{path.name}.tmp")
        with tmp.open("w") as f:
            json.dump(config, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def get_startup_app() -> str | None:
    """Return the persisted startup app name, or None if unset."""
    return _get_str(_KEY)


def get_speaker_eq_gains() -> list[float] | None:
    """Return the 10 speaker-EQ band gains (dB), or None if unset/invalid.

    Invalid values (wrong length, non-numeric, NaN/inf, or outside the
    equalizer-10bands [-24, +12] dB range) are treated as unset so the caller
    falls back to its built-in default.
    """
    config = _read()
    if _EQ_KEY not in config:
        return None
    value = config[_EQ_KEY]
    if (
        isinstance(value, list)
        and len(value) == 10
        and all(_is_valid_gain(x) for x in value)
    ):
        return [float(x) for x in value]
    # Present but malformed: warn so the user knows their values were ignored.
    logger.warning(
        "Ignoring invalid '%s' in daemon config (need 10 finite dB gains in "
        "[%g, %g]); using the built-in defaults.",
        _EQ_KEY,
        _EQ_GAIN_MIN,
        _EQ_GAIN_MAX,
    )
    return None


def set_startup_app(name: str | None) -> None:
    """Persist the startup app name; a falsy name clears it."""
    _set_str(_KEY, name)
