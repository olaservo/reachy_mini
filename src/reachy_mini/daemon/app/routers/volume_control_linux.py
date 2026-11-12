"""Volume control implementation for Linux systems."""

import logging
import re
import subprocess
from dataclasses import dataclass

from .volume_control import (
    AudioDevice,
    AudioDeviceType,
    VolumeControl,
)

logger = logging.getLogger(__name__)


def _active_external_card() -> str | None:
    """Return the ALSA card index of the selected external output, else None.

    Lets the output volume follow whichever external speaker is selected. Returns
    ``None`` for the built-in XMOS card, for a Bluetooth sink (no ALSA card to
    set), and when nothing is selected — in each case the caller falls back to the
    built-in card. Never enumerates devices; see ``audio_external_output`` for why
    that matters here. Imported lazily to match ``volume_control._selected_targets``
    and keep the router import graph acyclic.
    """
    from reachy_mini.daemon.app.routers.audio_external_output import (
        selected_external_alsa_card,
    )

    return selected_external_alsa_card()


try:
    import pulsectl

    with pulsectl.Pulse("dummy"):
        pass
    _PULSECTL_AVAILABLE = True
except (ImportError, OSError, pulsectl.PulseError):
    _PULSECTL_AVAILABLE = False

# Constants
AUDIO_COMMAND_TIMEOUT = 2  # Timeout in seconds for audio commands


@dataclass
class VolumeControlLinux(VolumeControl):
    """Volume control class for Linux systems.

    Uses pulsectl (PulseAudio/PipeWire) when available, falls back to ALSA (amixer/aplay/arecord) otherwise. Not using pyalsaaudio as fallback as the installation will fail if libasound2 is not present.
    """

    def __post_init__(self) -> None:
        """Log the active backend."""
        logger.info(
            f"Using {'pulsectl (PulseAudio/PipeWire)' if _PULSECTL_AVAILABLE else 'amixer (ALSA)'} backend"
        )

    # ---- Dispatch methods ----

    def _get_all_devices(self) -> dict[int | str, str]:
        """Get all available audio devices IDs and names.

        Returns:
            A dictionary mapping device IDs to device names.

        Raises:
            RuntimeError: If the audio device scan fails.

        """
        if _PULSECTL_AVAILABLE:
            return self._pulse_get_all_devices()
        return self._alsa_get_all_devices()

    def _get_input_output_devices(
        self, input_target: str | None, output_target: str | None
    ) -> tuple[AudioDevice, AudioDevice]:
        """Get the input and output audio devices to control.

        Uses the user-selected device when set, else the Reachy Mini Audio
        sound card, else the platform default. Always initializes the Reachy
        Mini ALSA card's index-1 controls to 100% (an XMOS hardware quirk),
        independent of the selection.

        Returns:
            A tuple of two AudioDevice: (input_device, output_device).

        """
        # Resolve the Reachy Mini ALSA card (selection ignored) and set its
        # index-1 controls to 100% (an XMOS hardware quirk).
        reachy_input, reachy_output = self._alsa_get_input_output_devices(None, None)
        self._initialize_device(reachy_input)
        self._initialize_device(reachy_output)
        # External USB cards reset to their own (often low) hardware default on
        # re-enumeration, and `alsactl store` does not reliably survive a cold
        # power-cycle — so raise the selected external sink to 100% at index 0
        # (the index-1 calls above are a no-op for USB cards).
        active_card = _active_external_card()
        if active_card:
            self._initialize_device(
                AudioDevice(active_card, active_card, AudioDeviceType.OUTPUT), index=0
            )

        if _PULSECTL_AVAILABLE:
            return self._pulse_get_input_output_devices(input_target, output_target)
        return self._alsa_get_input_output_devices(input_target, output_target)

    def _get_device_volume(self, device: AudioDevice) -> int:
        """Get the volume of an audio device.

        Args:
            device: The audio device.

        Returns:
            The volume as a value between 0 and 100. Returns -1 if the volume could not be read.

        """
        if _PULSECTL_AVAILABLE:
            return self._pulse_get_device_volume(device)
        return self._alsa_get_device_volume(device)

    def _set_device_volume(self, device: AudioDevice, volume: int) -> bool:
        """Set the volume of an audio device.

        Args:
            device: The audio device.
            volume: The volume to set between 0 (minimum volume) and 100 (maximum volume).

        Returns:
            True if the volume was set successfully, False otherwise.

        """
        if _PULSECTL_AVAILABLE:
            return self._pulse_set_device_volume(device, volume)
        return self._alsa_set_device_volume(device, volume)

    # ---- PulseAudio/PipeWire (pulsectl) backend ----

    def _pulse_get_all_devices(
        self, device_type: AudioDeviceType | None = None
    ) -> dict[int | str, str]:
        """Get all available audio devices IDs and names via pulsectl.

        Args:
            device_type: The type of device: INPUT or OUTPUT. If None, returns all devices.

        Returns:
            A dictionary containing the name of each audio device: {name: str, name: str, ...}. Monitor sources and sinks are not included.

        Raises:
            RuntimeError: If pulsectl fails when getting all audio devices.

        """
        devices: dict[int | str, str] = {}
        try:
            with pulsectl.Pulse("reachy-mini") as pulse:
                if device_type == AudioDeviceType.OUTPUT or device_type is None:
                    for sink in pulse.sink_list():
                        devices[sink.name] = (
                            sink.description or f"Unknown device (id={sink.name})"
                        )
                if device_type == AudioDeviceType.INPUT or device_type is None:
                    for source in pulse.source_list():
                        if not source.monitor_of_sink_name:
                            devices[source.name] = (
                                source.description
                                or f"Unknown device (id={source.name})"
                            )
        except Exception as e:
            raise RuntimeError(
                f"Could not scan audio devices, pulsectl failed with error: {e}"
            )
        return devices

    def _pulse_get_input_output_devices(
        self, input_target: str | None, output_target: str | None
    ) -> tuple[AudioDevice, AudioDevice]:
        """Get the input and output audio devices via pulsectl.

        Uses the user-selected device when set, else the Reachy Mini card,
        else the default sink/source.

        Returns:
            A tuple of two AudioDevice: (input_device, output_device).

        """
        input_devices = self._pulse_get_all_devices(AudioDeviceType.INPUT)
        output_devices = self._pulse_get_all_devices(AudioDeviceType.OUTPUT)

        # Input and output devices will appear with different IDs
        input_device: AudioDevice | None = None
        output_device: AudioDevice | None = None
        input_match = self._find_device(input_devices, input_target)
        if input_match is not None:
            input_device = AudioDevice(
                input_match[0], input_match[1], AudioDeviceType.INPUT
            )
        output_match = self._find_device(output_devices, output_target)
        if output_match is not None:
            output_device = AudioDevice(
                output_match[0], output_match[1], AudioDeviceType.OUTPUT
            )

        # Fall back to default devices if no matching device found
        if input_device is None:
            default_id = self._pulse_get_default_device(AudioDeviceType.INPUT)
            input_device = AudioDevice(
                default_id,
                input_devices.get(default_id, f"Unknown device (id={default_id})"),
                AudioDeviceType.INPUT,
            )
        if output_device is None:
            default_id = self._pulse_get_default_device(AudioDeviceType.OUTPUT)
            output_device = AudioDevice(
                default_id,
                output_devices.get(default_id, f"Unknown device (id={default_id})"),
                AudioDeviceType.OUTPUT,
            )

        return input_device, output_device

    def _pulse_get_default_device(self, device_type: AudioDeviceType) -> str:
        """Get the default audio device ID for a given type via pulsectl.

        Args:
            device_type: The type of device: INPUT or OUTPUT.

        Returns:
            The default audio device ID.

        Raises:
            RuntimeError: If pulsectl fails when getting the default audio device.

        """
        try:
            with pulsectl.Pulse("reachy-mini") as pulse:
                server_info = pulse.server_info()
                if device_type == AudioDeviceType.INPUT:
                    return str(server_info.default_source_name)
                return str(server_info.default_sink_name)
        except Exception as e:
            raise RuntimeError(
                f"Failed to get default {device_type.value} device via pulsectl: {e}"
            )

    def _pulse_get_device_volume(self, device: AudioDevice) -> int:
        """Get the volume of an audio device via pulsectl.

        Args:
            device: The audio device.

        Returns:
            The volume as a value between 0 and 100. Returns -1 on failure.

        """
        try:
            with pulsectl.Pulse("reachy-mini") as pulse:
                if device.device_type == AudioDeviceType.INPUT:
                    pulse_device = pulse.get_source_by_name(device.id)
                else:
                    pulse_device = pulse.get_sink_by_name(device.id)
                return round(float(pulse.volume_get_all_chans(pulse_device)) * 100)
        except Exception as e:
            logger.error(
                f"Failed to get volume on device {device.id} - pulsectl error: {e}"
            )
            return -1

    def _pulse_set_device_volume(self, device: AudioDevice, volume: int) -> bool:
        """Set the volume of an audio device via pulsectl.

        Args:
            device: The audio device.
            volume: The volume to set between 0 (minimum volume) and 100 (maximum volume).

        Returns:
            True if the volume was set successfully, False otherwise.

        """
        # Clamp and convert to 0.0-1.0 for pulsectl API
        volume_scalar = max(0.0, min(1.0, volume / 100.0))
        try:
            with pulsectl.Pulse("reachy-mini") as pulse:
                if device.device_type == AudioDeviceType.INPUT:
                    pulse_device = pulse.get_source_by_name(device.id)
                else:
                    pulse_device = pulse.get_sink_by_name(device.id)
                pulse.volume_set_all_chans(pulse_device, volume_scalar)
            return True
        except Exception as e:
            logger.error(
                f"Failed to set volume on device {device.id} - pulsectl error: {e}"
            )
            return False

    # ---- ALSA (amixer/aplay/arecord) backend ----

    def _alsa_get_device_controls(self, device: AudioDevice) -> list[str]:
        """Get the list of ALSA control names for a given device.

        Queries the card via ``amixer scontents`` and filters controls based on
        their capabilities: ``pvolume`` for output (playback) and ``cvolume``
        for input (capture).

        Args:
            device: The audio device.

        Returns:
            The list of ALSA control names matching the requested type.

        """
        capability = (
            "pvolume" if device.device_type == AudioDeviceType.OUTPUT else "cvolume"
        )

        command = (
            ["amixer", "-c", str(device.id), "scontents"]
            if device.id is not None
            else ["amixer", "scontents"]
        )

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=AUDIO_COMMAND_TIMEOUT,
                check=True,
            )
        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
        ) as e:
            logger.warning(f"Failed to list controls for device {device.id}: {e}")
            return []

        controls: list[str] = []
        current_control: str | None = None
        control_pattern = re.compile(r"Simple mixer control '([^']+)',(\d+)")

        for line in result.stdout.splitlines():
            match = control_pattern.match(line)
            if match:
                current_control = match.group(1)
                continue
            if current_control and capability in line:
                if current_control not in controls:
                    controls.append(current_control)

        return controls

    def _initialize_device(self, device: AudioDevice, index: int = 1) -> None:
        """Set the first ALSA volume control that accepts the given index to 100%.

        Tries the device's volume controls in order and stops at the first one
        that has the index, like the ``||`` chain this replaced.

        Args:
            device: The audio device. If its ID is None, uses the default audio device.
            index: The ALSA control index to set. The built-in XMOS card uses
                index-1 controls; plain USB cards expose their control at index 0.

        """
        try:
            self._run_amixer_on_controls(device, "sset", index=index, value="100%")
        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
            RuntimeError,
        ) as e:
            logger.warning(f"Failed to initialize {device.id} device: {e}")

    def _alsa_get_all_devices(self) -> dict[int | str, str]:
        """Get all available audio devices IDs and names via ALSA.

        Returns:
            A dictionary containing the ID and name of each audio device: {id: int, name: str, ...}.

        Raises:
            RuntimeError: If both aplay and arecord fail when getting all audio devices.

        """
        # Playback cards from aplay, capture cards from arecord.
        output = ""
        errors: list[str] = []
        for tool in ("aplay", "arecord"):
            try:
                output += subprocess.run(
                    [tool, "-l"],
                    capture_output=True,
                    text=True,
                    timeout=AUDIO_COMMAND_TIMEOUT,
                    check=True,
                ).stdout
            except (
                subprocess.TimeoutExpired,
                FileNotFoundError,
                subprocess.CalledProcessError,
            ) as e:
                errors.append(f"{tool}: {e}")
        if len(errors) == 2:
            raise RuntimeError(f"Could not scan audio devices: {'; '.join(errors)}")
        if errors:
            logger.warning(f"Partial audio device scan: {errors[0]}")

        devices: dict[int | str, str] = {}
        pattern = re.compile(r"card\s+(\d+):\s+[^[]+\[([^\]]+)\]")
        for line in output.splitlines():
            match = pattern.search(line)
            if match:
                devices.setdefault(int(match.group(1)), match.group(2))
        return devices

    def _alsa_get_input_output_devices(
        self, input_target: str | None, output_target: str | None
    ) -> tuple[AudioDevice, AudioDevice]:
        """Get the input and output audio devices via ALSA.

        Uses the user-selected device when set. For output, otherwise follows
        the active ``reachymini_audio_sink`` card so the volume slider controls
        whichever speaker is selected (built-in XMOS or an external USB card via
        the ``.asoundrc`` rewrite). Falls back to the Reachy Mini card / default.

        Returns:
            A tuple of two AudioDevice: (input_device, output_device).

        """
        devices = self._alsa_get_all_devices()

        # Input: an explicit user selection wins, else the Reachy Mini card.
        input_match = self._find_device(devices, input_target)
        input_device = (
            AudioDevice(input_match[0], input_match[1], AudioDeviceType.INPUT)
            if input_match is not None
            else AudioDevice(None, "Default", AudioDeviceType.INPUT)
        )

        # Output: when an external card is selected the slider must target that
        # card rather than the built-in one — amixer takes the card index
        # directly (``amixer -c <index>``). Everything else (no selection, the
        # built-in card, or a Bluetooth sink with no ALSA card) falls through to
        # the name match, which lands on the Reachy Mini card.
        active_card = _active_external_card()
        if active_card:
            output_device = AudioDevice(
                active_card, output_target or active_card, AudioDeviceType.OUTPUT
            )
        else:
            output_match = self._find_device(devices, output_target)
            output_device = (
                AudioDevice(output_match[0], output_match[1], AudioDeviceType.OUTPUT)
                if output_match is not None
                else AudioDevice(None, "Default", AudioDeviceType.OUTPUT)
            )
        return input_device, output_device

    def _run_amixer_on_controls(
        self,
        device: AudioDevice,
        action: str,
        index: int = 0,
        value: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``amixer sget|sset <control>,<index> [value]`` on the device's controls until one succeeds.

        Args:
            device: The audio device. If its ID is None, uses the default card.
            action: ``"sget"`` or ``"sset"``.
            index: The ALSA control index. Defaults to 0.
            value: The value to set, for ``sset`` (e.g. ``"50%"``).

        Returns:
            The result of the first control that succeeded.

        Raises:
            subprocess.CalledProcessError: If every control failed.
            subprocess.TimeoutExpired: If amixer hangs. The next controls are not
                tried: like the old ``||`` chain, a hang fails the whole command.
            RuntimeError: If the device has no volume control.

        """
        card = ["-c", str(device.id)] if device.id is not None else []
        error: Exception = RuntimeError(
            f"no ALSA {device.device_type.value} volume control found on card "
            f"{device.id if device.id is not None else 'default'}"
        )
        for control in self._alsa_get_device_controls(device):
            # Argument list, no shell: control names can contain spaces.
            cmd = ["amixer", *card, action, f"{control},{index}"]
            if value is not None:
                cmd.append(value)
            try:
                return subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=AUDIO_COMMAND_TIMEOUT,
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                error = e
        raise error

    def _alsa_get_device_volume(self, device: AudioDevice) -> int:
        """Get the volume of an audio device via amixer.

        Args:
            device: The audio device.

        Returns:
            The volume as a value between 0 and 100. Returns -1 on failure.

        """
        try:
            result = self._run_amixer_on_controls(device, "sget")
            for line in result.stdout.splitlines():
                # TODO: add support for other channels ?
                if "Left:" in line and "[" in line:
                    parts = line.split("[")
                    for part in parts:
                        if "%" in part:
                            volume_str = part.split("%")[0]
                            return int(volume_str)

        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            ValueError,
            subprocess.CalledProcessError,
            RuntimeError,
        ) as e:
            logger.error(f"Failed to get volume on device {device.id}: {e}")

        return -1

    def _alsa_set_device_volume(self, device: AudioDevice, volume: int) -> bool:
        """Set the volume of an audio device via amixer.

        Args:
            device: The audio device.
            volume: The volume to set between 0 (minimum volume) and 100 (maximum volume).

        Returns:
            True if the volume was set successfully, False otherwise.

        """
        try:
            self._run_amixer_on_controls(
                device, "sset", value=f"{max(0, min(100, volume))}%"
            )
            return True

        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
            RuntimeError,
        ) as e:
            logger.error(f"Failed to set volume on device {device.id}: {e}")
            return False

    # ---- Public API ----

    def get_output_volume(self) -> int:
        """Get the output volume.

        Returns:
            The output volume as a value between 0 (minimum volume) and 100 (maximum volume). Returns -1 on failure.

        """
        return self._get_device_volume(self.output_device)

    def set_output_volume(self, volume: int) -> bool:
        """Set the output volume.

        Args:
            volume: The volume to set between 0 (minimum volume) and 100 (maximum volume).

        Returns:
            True if the volume was set successfully, False otherwise.

        """
        return self._set_device_volume(self.output_device, volume)

    def get_input_volume(self) -> int:
        """Get the input volume.

        Returns:
            The input volume as a value between 0 (minimum volume) and 100 (maximum volume). Returns -1 on failure.

        """
        return self._get_device_volume(self.input_device)

    def set_input_volume(self, volume: int) -> bool:
        """Set the input volume.

        Args:
            volume: The volume to set between 0 (minimum volume) and 100 (maximum volume).

        Returns:
            True if the volume was set successfully, False otherwise.

        """
        return self._set_device_volume(self.input_device, volume)

    # ---- Debug / info ----

    def _get_device_controls(self, device_id: int) -> list[str]:
        """Get ALSA controls of an audio device given its ID.

        Args:
            device_id: The ALSA card number.

        Returns:
            A list of ALSA controls.

        """
        controls = []
        try:
            result = subprocess.run(
                ["amixer", "-c", str(device_id), "scontrols"],
                capture_output=True,
                text=True,
                timeout=AUDIO_COMMAND_TIMEOUT,
                check=True,
            )
            for line in result.stdout.splitlines():
                if "Simple mixer control" in line:
                    start = line.find("'") + 1
                    end = line.find("'", start)
                    if start > 0 and end > start:
                        controls.append(line[start:end])

        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
        ) as e:
            logger.error(
                f"Failed to list controls for device {device_id} - amixer failed with error: {e}"
            )
            return []

        return controls

    def get_information(self) -> dict[str, int | str | None | list[str] | bool]:
        """Get information about the controlled audio devices.

        Returns:
            A dictionary containing the information about the controlled audio devices.

        """
        info: dict[str, int | str | None | list[str] | bool] = {
            "backend": "pulsectl" if _PULSECTL_AVAILABLE else "alsa",
            "input_device_id": self.input_device.id,
            "output_device_id": self.output_device.id,
        }
        if not _PULSECTL_AVAILABLE and isinstance(self.input_device.id, int):
            info["available_input_controls"] = self._get_device_controls(
                self.input_device.id
            )
        if not _PULSECTL_AVAILABLE and isinstance(self.output_device.id, int):
            info["available_output_controls"] = self._get_device_controls(
                self.output_device.id
            )
        return info
