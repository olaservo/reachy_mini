"""Tests for the audio-devices selection router.

Uses the captured ``gst-device-monitor-1.0`` PipeWire dump from
``tests/unit_tests/data/`` in place of a live ``Gst.DeviceMonitor``, so the
listing, the ``aec`` flag, the ALSA-card resolution and the persistence of a
selection are all testable without audio hardware.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from reachy_mini.daemon import daemon_config
from reachy_mini.daemon.app.routers import audio_devices
from reachy_mini.media.device_detection import DeviceInfo
from test_device_detection import _parse

_BLUEZ_SINK = DeviceInfo(
    display_name="JBL Clip 5",
    device_class="Audio/Sink",
    properties={
        "node.name": "bluez_output.0C_E0_E4_00_00_00.1",
        "device.api": "bluez5",
        "device.class": "sound",
    },
)


@pytest.fixture()
def sinks() -> List[DeviceInfo]:
    """The Audio/Sink devices from the captured PipeWire dump."""
    return [
        d
        for d in _parse("gst-device-monitor-linux-pipewire.txt")
        if d.device_class == "Audio/Sink"
    ]


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point persistence at a temp file and reset the module's cached selection.

    The router keeps its selection in module globals, so without this a test
    would leak its selection into the next one.
    """
    monkeypatch.setattr(
        daemon_config, "_config_path", lambda: tmp_path / "daemon_config.json"
    )
    monkeypatch.setattr(audio_devices, "_selected_input_device", None)
    monkeypatch.setattr(audio_devices, "_selected_output_device", None)
    monkeypatch.setattr(audio_devices, "_loaded", False)


def _fake_request() -> Any:
    """A Request stand-in with no daemon, so applying a change is a no-op."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


def _by_name(devices: List[DeviceInfo], name: str) -> DeviceInfo:
    return next(d for d in devices if d.display_name == name)


class TestAecFlag:
    """Only the XMOS card provides hardware echo cancellation."""

    def test_builtin_xmos_card_has_aec(self, sinks: List[DeviceInfo]) -> None:
        device = _by_name(sinks, "Reachy Mini Audio Analog Stereo")
        assert audio_devices._has_hardware_aec(device) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Built-in Audio Analog Stereo",
            "GA106 High Definition Audio Controller Digital Stereo (HDMI)",
        ],
    )
    def test_other_alsa_cards_have_no_aec(
        self, sinks: List[DeviceInfo], name: str
    ) -> None:
        assert audio_devices._has_hardware_aec(_by_name(sinks, name)) is False

    def test_bluetooth_sink_has_no_aec(self) -> None:
        # No alsa.card_name at all, so this exercises the display-name fallback.
        assert audio_devices._has_hardware_aec(_BLUEZ_SINK) is False


class TestListing:
    """The device list carries a name and an aec flag per device."""

    def test_lists_names_with_aec(
        self, sinks: List[DeviceInfo], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(audio_devices, "_filtered_devices", lambda _cls: sinks)
        devices = audio_devices.list_audio_devices("Audio/Sink")
        assert {d.name: d.aec for d in devices} == {
            "Built-in Audio Analog Stereo": False,
            "Reachy Mini Audio Analog Stereo": True,
            "GA106 High Definition Audio Controller Digital Stereo (HDMI)": False,
        }

    def test_deduplicates_by_name(
        self, sinks: List[DeviceInfo], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(audio_devices, "_filtered_devices", lambda _cls: sinks * 2)
        devices = audio_devices.list_audio_devices("Audio/Sink")
        assert len(devices) == len(sinks)


class TestSelectionPersistence:
    """A selection must survive a daemon restart — the point of Stage 3."""

    def test_selecting_an_output_persists_it(
        self, sinks: List[DeviceInfo], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(audio_devices, "_filtered_devices", lambda _cls: sinks)
        asyncio.run(
            audio_devices.set_selected_output_device(
                audio_devices.SetDeviceRequest(
                    device_name="Built-in Audio Analog Stereo"
                ),
                _fake_request(),
            )
        )
        assert (
            daemon_config.get_selected_audio_output() == "Built-in Audio Analog Stereo"
        )

    def test_selection_is_restored_after_a_restart(
        self, sinks: List[DeviceInfo], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon_config.set_selected_audio_output("Reachy Mini Audio Analog Stereo")
        daemon_config.set_selected_audio_input("Reachy Mini Audio Analog Stereo")
        # Fresh process: nothing loaded yet, as after a daemon restart.
        assert audio_devices.get_local_selected_output() == (
            "Reachy Mini Audio Analog Stereo"
        )
        assert audio_devices.get_local_selected_input() == (
            "Reachy Mini Audio Analog Stereo"
        )

    def test_clearing_an_output_clears_the_persisted_value(
        self, sinks: List[DeviceInfo], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(audio_devices, "_filtered_devices", lambda _cls: sinks)
        daemon_config.set_selected_audio_output("Built-in Audio Analog Stereo")
        asyncio.run(audio_devices.clear_selected_output_device(_fake_request()))
        assert daemon_config.get_selected_audio_output() is None
        assert audio_devices.get_local_selected_output() is None

    def test_unknown_device_is_rejected_and_not_persisted(
        self, sinks: List[DeviceInfo], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import HTTPException

        monkeypatch.setattr(audio_devices, "_filtered_devices", lambda _cls: sinks)
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(
                audio_devices.set_selected_output_device(
                    audio_devices.SetDeviceRequest(device_name="No Such Speaker"),
                    _fake_request(),
                )
            )
        assert excinfo.value.status_code == 404
        assert daemon_config.get_selected_audio_output() is None

    def test_audio_selection_does_not_clobber_the_startup_app(
        self, sinks: List[DeviceInfo], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both settings share one JSON file; a write of either must keep the other.
        monkeypatch.setattr(audio_devices, "_filtered_devices", lambda _cls: sinks)
        daemon_config.set_startup_app("my_app")
        asyncio.run(
            audio_devices.set_selected_output_device(
                audio_devices.SetDeviceRequest(
                    device_name="Built-in Audio Analog Stereo"
                ),
                _fake_request(),
            )
        )
        assert daemon_config.get_startup_app() == "my_app"
