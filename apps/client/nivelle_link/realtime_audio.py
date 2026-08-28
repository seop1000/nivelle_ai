from __future__ import annotations

import math
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QByteArray, QIODevice, QObject, Signal
from PySide6.QtMultimedia import (
    QAudio,
    QAudioDevice,
    QAudioFormat,
    QAudioSource,
    QMediaDevices,
    QtAudio,
)

FloatArray = NDArray[np.float32]


def _qbyte_array_bytes(value: QByteArray) -> bytes:
    raw = value.data()
    return raw if isinstance(raw, bytes) else bytes(raw)


@dataclass(frozen=True)
class AudioInputDevice:
    identifier: bytes
    description: str
    is_default: bool = False


@dataclass(frozen=True)
class RealtimeAnalysis:
    sample_rate: int
    rms: float
    rms_dbfs: float
    peak: float
    peak_dbfs: float
    clipping: bool
    peak_frequency_hz: float | None
    fundamental_frequency_hz: float | None
    waveform_minimum: tuple[float, ...]
    waveform_maximum: tuple[float, ...]
    waveform_duration_seconds: float
    spectrum_frequencies_hz: tuple[float, ...]
    spectrum_dbfs: tuple[float, ...]
    spectrogram_frequencies_hz: tuple[float, ...]
    spectrogram_column_dbfs: tuple[float, ...]


class AudioRingBuffer:
    """Fixed-capacity mono float buffer used only by the analysis worker."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("오디오 링 버퍼 크기는 1 이상이어야 합니다.")
        self._values = np.zeros(capacity, dtype=np.float32)
        self._write_index = 0
        self._size = 0

    @property
    def capacity(self) -> int:
        return len(self._values)

    @property
    def size(self) -> int:
        return self._size

    def clear(self) -> None:
        self._values.fill(0.0)
        self._write_index = 0
        self._size = 0

    def append(self, samples: FloatArray) -> None:
        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        if values.size == 0:
            return
        if values.size >= self.capacity:
            self._values[:] = values[-self.capacity :]
            self._write_index = 0
            self._size = self.capacity
            return
        first = min(values.size, self.capacity - self._write_index)
        self._values[self._write_index : self._write_index + first] = values[:first]
        remaining = values.size - first
        if remaining:
            self._values[:remaining] = values[first:]
        self._write_index = (self._write_index + values.size) % self.capacity
        self._size = min(self._size + values.size, self.capacity)

    def latest(self, count: int) -> FloatArray:
        count = min(max(int(count), 0), self._size)
        if count == 0:
            return np.empty(0, dtype=np.float32)
        start = (self._write_index - count) % self.capacity
        if start < self._write_index:
            return self._values[start : self._write_index].copy()
        return np.concatenate((self._values[start:], self._values[: self._write_index])).astype(
            np.float32, copy=False
        )


class PcmStreamDecoder:
    """Turn arbitrary aligned or unaligned Qt PCM byte chunks into mono floats."""

    _DTYPES: ClassVar[dict[str, np.dtype[np.generic]]] = {
        "uint8": np.dtype("u1"),
        "int16": np.dtype("<i2"),
        "int32": np.dtype("<i4"),
        "float32": np.dtype("<f4"),
    }

    def __init__(self, sample_format: str, channels: int) -> None:
        if sample_format not in self._DTYPES:
            raise ValueError("지원하지 않는 오디오 샘플 형식입니다.")
        if channels < 1:
            raise ValueError("오디오 채널 수가 올바르지 않습니다.")
        self.sample_format = sample_format
        self.channels = channels
        self._dtype = self._DTYPES[sample_format]
        self._frame_bytes = self._dtype.itemsize * channels
        self._remainder = b""

    def decode(self, payload: bytes) -> FloatArray:
        data = self._remainder + payload
        usable = len(data) - (len(data) % self._frame_bytes)
        self._remainder = data[usable:]
        if usable == 0:
            return np.empty(0, dtype=np.float32)
        raw = np.frombuffer(data[:usable], dtype=self._dtype)
        if self.sample_format == "uint8":
            values = (raw.astype(np.float32) - 128.0) / 128.0
        elif self.sample_format == "int16":
            values = raw.astype(np.float32) / 32_768.0
        elif self.sample_format == "int32":
            values = raw.astype(np.float32) / 2_147_483_648.0
        else:
            values = raw.astype(np.float32)
        frames = values.reshape((-1, self.channels))
        mono = frames.mean(axis=1, dtype=np.float32)
        return np.nan_to_num(mono, nan=0.0, posinf=1.0, neginf=-1.0)


class RealtimeAudioAnalyzer:
    """Stateful, UI-independent real-time waveform/FFT/F0 analyzer."""

    FFT_SIZE = 4_096
    HOP_SIZE = 2_048
    BUFFER_SECONDS = 1.0
    WAVEFORM_SECONDS = 0.12
    WAVEFORM_POINTS = 512
    SPECTRUM_POINTS = 600
    SPECTROGRAM_BINS = 128
    MIN_FREQUENCY_HZ = 20.0
    MAX_FREQUENCY_HZ = 20_000.0
    MIN_F0_HZ = 50.0
    MAX_F0_HZ = 1_000.0
    SILENCE_DBFS = -60.0
    PEAK_PROMINENCE_DB = 15.0
    F0_CONFIDENCE = 0.72

    def __init__(self, sample_rate: int) -> None:
        if sample_rate < 1:
            raise ValueError("샘플레이트는 1 이상이어야 합니다.")
        self.sample_rate = int(sample_rate)
        capacity = max(round(self.sample_rate * self.BUFFER_SECONDS), self.FFT_SIZE)
        self.ring_buffer = AudioRingBuffer(capacity)
        self._samples_since_analysis = 0
        self._window = np.hanning(self.FFT_SIZE).astype(np.float32)
        self._window_sum = float(self._window.sum())
        self._smoothed_peak: float | None = None
        self._smoothed_f0: float | None = None

    def reset(self) -> None:
        self.ring_buffer.clear()
        self._samples_since_analysis = 0
        self._smoothed_peak = None
        self._smoothed_f0 = None

    def push(self, samples: FloatArray) -> RealtimeAnalysis | None:
        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        if values.size == 0:
            return None
        values = np.nan_to_num(values, nan=0.0, posinf=1.0, neginf=-1.0)
        self.ring_buffer.append(values)
        self._samples_since_analysis += values.size
        if self._samples_since_analysis < self.HOP_SIZE:
            return None
        self._samples_since_analysis %= self.HOP_SIZE
        return self._analyze_latest()

    @staticmethod
    def _dbfs(value: float) -> float:
        return max(20.0 * math.log10(max(value, 1e-6)), -120.0)

    @staticmethod
    def _parabolic_bin(values: NDArray[np.float64], index: int) -> float:
        if index <= 0 or index >= values.size - 1:
            return float(index)
        left = float(values[index - 1])
        center = float(values[index])
        right = float(values[index + 1])
        denominator = left - 2.0 * center + right
        if abs(denominator) < 1e-12:
            return float(index)
        return float(index) + 0.5 * (left - right) / denominator

    @staticmethod
    def _smooth(previous: float | None, current: float, alpha: float) -> float:
        if previous is None or current <= 0 or previous <= 0:
            return current
        if abs(math.log2(current / previous)) > 0.75:
            return current
        return previous + alpha * (current - previous)

    @staticmethod
    def _downsample_envelope(samples: FloatArray, points: int) -> tuple[FloatArray, FloatArray]:
        if samples.size == 0:
            empty = np.empty(0, dtype=np.float32)
            return empty, empty
        group_size = max(math.ceil(samples.size / max(points, 1)), 1)
        starts = np.arange(0, samples.size, group_size)
        minimum = np.minimum.reduceat(samples, starts)
        maximum = np.maximum.reduceat(samples, starts)
        return minimum.astype(np.float32), maximum.astype(np.float32)

    @staticmethod
    def _reduce_spectrum(
        frequencies: NDArray[np.float64], levels: NDArray[np.float64], points: int
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        if frequencies.size <= points:
            return frequencies, levels
        edges = np.linspace(0, frequencies.size, points + 1, dtype=np.int64)
        reduced_frequencies = np.empty(points, dtype=np.float64)
        reduced_levels = np.empty(points, dtype=np.float64)
        for index in range(points):
            start = int(edges[index])
            end = max(int(edges[index + 1]), start + 1)
            local = levels[start:end]
            peak = int(np.argmax(local))
            reduced_frequencies[index] = frequencies[start + peak]
            reduced_levels[index] = local[peak]
        return reduced_frequencies, reduced_levels

    def _estimate_f0(self, samples: FloatArray, rms_dbfs: float) -> float | None:
        if rms_dbfs < self.SILENCE_DBFS:
            self._smoothed_f0 = None
            return None
        factor = max(round(self.sample_rate / 8_000), 1)
        downsampled = samples[::factor].astype(np.float64)
        effective_rate = self.sample_rate / factor
        desired = max(round(effective_rate * 0.08), 1)
        downsampled = downsampled[-desired:]
        if downsampled.size < 32:
            return None
        downsampled -= downsampled.mean()
        energy = float(np.dot(downsampled, downsampled))
        if energy < 1e-10:
            self._smoothed_f0 = None
            return None
        minimum_lag = max(round(effective_rate / self.MAX_F0_HZ), 2)
        maximum_lag = min(round(effective_rate / self.MIN_F0_HZ), downsampled.size - 3)
        if maximum_lag <= minimum_lag:
            return None
        correlations = np.full(maximum_lag + 1, -1.0, dtype=np.float64)
        for lag in range(minimum_lag, maximum_lag + 1):
            left = downsampled[:-lag]
            right = downsampled[lag:]
            denominator = math.sqrt(float(np.dot(left, left) * np.dot(right, right)))
            if denominator > 1e-12:
                correlations[lag] = float(np.dot(left, right)) / denominator
        candidates = [
            lag
            for lag in range(minimum_lag + 1, maximum_lag)
            if correlations[lag] >= correlations[lag - 1]
            and correlations[lag] > correlations[lag + 1]
        ]
        if not candidates:
            return None
        best_score = max(float(correlations[lag]) for lag in candidates)
        threshold = max(self.F0_CONFIDENCE, best_score * 0.9)
        eligible = [lag for lag in candidates if correlations[lag] >= threshold]
        if not eligible or best_score < self.F0_CONFIDENCE:
            self._smoothed_f0 = None
            return None
        lag = eligible[0]
        refined_lag = self._parabolic_bin(correlations, lag)
        if refined_lag <= 0:
            return None
        current = effective_rate / refined_lag
        if not self.MIN_F0_HZ <= current <= self.MAX_F0_HZ:
            return None
        self._smoothed_f0 = self._smooth(self._smoothed_f0, current, 0.25)
        return self._smoothed_f0

    def _analyze_latest(self) -> RealtimeAnalysis:
        level_count = min(self.HOP_SIZE, self.ring_buffer.size)
        level_samples = self.ring_buffer.latest(level_count)
        rms = float(np.sqrt(np.mean(np.square(level_samples, dtype=np.float64))))
        peak = float(np.max(np.abs(level_samples))) if level_samples.size else 0.0
        rms_dbfs = self._dbfs(rms)
        peak_dbfs = self._dbfs(peak)

        fft_samples = self.ring_buffer.latest(self.FFT_SIZE)
        if fft_samples.size < self.FFT_SIZE:
            fft_samples = np.pad(
                fft_samples, (self.FFT_SIZE - fft_samples.size, 0), mode="constant"
            )
        windowed = fft_samples.astype(np.float64) * self._window
        magnitudes = np.abs(np.fft.rfft(windowed)) * (2.0 / max(self._window_sum, 1e-9))
        if magnitudes.size:
            magnitudes[0] *= 0.5
        spectrum_db = np.maximum(20.0 * np.log10(np.maximum(magnitudes, 1e-6)), -120.0)
        frequencies = np.fft.rfftfreq(self.FFT_SIZE, 1.0 / self.sample_rate)
        maximum_frequency = min(self.MAX_FREQUENCY_HZ, self.sample_rate / 2.0)
        visible = (frequencies >= self.MIN_FREQUENCY_HZ) & (frequencies <= maximum_frequency)
        visible_frequencies = frequencies[visible]
        visible_levels = spectrum_db[visible]

        peak_frequency: float | None = None
        if rms_dbfs >= self.SILENCE_DBFS and visible_levels.size:
            local_index = int(np.argmax(visible_levels))
            full_indices = np.flatnonzero(visible)
            full_index = int(full_indices[local_index])
            refined = self._parabolic_bin(spectrum_db, full_index)
            current_peak = refined * self.sample_rate / self.FFT_SIZE
            prominence = float(visible_levels[local_index]) - float(
                np.percentile(visible_levels, 75)
            )
            if (
                float(visible_levels[local_index]) >= -75.0
                and prominence >= self.PEAK_PROMINENCE_DB
            ):
                self._smoothed_peak = self._smooth(self._smoothed_peak, current_peak, 0.35)
                peak_frequency = self._smoothed_peak
        else:
            self._smoothed_peak = None

        waveform_samples = self.ring_buffer.latest(
            min(round(self.sample_rate * self.WAVEFORM_SECONDS), self.ring_buffer.size)
        )
        waveform_minimum, waveform_maximum = self._downsample_envelope(
            waveform_samples, self.WAVEFORM_POINTS
        )
        reduced_frequencies, reduced_levels = self._reduce_spectrum(
            visible_frequencies, visible_levels, self.SPECTRUM_POINTS
        )
        if maximum_frequency > self.MIN_FREQUENCY_HZ:
            spectrogram_frequencies = np.geomspace(
                self.MIN_FREQUENCY_HZ,
                maximum_frequency,
                self.SPECTROGRAM_BINS,
            )
            spectrogram_levels = np.interp(
                spectrogram_frequencies, visible_frequencies, visible_levels
            )
        else:
            spectrogram_frequencies = np.empty(0, dtype=np.float64)
            spectrogram_levels = np.empty(0, dtype=np.float64)
        f0 = self._estimate_f0(fft_samples, rms_dbfs)

        return RealtimeAnalysis(
            sample_rate=self.sample_rate,
            rms=round(rms, 6),
            rms_dbfs=round(rms_dbfs, 2),
            peak=round(peak, 6),
            peak_dbfs=round(peak_dbfs, 2),
            clipping=peak >= 0.98,
            peak_frequency_hz=round(peak_frequency, 2) if peak_frequency is not None else None,
            fundamental_frequency_hz=round(f0, 2) if f0 is not None else None,
            waveform_minimum=tuple(float(value) for value in waveform_minimum),
            waveform_maximum=tuple(float(value) for value in waveform_maximum),
            waveform_duration_seconds=waveform_samples.size / self.sample_rate,
            spectrum_frequencies_hz=tuple(float(value) for value in reduced_frequencies),
            spectrum_dbfs=tuple(float(value) for value in reduced_levels),
            spectrogram_frequencies_hz=tuple(float(value) for value in spectrogram_frequencies),
            spectrogram_column_dbfs=tuple(float(value) for value in spectrogram_levels),
        )


class RealtimeAnalysisWorker:
    """Bounded producer/consumer boundary between Qt capture and NumPy analysis."""

    QUEUE_CAPACITY = 8

    def __init__(
        self,
        *,
        sample_rate: int,
        sample_format: str,
        channels: int,
        result_callback: Callable[[RealtimeAnalysis], None],
        error_callback: Callable[[str], None],
    ) -> None:
        self.analyzer = RealtimeAudioAnalyzer(sample_rate)
        self.decoder = PcmStreamDecoder(sample_format, channels)
        self.result_callback = result_callback
        self.error_callback = error_callback
        self._queue: queue.Queue[bytes | None] = queue.Queue(self.QUEUE_CAPACITY)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def queued_chunks(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="nivelle-realtime-audio-analysis",
            daemon=True,
        )
        self._thread.start()

    def submit(self, payload: bytes) -> None:
        if not payload or not self.running:
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                pass

    def stop(self, timeout: float = 1.0) -> None:
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self.analyzer.reset()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                payload = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if payload is None:
                break
            try:
                result = self.analyzer.push(self.decoder.decode(payload))
                if result is not None and not self._stop_event.is_set():
                    self.result_callback(result)
            except (ArithmeticError, RuntimeError, TypeError, ValueError):
                self.error_callback("오디오 분석 중 오류가 발생했습니다.")


class RealtimeAudioEngine(QObject):
    analysis_ready = Signal(object)
    state_changed = Signal(str)
    error_occurred = Signal(str)
    devices_changed = Signal()

    _SAMPLE_FORMAT_NAMES: ClassVar[dict[QAudioFormat.SampleFormat, str]] = {
        QAudioFormat.SampleFormat.UInt8: "uint8",
        QAudioFormat.SampleFormat.Int16: "int16",
        QAudioFormat.SampleFormat.Int32: "int32",
        QAudioFormat.SampleFormat.Float: "float32",
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._media_devices = QMediaDevices(self)
        self._media_devices.audioInputsChanged.connect(self._inputs_changed)
        self._source: QAudioSource | None = None
        self._device_io: QIODevice | None = None
        self._worker: RealtimeAnalysisWorker | None = None
        self._current_device_id: bytes | None = None
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._source is not None and self._worker is not None and self._worker.running

    @property
    def current_device_id(self) -> bytes | None:
        return self._current_device_id

    def available_devices(self) -> list[AudioInputDevice]:
        default_id = _qbyte_array_bytes(QMediaDevices.defaultAudioInput().id())
        return [
            AudioInputDevice(
                identifier=_qbyte_array_bytes(device.id()),
                description=device.description() or "이름 없는 입력 장치",
                is_default=_qbyte_array_bytes(device.id()) == default_id,
            )
            for device in QMediaDevices.audioInputs()
        ]

    @classmethod
    def _select_format(cls, device: QAudioDevice) -> tuple[QAudioFormat, str]:
        preferred = device.preferredFormat()
        preferred_channels = max(preferred.channelCount(), 1)
        channel_counts = list(dict.fromkeys((1, preferred_channels)))
        for sample_format in (
            QAudioFormat.SampleFormat.Float,
            QAudioFormat.SampleFormat.Int16,
        ):
            for channels in channel_counts:
                candidate = QAudioFormat()
                candidate.setSampleRate(48_000)
                candidate.setChannelCount(channels)
                candidate.setSampleFormat(sample_format)
                if device.isFormatSupported(candidate):
                    return candidate, cls._SAMPLE_FORMAT_NAMES[sample_format]
        name = cls._SAMPLE_FORMAT_NAMES.get(preferred.sampleFormat())
        if name is None or preferred.sampleRate() < 1 or preferred.channelCount() < 1:
            raise ValueError("입력 장치의 오디오 형식을 지원하지 않습니다.")
        return preferred, name

    def start(self, device_id: bytes | None = None) -> bool:
        self.stop(emit_state=False)
        devices = QMediaDevices.audioInputs()
        if not devices:
            self.error_occurred.emit("사용 가능한 오디오 입력 장치가 없습니다.")
            self.state_changed.emit("입력 장치 없음")
            return False
        selected = next(
            (device for device in devices if _qbyte_array_bytes(device.id()) == device_id),
            QMediaDevices.defaultAudioInput(),
        )
        try:
            audio_format, sample_format = self._select_format(selected)
            worker = RealtimeAnalysisWorker(
                sample_rate=audio_format.sampleRate(),
                sample_format=sample_format,
                channels=audio_format.channelCount(),
                result_callback=self.analysis_ready.emit,
                error_callback=self.error_occurred.emit,
            )
            source = QAudioSource(selected, audio_format, self)
            source.setBufferSize(
                max(
                    audio_format.bytesForDuration(100_000),
                    audio_format.bytesPerFrame() * 1_024,
                )
            )
            source.stateChanged.connect(self._source_state_changed)
            worker.start()
            device_io = source.start()
            if device_io is None or source.error() != QtAudio.Error.NoError:
                source.stop()
                source.deleteLater()
                worker.stop()
                raise RuntimeError("오디오 입력 스트림을 시작하지 못했습니다.")
            device_io.readyRead.connect(self._read_available)
        except (OSError, RuntimeError, ValueError) as exc:
            self.error_occurred.emit(str(exc))
            self.state_changed.emit("시작 실패")
            return False
        self._source = source
        self._device_io = device_io
        self._worker = worker
        self._current_device_id = _qbyte_array_bytes(selected.id())
        self.state_changed.emit("분석 중")
        return True

    def stop(self, *, emit_state: bool = True) -> None:
        self._stopping = True
        device_io = self._device_io
        self._device_io = None
        if device_io is not None:
            try:
                device_io.readyRead.disconnect(self._read_available)
            except (RuntimeError, TypeError):
                pass
        source = self._source
        self._source = None
        if source is not None:
            source.stop()
            source.deleteLater()
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.stop()
        self._current_device_id = None
        self._stopping = False
        if emit_state:
            self.state_changed.emit("중지됨")

    def _read_available(self) -> None:
        if self._device_io is None or self._worker is None:
            return
        payload: QByteArray = self._device_io.readAll()
        self._worker.submit(_qbyte_array_bytes(payload))

    def _source_state_changed(self, state: QAudio.State) -> None:
        if self._stopping or state != QAudio.State.StoppedState:
            return
        source = self._source
        if source is None:
            return
        error = source.error()
        if error == QtAudio.Error.NoError:
            message = "오디오 입력이 중지되었습니다."
        else:
            messages = {
                QtAudio.Error.OpenError: "오디오 입력 장치를 열 수 없습니다.",
                QtAudio.Error.IOError: "오디오 입력 장치 연결이 끊어졌습니다.",
                QtAudio.Error.UnderrunError: "오디오 입력 데이터가 제시간에 도착하지 않았습니다.",
                QtAudio.Error.FatalError: "오디오 입력 장치에서 복구할 수 없는 오류가 발생했습니다.",
            }
            message = messages.get(error, "오디오 캡처 중 오류가 발생했습니다.")
        self.error_occurred.emit(message)
        self.stop(emit_state=False)
        self.state_changed.emit("오류")

    def _inputs_changed(self) -> None:
        active_id = self._current_device_id
        available_ids = {device.identifier for device in self.available_devices()}
        if active_id is not None and active_id not in available_ids:
            self.error_occurred.emit("사용 중인 입력 장치의 연결이 해제되었습니다.")
            self.stop(emit_state=False)
            self.state_changed.emit("장치 연결 해제")
        self.devices_changed.emit()


__all__ = [
    "AudioInputDevice",
    "AudioRingBuffer",
    "PcmStreamDecoder",
    "RealtimeAnalysis",
    "RealtimeAnalysisWorker",
    "RealtimeAudioAnalyzer",
    "RealtimeAudioEngine",
]
