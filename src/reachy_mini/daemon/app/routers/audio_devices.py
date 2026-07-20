"""Audio device selection API routes: list, get, and set input/output devices.

This is the daemon's only audio-device selection API. A selection is applied by
rebuilding the media pipeline, which routes playback through ``pulsesink`` on the
chosen PipeWire node — so it reaches USB cards and Bluetooth speakers alike. With
no selection, playback falls back to the stock ``~/.asoundrc`` path (``alsasink``
on the built-in XMOS card), which keeps that chip's hardware echo cancellation.

Selections are persisted (see :mod:`reachy_mini.daemon.startup_app_config`) so an
external speaker chosen once survives a daemon restart or reboot.

.. warning::
   Enumeration is expensive in a way that is not obvious: every
   ``Gst.DeviceMonitor`` start/stop makes the PipeWire/bluez provider acquire and
   release devices, and that churn tears down an active A2DP link — it silently
   drops the very Bluetooth speaker that was just selected. Enumerate only while
   validating a selection or at startup, never on a hot path, and rely on the
   short TTL cache in ``device_detection`` to collapse bursts.
"""

import logging
import threading

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from reachy_mini.daemon import startup_app_config

router = APIRouter(prefix="/audio-devices")
logger = logging.getLogger(__name__)

DAEMON_BASE_URL = "http://127.0.0.1:8000"
DAEMON_API_TIMEOUT = 0.5

# In-process cache of the persisted selection. Daemon-internal callers read this
# rather than the config file or the REST API on every access.
_selected_input_device: str | None = None
_selected_output_device: str | None = None
_loaded = False
_LOAD_LOCK = threading.Lock()


class AudioDevice(BaseModel):
    """A selectable audio device."""

    name: str
    aec: bool


class AudioDeviceListResponse(BaseModel):
    """Response model for listing audio devices."""

    devices: list[AudioDevice]


class SelectedDeviceResponse(BaseModel):
    """Response model for the currently selected device."""

    device_name: str | None


class SetDeviceRequest(BaseModel):
    """Request model for setting the audio device."""

    device_name: str


def _ensure_loaded() -> None:
    """Populate the in-process selection cache from the persisted config once.

    Reads a small JSON file, never enumerates devices, so this is safe to call
    from any path including the volume hot path.
    """
    global _selected_input_device, _selected_output_device, _loaded
    if _loaded:
        return
    with _LOAD_LOCK:
        if _loaded:
            return
        _selected_input_device = startup_app_config.get_selected_audio_input()
        _selected_output_device = startup_app_config.get_selected_audio_output()
        _loaded = True
        if _selected_input_device or _selected_output_device:
            logger.info(
                f"Restored persisted audio selection (input: "
                f"{_selected_input_device}, output: {_selected_output_device})"
            )


def _filtered_devices(device_class: str):  # type: ignore[no-untyped-def]
    """Enumerate devices for a GStreamer class, preferring PipeWire nodes.

    Returns ``DeviceInfo`` objects. Callers that need only names should use
    :func:`list_audio_devices`. See the module warning before adding a call site.
    """
    # Imported lazily so importing this router does not require GStreamer (gi).
    from reachy_mini.media.device_detection import gst_monitor_devices

    try:
        devices = gst_monitor_devices(device_class)
    except Exception as e:
        logger.error(f"Failed to list {device_class} devices: {e}")
        return []

    # Prefer PipeWire nodes: they are stable across the ALSA/PipeWire providers,
    # include Bluetooth sinks, and their display names round-trip through
    # find_audio_device -> node.name -> pulsesink. The raw ALSA provider view
    # (e.g. "Dummy Output", monitor sources) is dropped when PipeWire is present
    # so listing and selection agree. Fall back to the raw list off PipeWire.
    pipewire = [
        device
        for device in devices
        if device.properties.get("node.name")
        and device.properties.get("device.class") != "monitor"
        and device.display_name
        and device.display_name != "Dummy Output"
    ]
    return pipewire or devices


def _has_hardware_aec(device) -> bool:  # type: ignore[no-untyped-def]
    """Return True if this device routes through the XMOS chip, which provides AEC.

    Echo cancellation on Reachy Mini is hardware, inside the XVF3800: it cancels
    only because that chip both drives the speaker and uses that signal as its
    reference. So the flag is really "is this the built-in card" — and AEC is
    only *actually* active when the input and output are both the XMOS. Any
    external speaker bypasses it, and the mic array then hears the robot's own
    voice in full duplex (use push-to-talk instead).
    """
    from reachy_mini.media.device_detection import DEFAULT_AUDIO_TARGET

    card_name = device.properties.get("alsa.card_name") or device.properties.get(
        "api.alsa.card.name"
    )
    return DEFAULT_AUDIO_TARGET in (card_name or device.display_name or "")


def list_audio_devices(device_class: str) -> list[AudioDevice]:
    """List selectable devices for a GStreamer class (Audio/Source or Audio/Sink)."""
    devices: list[AudioDevice] = []
    seen: set[str] = set()
    for device in _filtered_devices(device_class):
        if not device.display_name or device.display_name in seen:
            continue
        seen.add(device.display_name)
        devices.append(
            AudioDevice(name=device.display_name, aec=_has_hardware_aec(device))
        )
    return devices


def _apply_device_change(http_request: Request) -> None:
    """Rebuild the media pipeline so a device change takes effect (best-effort).

    Both the mic source and the playback sink are baked in when ``GStreamerAudio``
    is constructed, so a rebuild is the only way a new selection applies — for
    output as much as input ("applies on the next sound" was never true). The
    rebuild briefly interrupts any active stream. No-op (change only saved) when
    an app holds the audio device.
    """
    daemon = getattr(http_request.app.state, "daemon", None)
    if daemon is None:
        return

    if getattr(daemon, "media_released", False):
        logger.warning(
            "An app currently holds the audio device — the device change is saved "
            "but only takes effect on the next app launch (or when the daemon "
            "re-acquires the audio hardware). A running app will not pick it up live."
        )
        return

    logger.warning(
        "Restarting the media pipeline to apply the audio device change now "
        "(this briefly interrupts any active audio/video stream)."
    )
    try:
        daemon.restart_media_pipeline()
    except Exception as e:  # pragma: no cover - defensive
        logger.error(f"Could not restart media pipeline after device change: {e}")


@router.get("/output")
async def get_output_devices() -> AudioDeviceListResponse:
    """List available audio output devices."""
    return AudioDeviceListResponse(devices=list_audio_devices("Audio/Sink"))


@router.get("/input")
async def get_input_devices() -> AudioDeviceListResponse:
    """List available audio input devices."""
    return AudioDeviceListResponse(devices=list_audio_devices("Audio/Source"))


@router.get("/output/selected")
async def get_selected_output_device() -> SelectedDeviceResponse:
    """Get the currently selected output device."""
    _ensure_loaded()
    return SelectedDeviceResponse(device_name=_selected_output_device)


@router.post("/output/selected")
async def set_selected_output_device(
    request: SetDeviceRequest, http_request: Request
) -> SelectedDeviceResponse:
    """Set the output device to use. The selection persists across restarts."""
    global _selected_output_device
    _ensure_loaded()

    # The one enumeration this switch costs: the TTL cache in device_detection
    # then collapses the pipeline rebuild's own lookup into it.
    devices = list_audio_devices("Audio/Sink")
    match = next((d for d in devices if d.name == request.device_name), None)
    if match is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Device '{request.device_name}' not found. "
                f"Available: {[d.name for d in devices]}"
            ),
        )

    changed = request.device_name != _selected_output_device
    _selected_output_device = request.device_name
    startup_app_config.set_selected_audio_output(_selected_output_device)
    logger.info(f"Output device set to: {_selected_output_device} (aec: {match.aec})")
    if changed:
        _apply_device_change(http_request)

    return SelectedDeviceResponse(device_name=_selected_output_device)


@router.delete("/output/selected")
async def clear_selected_output_device(http_request: Request) -> SelectedDeviceResponse:
    """Clear the selected output device (use default)."""
    global _selected_output_device
    _ensure_loaded()
    changed = _selected_output_device is not None
    _selected_output_device = None
    startup_app_config.set_selected_audio_output(None)
    logger.info("Output device cleared, using default")
    if changed:
        _apply_device_change(http_request)

    return SelectedDeviceResponse(device_name=None)


@router.get("/input/selected")
async def get_selected_input_device() -> SelectedDeviceResponse:
    """Get the currently selected input device."""
    _ensure_loaded()
    return SelectedDeviceResponse(device_name=_selected_input_device)


@router.post("/input/selected")
async def set_selected_input_device(
    request: SetDeviceRequest, http_request: Request
) -> SelectedDeviceResponse:
    """Set the input device to use. The selection persists across restarts."""
    global _selected_input_device
    _ensure_loaded()

    device_names = [d.name for d in list_audio_devices("Audio/Source")]
    if request.device_name not in device_names:
        raise HTTPException(
            status_code=404,
            detail=f"Device '{request.device_name}' not found. Available: {device_names}",
        )

    changed = request.device_name != _selected_input_device
    _selected_input_device = request.device_name
    startup_app_config.set_selected_audio_input(_selected_input_device)
    logger.info(f"Input device set to: {_selected_input_device}")
    if changed:
        _apply_device_change(http_request)

    return SelectedDeviceResponse(device_name=_selected_input_device)


@router.delete("/input/selected")
async def clear_selected_input_device(http_request: Request) -> SelectedDeviceResponse:
    """Clear the selected input device (use default)."""
    global _selected_input_device
    _ensure_loaded()
    changed = _selected_input_device is not None
    _selected_input_device = None
    startup_app_config.set_selected_audio_input(None)
    logger.info("Input device cleared, using default")
    if changed:
        _apply_device_change(http_request)

    return SelectedDeviceResponse(device_name=None)


def get_local_selected_input() -> str | None:
    """Return the selected input device name from in-process state (no HTTP).

    For daemon-internal callers: a self-HTTP call would stall the busy event loop.
    """
    _ensure_loaded()
    return _selected_input_device


def get_local_selected_output() -> str | None:
    """Return the selected output device name from in-process state (no HTTP).

    See :func:`get_local_selected_input` for why daemon-internal callers avoid HTTP.
    """
    _ensure_loaded()
    return _selected_output_device


def get_selected_input() -> str | None:
    """Get the selected input device name (for out-of-process SDK clients).

    Tries the daemon API, then falls back to module state; daemon-internal
    callers should use :func:`get_local_selected_input`.
    """
    try:
        response = requests.get(
            f"{DAEMON_BASE_URL}/api/audio-devices/input/selected",
            timeout=DAEMON_API_TIMEOUT,
        )
        if response.status_code == 200:
            data = response.json()
            device_name: str | None = data.get("device_name", None)
            return device_name
    except (requests.RequestException, ValueError) as e:
        logger.debug(f"Could not fetch input device from daemon API: {e}")

    return get_local_selected_input()


def get_selected_output() -> str | None:
    """Get the selected output device name (for out-of-process SDK clients).

    Tries the daemon API, then falls back to module state; daemon-internal
    callers should use :func:`get_local_selected_output`.
    """
    try:
        response = requests.get(
            f"{DAEMON_BASE_URL}/api/audio-devices/output/selected",
            timeout=DAEMON_API_TIMEOUT,
        )
        if response.status_code == 200:
            data = response.json()
            device_name: str | None = data.get("device_name", None)
            return device_name
    except (requests.RequestException, ValueError) as e:
        logger.debug(f"Could not fetch output device from daemon API: {e}")

    return get_local_selected_output()
