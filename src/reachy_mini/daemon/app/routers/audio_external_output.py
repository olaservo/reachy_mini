"""The selected external output's ALSA card, and keeping its volume up.

Output *selection* lives in the audio_devices router and is stored as a PipeWire
display name. Volume, though, is set with ``amixer``, which needs an ALSA card —
so this module maps the one to the other and owns the ALSA-side handling of an
external card.

The card is resolved from ``aplay -l`` rather than from the selected device's
PipeWire properties, even though those do carry an ``alsa.card``. Two reasons:

* Reading them means running a ``Gst.DeviceMonitor``, and that enumeration tears
  down an active Bluetooth A2DP link. This has to be callable from the volume
  path, which is exactly where an enumeration must never happen.
* The PipeWire device provider is not up for roughly the first 15s after a daemon
  start — a monitor run then returns a transient ALSA-only view that does not
  contain the selected node at all. ``aplay -l`` is correct from boot.

The card index is deliberately not persisted: it shifts as USB devices come and
go. It is re-derived from the persisted name, which is stable.
"""

import asyncio
import logging
import re
import shlex
import subprocess

from .audio_devices import get_local_selected_output

logger = logging.getLogger(__name__)

AUDIO_CMD_TIMEOUT = 4

# Seconds after app startup at which to (re-)assert the external card's volume.
#
# Why re-assert at all: a USB card resets to its own hardware default when the
# media pipeline reopens the sink (~18s after service start on the Wireless
# unit), and that default is low — -23 dB on the SABRENT AU-UCMA, quiet enough to
# read as broken. `alsactl store` does not reliably survive a cold power-cycle,
# and VolumeControl is built lazily on the first REST volume call, so nothing
# asserts the level at boot on its own. A single well-timed set is fragile
# against the pipeline's own reset, so assert a few times over the first minute;
# repeated sets are no-ops once the level sticks.
_STARTUP_VOLUME_DELAYS = (10.0, 20.0, 30.0, 45.0)

# Cache of the last name -> card lookup, so the volume path does not run
# `aplay -l` on every call. Keyed by the selection it was resolved for.
_cached_card: str | None = None
_cached_for: str | None = None
_cache_valid = False


def _parse_playback_cards(aplay_output: str) -> list[tuple[str, str]]:
    """Parse ``aplay -l`` into ``[(card_index, description), ...]``."""
    cards: list[tuple[str, str]] = []
    seen: set[str] = set()
    # e.g. "card 1: Device [USB Advanced Audio Device], device 0: USB Audio ..."
    for match in re.finditer(r"card\s+(\d+):\s+(\S+)\s+\[([^\]]+)\]", aplay_output):
        index, description = match.group(1), match.group(3)
        if index in seen:
            continue
        seen.add(index)
        cards.append((index, description))
    return cards


def _list_playback_cards() -> list[tuple[str, str]]:
    """Return ``[(card_index, description), ...]`` for playback-capable cards."""
    try:
        out = subprocess.run(
            ["aplay", "-l"],
            capture_output=True,
            text=True,
            timeout=AUDIO_CMD_TIMEOUT,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        # Not fatal, and expected off Linux: the caller falls back to the
        # built-in card.
        logger.debug("audio-output: could not list ALSA cards: %s", e)
        return []
    return _parse_playback_cards(out)


def _resolve_alsa_card(device_name: str | None) -> str | None:
    """Map a selected output's display name to an ALSA card index, or None.

    None means "use the built-in path": nothing is selected, the built-in card is
    selected, or a Bluetooth sink is selected (no ALSA card exists for it, so its
    volume is not settable with amixer — the speaker's own control is the only
    one).

    Matching is by substring because PipeWire names an ALSA node after the card
    plus its profile: ``aplay -l``'s "USB Advanced Audio Device" becomes
    "USB Advanced Audio Device Analog Stereo". Verified on the Wireless unit to
    agree with the node's own ``alsa.card`` property.
    """
    if device_name is None:
        return None

    # Imported lazily so importing this module does not require GStreamer (gi).
    from reachy_mini.media.device_detection import DEFAULT_AUDIO_TARGET

    if DEFAULT_AUDIO_TARGET in device_name:
        # The XMOS is driven through its index-1 controls by the volume code's
        # own built-in path, which None selects.
        return None

    for index, description in _list_playback_cards():
        if description and description in device_name:
            return index
    return None


def selected_external_alsa_card() -> str | None:
    """Return the selected external output's ALSA card index, or None.

    Safe to call from any path: it never enumerates devices, and the ``aplay -l``
    lookup is cached until the selection changes.
    """
    global _cached_card, _cached_for, _cache_valid
    selected = get_local_selected_output()
    if not _cache_valid or _cached_for != selected:
        _cached_card = _resolve_alsa_card(selected)
        _cached_for = selected
        _cache_valid = True
    return _cached_card


def _raise_card_volume(card_id: str) -> None:
    """Set an external card's playback volume to 100% (best effort)."""
    try:
        subprocess.run(
            f"amixer -c {shlex.quote(card_id)} sset Speaker,0 100% "
            f"|| amixer -c {shlex.quote(card_id)} sset PCM,0 100%",
            shell=True,
            capture_output=True,
            timeout=AUDIO_CMD_TIMEOUT,
        )
    except subprocess.SubprocessError as e:
        logger.warning("audio-output: failed to raise card %s volume: %s", card_id, e)


async def ensure_external_volume_after_startup() -> None:
    """Re-assert the selected external card's volume during daemon startup.

    Registered as a startup task from ``volume.py``. Does nothing unless an
    external ALSA card is selected: the built-in card's levels are handled by the
    XMOS init, and a Bluetooth sink has no ALSA card to set.

    The stock device init only drives index-1 controls (the XMOS ``PCM,1``), which
    is a no-op on a USB card whose ``Speaker``/``PCM`` control is at index 0 —
    hence the index-0 sets here.
    """
    card = await asyncio.to_thread(selected_external_alsa_card)
    if card is None:
        return

    for i, delay in enumerate(_STARTUP_VOLUME_DELAYS):
        await asyncio.sleep(delay - (_STARTUP_VOLUME_DELAYS[i - 1] if i else 0))
        await asyncio.to_thread(_raise_card_volume, card)
    logger.info("audio-output: external card %s volume asserted at 100%%", card)
