"""Unit tests for the shared appsrc gap-aware buffer push helper."""

import time
from types import SimpleNamespace
from typing import cast

import gi
import numpy as np

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from reachy_mini.media.audio_base import AudioBase  # noqa: E402

Gst.init([])


class _FakeAppsrc:
    """Stand-in for ``Gst.AppSrc`` capturing the pushed buffer."""

    def __init__(self, running_time_ns: int) -> None:
        self._running_time = running_time_ns
        self.pushed: Gst.Buffer | None = None

    def get_current_running_time(self) -> int:
        return self._running_time

    def push_buffer(self, buf: Gst.Buffer) -> Gst.FlowReturn:
        self.pushed = buf
        return Gst.FlowReturn.OK


def _fake_self(running_time_ns: int, prev_pts_ns: int) -> AudioBase:
    """Return a stand-in carrying just the attrs the helper touches."""
    return cast(
        AudioBase,
        SimpleNamespace(
            SAMPLE_RATE=AudioBase.SAMPLE_RATE,
            GAP_RESET_NS=AudioBase.GAP_RESET_NS,
            _appsrc=_FakeAppsrc(running_time_ns),
            _appsrc_pts=prev_pts_ns,
        ),
    )


def test_push_first_buffer_anchors_to_running_time() -> None:
    """First buffer after start/flush is stamped with running-time + DISCONT."""
    fake = _fake_self(running_time_ns=2_000_000_000, prev_pts_ns=-1)
    data = np.zeros(1600, dtype=np.float32)

    ret = AudioBase._push_appsrc_buffer(fake, data)

    assert ret == Gst.FlowReturn.OK
    buf = fake._appsrc.pushed
    assert buf is not None
    assert buf.has_flags(Gst.BufferFlags.DISCONT)
    assert buf.pts == 2_000_000_000
    assert buf.dts == 2_000_000_000
    assert fake._appsrc_pts == 2_100_000_000


def test_push_continues_without_gap() -> None:
    """Follow-up buffers within GAP_RESET_NS are placed contiguously, untimestamped."""
    fake = _fake_self(running_time_ns=1_050_000_000, prev_pts_ns=1_100_000_000)
    data = np.zeros(800, dtype=np.float32)

    AudioBase._push_appsrc_buffer(fake, data)

    buf = fake._appsrc.pushed
    assert buf is not None
    assert not buf.has_flags(Gst.BufferFlags.DISCONT)
    assert buf.pts == Gst.CLOCK_TIME_NONE
    assert buf.dts == Gst.CLOCK_TIME_NONE
    assert fake._appsrc_pts == 1_150_000_000


def test_push_resets_after_large_gap() -> None:
    """A gap larger than GAP_RESET_NS re-anchors to running-time + DISCONT."""
    fake = _fake_self(running_time_ns=1_400_000_000, prev_pts_ns=1_100_000_000)
    data = np.zeros(800, dtype=np.float32)

    AudioBase._push_appsrc_buffer(fake, data)

    buf = fake._appsrc.pushed
    assert buf is not None
    assert buf.has_flags(Gst.BufferFlags.DISCONT)
    assert buf.pts == 1_400_000_000
    assert fake._appsrc_pts == 1_450_000_000


def test_push_without_appsrc_returns_none() -> None:
    """Helper is a no-op when ``_appsrc`` is not initialized."""
    fake = cast(
        AudioBase,
        SimpleNamespace(
            SAMPLE_RATE=AudioBase.SAMPLE_RATE,
            GAP_RESET_NS=AudioBase.GAP_RESET_NS,
            _appsrc_pts=-1,
        ),
    )

    ret = AudioBase._push_appsrc_buffer(fake, np.zeros(100, dtype=np.float32))

    assert ret is None


def test_clear_player_flushes_past_the_mixer() -> None:
    """clear_player() flushes up to the sink, which keeps a segment and plays on."""
    from reachy_mini.media.audio_gstreamer import GStreamerAudio

    caps = "audio/x-raw,format=F32LE,channels=2,rate=16000,layout=interleaved"
    pipeline = Gst.parse_launch(
        f"appsrc name=src is-live=true format=time caps={caps} ! queue ! audiomixer name=mix"
        f" audiotestsrc is-live=true wave=silence ! {caps} ! queue ! mix."
        " mix. ! queue ! fakesink name=sink sync=true"
    )
    seen: list[str] = []

    def probe(pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        if info.type & Gst.PadProbeType.BUFFER:
            seen.append("buffer")
        elif info.get_event().type == Gst.EventType.FLUSH_STOP:
            seen.append("flush")
        return Gst.PadProbeReturn.OK

    sink_pad = pipeline.get_by_name("sink").get_static_pad("sink")
    sink_pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_FLUSH, probe)
    fake = SimpleNamespace(
        _appsrc=pipeline.get_by_name("src"),
        _mixer=pipeline.get_by_name("mix"),
        _pipeline=pipeline,
        _head_wobbler=None,
        _appsrc_pts=-1,
        logger=SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
    )

    def buffer_after_last_flush() -> bool:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if seen and seen[-1] == "buffer":
                return True
            time.sleep(0.01)
        return False

    pipeline.set_state(Gst.State.PLAYING)
    try:
        assert buffer_after_last_flush()

        GStreamerAudio.clear_player(fake)  # type: ignore[arg-type]

        assert "flush" in seen
        assert sink_pad.get_sticky_event(Gst.EventType.SEGMENT, 0) is not None
        # The mixer's output task survived the flush.
        assert buffer_after_last_flush()
    finally:
        pipeline.set_state(Gst.State.NULL)


from reachy_mini.media.audio_gstreamer import GStreamerAudio  # noqa: E402


class TestAudiosinkSpec:
    """The sink-selection matrix. The regression this guards: a selected output
    that can't be resolved (PipeWire lost its devices) must fall back to the
    built-in ALSA sink, never a pulsesink to a ghost (which spins/leaks/aborts)."""

    def test_unresolved_with_asoundrc_falls_back_to_builtin_alsa(self) -> None:
        # The fix: missing device + wireless -> direct-ALSA built-in sink.
        assert GStreamerAudio._audiosink_spec(None, "Linux", True) == (
            "alsasink",
            ("device", "reachymini_audio_sink"),
        )

    def test_unresolved_without_asoundrc_uses_autoaudiosink(self) -> None:
        # Non-wireless host with nothing resolved: platform default.
        assert GStreamerAudio._audiosink_spec(None, "Linux", False) == (
            "autoaudiosink",
            None,
        )

    def test_resolved_linux_uses_pulsesink_even_with_asoundrc(self) -> None:
        # A resolved node (e.g. a connected Bluetooth sink) uses pulsesink; the
        # .asoundrc fallback is ONLY for the unresolved case.
        assert GStreamerAudio._audiosink_spec(
            "bluez_output.AA_BB.1", "Linux", True
        ) == ("pulsesink", ("device", "bluez_output.AA_BB.1"))

    def test_resolved_windows_uses_wasapi(self) -> None:
        assert GStreamerAudio._audiosink_spec("id-1", "Windows", True) == (
            "wasapi2sink",
            ("device", "id-1"),
        )

    def test_resolved_darwin_uses_osx_unique_id(self) -> None:
        assert GStreamerAudio._audiosink_spec("uid-1", "Darwin", False) == (
            "osxaudiosink",
            ("unique-id", "uid-1"),
        )
