"""Tests for mapping a selected output to its ALSA card.

The mapping goes PipeWire display name -> ``aplay -l`` card index, deliberately
without a ``Gst.DeviceMonitor`` (which would disconnect Bluetooth, and is not
even usable in the first ~15s after a daemon start). The dump below is real
``aplay -l`` output from the Wireless unit.
"""

from __future__ import annotations

import pytest

from reachy_mini.daemon.app.routers import audio_external_output

# Verbatim `aplay -l` from the robot: XMOS on card 0, SABRENT USB card on card 1.
_APLAY_OUTPUT = """**** List of PLAYBACK Hardware Devices ****
card 0: Audio [Reachy Mini Audio], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 1: Device [USB Advanced Audio Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 3: vc4hdmi1 [vc4-hdmi-1], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""


@pytest.fixture(autouse=True)
def fake_aplay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the captured `aplay -l` dump and reset the module's lookup cache."""
    monkeypatch.setattr(
        audio_external_output,
        "_list_playback_cards",
        lambda: audio_external_output._parse_playback_cards(_APLAY_OUTPUT),
    )
    monkeypatch.setattr(audio_external_output, "_cached_card", None)
    monkeypatch.setattr(audio_external_output, "_cached_for", None)
    monkeypatch.setattr(audio_external_output, "_cache_valid", False)


class TestParsePlaybackCards:
    """`aplay -l` parsing."""

    def test_extracts_index_and_description(self) -> None:
        cards = audio_external_output._parse_playback_cards(_APLAY_OUTPUT)
        assert cards == [
            ("0", "Reachy Mini Audio"),
            ("1", "USB Advanced Audio Device"),
            ("2", "vc4-hdmi-0"),
            ("3", "vc4-hdmi-1"),
        ]

    def test_tolerates_junk(self) -> None:
        assert audio_external_output._parse_playback_cards("no cards here") == []


class TestResolveAlsaCard:
    """PipeWire display name -> ALSA card index."""

    def test_usb_card_resolves_by_substring(self) -> None:
        # PipeWire appends the profile to the card description; the aplay
        # description is a substring of it. Verified against the node's own
        # alsa.card property on the robot (card 1).
        assert (
            audio_external_output._resolve_alsa_card(
                "USB Advanced Audio Device Analog Stereo"
            )
            == "1"
        )

    def test_builtin_card_resolves_to_none(self) -> None:
        # The XMOS is driven via its index-1 controls by the built-in path.
        assert (
            audio_external_output._resolve_alsa_card("Reachy Mini Audio Analog Stereo")
            is None
        )

    def test_bluetooth_sink_resolves_to_none(self) -> None:
        # No ALSA card exists for a BT sink, so amixer cannot set its volume.
        assert audio_external_output._resolve_alsa_card("JBL Clip 5") is None

    def test_no_selection_resolves_to_none(self) -> None:
        assert audio_external_output._resolve_alsa_card(None) is None

    def test_absent_card_resolves_to_none(self) -> None:
        # e.g. the USB card was unplugged while the daemon was down.
        assert audio_external_output._resolve_alsa_card("Some Vanished Speaker") is None


class TestSelectedExternalAlsaCard:
    """The cached, selection-driven lookup used by the volume path."""

    def test_follows_the_selection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            audio_external_output,
            "get_local_selected_output",
            lambda: "USB Advanced Audio Device Analog Stereo",
        )
        assert audio_external_output.selected_external_alsa_card() == "1"

    def test_no_selection_means_builtin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            audio_external_output, "get_local_selected_output", lambda: None
        )
        assert audio_external_output.selected_external_alsa_card() is None

    def test_reresolves_when_the_selection_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        selection = {"name": "USB Advanced Audio Device Analog Stereo"}
        monkeypatch.setattr(
            audio_external_output, "get_local_selected_output", lambda: selection["name"]
        )
        assert audio_external_output.selected_external_alsa_card() == "1"
        selection["name"] = None
        assert audio_external_output.selected_external_alsa_card() is None

    def test_caches_between_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The volume path calls this on every read; it must not shell out each time.
        calls = []
        monkeypatch.setattr(
            audio_external_output,
            "get_local_selected_output",
            lambda: "USB Advanced Audio Device Analog Stereo",
        )

        def counting_list() -> list[tuple[str, str]]:
            calls.append(1)
            return audio_external_output._parse_playback_cards(_APLAY_OUTPUT)

        monkeypatch.setattr(audio_external_output, "_list_playback_cards", counting_list)
        audio_external_output.selected_external_alsa_card()
        audio_external_output.selected_external_alsa_card()
        audio_external_output.selected_external_alsa_card()
        assert len(calls) == 1
