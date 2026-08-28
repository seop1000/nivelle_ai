from __future__ import annotations

import struct
import threading
from typing import Any

import numpy as np
import pytest
from nivelle_link.audio_widgets import (
    RealtimeAudioAnalysisPage,
    RealtimeSpectrogramWidget,
)
from nivelle_link.realtime_audio import (
    AudioInputDevice,
    PcmStreamDecoder,
    RealtimeAnalysisWorker,
    RealtimeAudioAnalyzer,
)
from PySide6.QtCore import QObject, Signal


def tone(
    frequency: float,
    *,
    sample_rate: int = 48_000,
    sample_count: int = 16_384,
    amplitude: float = 0.5,
) -> np.ndarray[Any, np.dtype[np.float32]]:
    time = np.arange(sample_count, dtype=np.float32) / sample_rate
    return (amplitude * np.sin(2.0 * np.pi * frequency * time)).astype(np.float32)


def analyze(samples: np.ndarray[Any, np.dtype[np.float32]], sample_rate: int = 48_000) -> Any:
    result = RealtimeAudioAnalyzer(sample_rate).push(samples)
    assert result is not None
    return result


def level_near(result: Any, frequency: float) -> float:
    index = min(
        range(len(result.spectrum_frequencies_hz)),
        key=lambda item: abs(result.spectrum_frequencies_hz[item] - frequency),
    )
    return float(result.spectrum_dbfs[index])


def test_silence_does_not_report_peak_or_fundamental_frequency() -> None:
    result = analyze(np.zeros(8_192, dtype=np.float32))

    assert result.peak_frequency_hz is None
    assert result.fundamental_frequency_hz is None
    assert result.rms == 0.0
    assert result.rms_dbfs == -120.0
    assert result.peak_dbfs == -120.0


def test_broadband_noise_does_not_report_an_unreliable_frequency() -> None:
    random = np.random.default_rng(1234)
    noise = random.normal(0.0, 0.1, 16_384).astype(np.float32)

    result = analyze(noise)

    assert result.peak_frequency_hz is None
    assert result.fundamental_frequency_hz is None


def test_known_tone_uses_interpolated_peak_and_separate_f0() -> None:
    result = analyze(tone(437.8))

    assert result.peak_frequency_hz == pytest.approx(437.8, abs=2.0)
    assert result.fundamental_frequency_hz == pytest.approx(437.8, abs=2.0)
    assert result.rms_dbfs == pytest.approx(-9.0, abs=0.5)
    assert result.peak_dbfs == pytest.approx(-6.0, abs=0.5)


def test_actual_nonpreferred_sample_rate_is_preserved() -> None:
    result = analyze(tone(330.0, sample_rate=44_100), sample_rate=44_100)

    assert result.sample_rate == 44_100
    assert result.peak_frequency_hz == pytest.approx(330.0, abs=2.0)


def test_voiced_harmonics_produce_fundamental_frequency() -> None:
    signal = tone(120.0, amplitude=0.5)
    signal += tone(240.0, amplitude=0.25)
    signal += tone(360.0, amplitude=0.1)

    result = analyze(signal)

    assert result.fundamental_frequency_hz == pytest.approx(120.0, abs=2.0)
    assert result.peak_frequency_hz == pytest.approx(120.0, abs=2.0)


def test_complex_signal_keeps_multiple_visible_spectrum_components() -> None:
    signal = tone(440.0, amplitude=0.5) + tone(1_000.0, amplitude=0.3)

    result = analyze(signal)

    assert level_near(result, 440.0) > -15.0
    assert level_near(result, 1_000.0) > -20.0
    assert len(result.spectrum_frequencies_hz) <= RealtimeAudioAnalyzer.SPECTRUM_POINTS


def test_analysis_ring_buffer_remains_bounded_during_long_input() -> None:
    analyzer = RealtimeAudioAnalyzer(48_000)
    chunk = tone(220.0, sample_count=2_048)

    for _ in range(2_000):
        analyzer.push(chunk)

    assert analyzer.ring_buffer.size == analyzer.ring_buffer.capacity
    assert analyzer.ring_buffer.capacity == 48_000


def test_pcm_stream_decoder_preserves_partial_multichannel_frames() -> None:
    payload = struct.pack("<hhhh", 16_384, -16_384, 8_192, 8_192)
    decoder = PcmStreamDecoder("int16", channels=2)

    first = decoder.decode(payload[:3])
    second = decoder.decode(payload[3:])

    assert first.size == 0
    assert second.tolist() == pytest.approx([0.0, 0.25])


def test_worker_start_stop_cycles_release_thread_and_bound_queue() -> None:
    completed = threading.Event()
    results: list[object] = []
    errors: list[str] = []
    samples = tone(440.0, sample_count=4_096)
    payload = (samples * 32_767.0).astype("<i2").tobytes()

    for _ in range(5):
        worker = RealtimeAnalysisWorker(
            sample_rate=48_000,
            sample_format="int16",
            channels=1,
            result_callback=lambda result: (results.append(result), completed.set()),
            error_callback=errors.append,
        )
        completed.clear()
        worker.start()
        for _ in range(20):
            worker.submit(payload)
        assert completed.wait(2.0)
        assert worker.queued_chunks <= worker.QUEUE_CAPACITY
        worker.stop()
        assert not worker.running
        assert worker.queued_chunks == 0
        assert not any(
            thread.name == "nivelle-realtime-audio-analysis" for thread in threading.enumerate()
        )

    assert results
    assert errors == []


class FakeAudioEngine(QObject):
    analysis_ready = Signal(object)
    state_changed = Signal(str)
    error_occurred = Signal(str)
    devices_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.devices = [AudioInputDevice(b"one", "첫 번째 마이크", True)]
        self.started: list[bytes | None] = []
        self.stop_count = 0
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def available_devices(self) -> list[AudioInputDevice]:
        return list(self.devices)

    def start(self, device_id: bytes | None = None) -> bool:
        self.started.append(device_id)
        self._running = True
        self.state_changed.emit("분석 중")
        return True

    def stop(self, *, emit_state: bool = True) -> None:
        self.stop_count += 1
        self._running = False
        if emit_state:
            self.state_changed.emit("중지됨")


def test_realtime_page_switches_input_without_app_restart(qtbot: Any) -> None:
    engine = FakeAudioEngine()
    page = RealtimeAudioAnalysisPage(engine=engine)  # type: ignore[arg-type]
    qtbot.addWidget(page)

    assert page.device_select.currentText() == "첫 번째 마이크 (기본)"
    page.start_button.click()
    assert engine.started == [b"one"]
    assert page.ui_timer.interval() == 40

    engine.devices.append(AudioInputDevice(b"two", "두 번째 마이크"))
    page.refresh_devices()
    page.device_select.setCurrentIndex(1)

    assert engine.started[-1] == b"two"
    assert "분석 중" in page.status.text()
    page.stop_button.click()
    assert not engine.running


def test_device_disconnect_and_analysis_error_do_not_close_page(qtbot: Any) -> None:
    engine = FakeAudioEngine()
    page = RealtimeAudioAnalysisPage(engine=engine)  # type: ignore[arg-type]
    qtbot.addWidget(page)
    page.start_button.click()

    engine.error_occurred.emit("사용 중인 입력 장치의 연결이 해제되었습니다.")
    engine._running = False
    engine.devices = []
    engine.devices_changed.emit()

    assert "연결이 해제" in page.status.text() or "입력 장치 없음" in page.status.text()
    assert page.isEnabled()
    assert not page.start_button.isEnabled()


def test_spectrogram_history_is_bounded(qtbot: Any) -> None:
    widget = RealtimeSpectrogramWidget()
    qtbot.addWidget(widget)
    frequencies = tuple(float(value) for value in np.geomspace(20.0, 20_000.0, 128))
    levels = tuple(-60.0 for _ in frequencies)

    for _ in range(widget.MAX_COLUMNS + 50):
        widget.append_column(frequencies, levels, sample_rate=48_000)

    assert widget.column_count == widget.MAX_COLUMNS


def test_realtime_page_renders_korean_metrics(qtbot: Any) -> None:
    engine = FakeAudioEngine()
    page = RealtimeAudioAnalysisPage(engine=engine)  # type: ignore[arg-type]
    qtbot.addWidget(page)
    result = analyze(tone(440.0))

    engine.analysis_ready.emit(result)
    page._render_pending()

    assert "Hz" in page.peak_frequency.text()
    assert "dBFS" in page.current_volume.text()
    assert page.sample_rate.text() == "48,000 Hz"
    assert page.clipping_status.text() == "정상"
    assert page.spectrogram.column_count == 1


def test_realtime_page_shutdown_is_repeatable(qtbot: Any) -> None:
    engine = FakeAudioEngine()
    page = RealtimeAudioAnalysisPage(engine=engine)  # type: ignore[arg-type]
    qtbot.addWidget(page)
    page.start_button.click()

    page.shutdown()
    page.shutdown()

    assert not engine.running
    assert not page.ui_timer.isActive()
    assert engine.stop_count == 2
