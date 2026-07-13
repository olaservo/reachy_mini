"""Audio output device selection.

Lets the dashboard switch which speaker the daemon plays through by
repointing the ``reachymini_audio_sink`` ALSA device in ``~/.asoundrc``
and restarting the daemon so the media pipeline reopens on the new card.

Design constraints (see ``daemon/app/main.py`` startup):

* The microphone source (``reachymini_audio_src``) and the ALSA default
  stay on the built-in XMOS card (card 0). This keeps the 4-mic array
  working **and** keeps ``check_reachymini_asoundrc()`` passing, so the
  daemon does not regenerate (and clobber) the file on the next start.
* Built-in output keeps the XMOS hardware echo cancellation. Any external
  card bypasses it (output no longer routes through the XMOS), so those
  options report ``aec: false`` — use push-to-talk for full duplex.

The two endpoints are attached to the existing ``/api/volume`` router
(see ``volume.py``) so no change to ``main.py`` router registration is
needed.
"""

import asyncio
import logging
import re
import shlex
import subprocess
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

ASOUNDRC = Path.home() / ".asoundrc"
# Substrings that identify the built-in XMOS sound card in `aplay -l`.
BUILTIN_CARD_NAMES = ["reachy mini audio", "respeaker"]
BUILTIN_ID = "builtin"
AUDIO_CMD_TIMEOUT = 4

# `~/.asoundrc` templates. `!default` + the mic source always stay on the
# XMOS (hw:0,0); only the sink block changes between built-in and external.
_DEFAULT_AND_SRC = """pcm.!default {{
    type hw
    card 0
}}
ctl.!default {{
    type hw
    card 0
}}
{sink}
pcm.reachymini_audio_src {{
    type dsnoop
    ipc_key 4242
    slave {{
        pcm "hw:0,0"
        channels 2
        rate 16000
        period_size 1024
        buffer_size 4096
    }}
}}
"""

_SINK_BUILTIN = """pcm.reachymini_audio_sink {
    type dmix
    ipc_key 4241
    slave {
        pcm "hw:0,0"
        channels 2
        period_size 1024
        buffer_size 4096
        rate 16000
    }
    bindings {
        0 0
        1 1
    }
}"""

# External USB cards: wrap dmix in `plug` so any rate/channel/format the
# card wants is converted automatically (bare dmix rejects mismatches),
# and address the card by stable name so a reboot renumbering is harmless.
_SINK_EXTERNAL = """pcm.reachymini_audio_sink {{
    type plug
    slave.pcm {{
        type dmix
        ipc_key 4243
        slave {{
            pcm "hw:CARD={card},0"
            channels 2
            period_size 1024
            buffer_size 4096
            rate 48000
        }}
        bindings {{
            0 0
            1 1
        }}
    }}
}}"""


class AudioOutputDevice(BaseModel):
    """A selectable audio output device."""

    id: str
    label: str
    aec: bool
    active: bool


class SetAudioOutputRequest(BaseModel):
    """Request body for switching the output device."""

    id: str


def _is_builtin(desc: str) -> bool:
    d = desc.lower()
    return any(name in d for name in BUILTIN_CARD_NAMES)


def _list_playback_cards() -> list[tuple[str, str]]:
    """Return ``[(card_id, description), ...]`` for playback-capable cards."""
    try:
        out = subprocess.run(
            ["aplay", "-l"],
            capture_output=True,
            text=True,
            timeout=AUDIO_CMD_TIMEOUT,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        raise HTTPException(500, f"Failed to list audio cards: {e}")

    cards: list[tuple[str, str]] = []
    seen: set[str] = set()
    # e.g. "card 1: Device [USB Advanced Audio Device], device 0: USB Audio ..."
    pattern = re.compile(r"card\s+\d+:\s+(\S+)\s+\[([^\]]+)\]")
    for line in out.splitlines():
        m = pattern.search(line)
        if not m:
            continue
        card_id, desc = m.group(1), m.group(2)
        if card_id in seen:
            continue
        seen.add(card_id)
        cards.append((card_id, desc))
    return cards


def _current_sink_card() -> str:
    """Return which card the sink currently targets.

    ``"__builtin__"`` for the XMOS (hw:0,0), the ALSA card id for an
    external ``hw:CARD=<id>`` sink, or ``""`` if it can't be determined.
    """
    try:
        content = ASOUNDRC.read_text(errors="ignore")
    except OSError:
        return ""
    m = re.search(r"reachymini_audio_sink\s*\{(.*?)\n\}", content, re.DOTALL)
    block = m.group(1) if m else content
    cm = re.search(r'pcm\s+"hw:CARD=([^,"]+)', block)
    if cm:
        return cm.group(1)
    if re.search(r'pcm\s+"hw:0(,0)?"', block):
        return "__builtin__"
    return ""


def list_devices() -> list[AudioOutputDevice]:
    """Enumerate selectable output devices with the active one flagged."""
    active = _current_sink_card()
    devices: list[AudioOutputDevice] = []
    for card_id, desc in _list_playback_cards():
        if "hdmi" in desc.lower() or "hdmi" in card_id.lower():
            continue  # HDMI is not a usable speaker on this robot
        builtin = _is_builtin(desc)
        dev_id = BUILTIN_ID if builtin else card_id
        is_active = (builtin and active == "__builtin__") or (
            not builtin and active == card_id
        )
        label = "Built-in speaker" if builtin else f"USB: {desc}"
        devices.append(
            AudioOutputDevice(
                id=dev_id, label=label, aec=builtin, active=is_active
            )
        )
    return devices


# Seconds after app startup at which to (re-)assert the external card's
# volume. USB cards reset to their own low hardware default when the media
# pipeline reopens the sink (~18s after service start on the Wireless unit),
# and `alsactl store` does not reliably survive a cold power-cycle — so a
# single well-timed set is fragile; asserting a few times over the first
# minute is cheap and robust. Repeated sets are no-ops once the level sticks.
_STARTUP_VOLUME_DELAYS = (10.0, 20.0, 30.0, 45.0)


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
        logger.warning("audio-output: failed to raise %s volume: %s", card_id, e)


async def ensure_external_volume_after_startup() -> None:
    """Re-assert the active external sink's volume during daemon startup.

    Registered as a startup task from ``volume.py``. Does nothing when the
    built-in speaker is active (its levels are handled by the XMOS init).
    """
    card = _current_sink_card()
    if not card or card == "__builtin__":
        return
    for i, delay in enumerate(_STARTUP_VOLUME_DELAYS):
        await asyncio.sleep(delay - (_STARTUP_VOLUME_DELAYS[i - 1] if i else 0))
        await asyncio.to_thread(_raise_card_volume, card)
    logger.info("audio-output: external card %s volume asserted at 100%%", card)


def _write_asoundrc(sink: str) -> None:
    content = _DEFAULT_AND_SRC.format(sink=sink)
    try:
        if ASOUNDRC.exists():
            (ASOUNDRC.parent / ".asoundrc.autobak").write_text(
                ASOUNDRC.read_text(errors="ignore")
            )
        ASOUNDRC.write_text(content)
    except OSError as e:
        raise HTTPException(500, f"Failed to write {ASOUNDRC}: {e}")


def switch(device_id: str) -> dict[str, object]:
    """Repoint the sink to ``device_id`` and restart the daemon to apply it."""
    devices = {d.id for d in list_devices()}
    if device_id not in devices:
        raise HTTPException(404, f"Unknown audio output device: {device_id!r}")

    if device_id == BUILTIN_ID:
        _write_asoundrc(_SINK_BUILTIN)
        post_restart = ""
    else:
        _write_asoundrc(_SINK_EXTERNAL.format(card=device_id))
        # Raise the external card to full volume and persist it -- but AFTER
        # the restart below. The card's level is reset to a low default while
        # the daemon reinitialises the sink, so setting it beforehand is wiped.
        card = shlex.quote(device_id)
        post_restart = (
            f"; sleep 9; amixer -c {card} sset Speaker 100%; alsactl store"
        )

    # Restart the daemon so the GStreamer pipeline reopens the new sink.
    # Must run in a detached transient unit: the service is KillMode=
    # control-group, so a restart launched inside our own cgroup would be
    # killed mid-stop. `sleep 1` lets the HTTP response flush first.
    restart_cmd = f"sleep 1; systemctl restart reachy-mini-daemon{post_restart}"
    try:
        subprocess.Popen(
            [
                "sudo",
                "-n",
                "systemd-run",
                "--collect",
                "--unit=reachy-audio-switch",
                "bash",
                "-c",
                restart_cmd,
            ]
        )
    except OSError as e:
        raise HTTPException(500, f"Failed to schedule daemon restart: {e}")

    logger.info("audio-output: switched to %s; daemon restarting", device_id)
    return {"status": "switching", "id": device_id, "restart": True}
