from __future__ import annotations

import bisect
import math
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRect, QRectF, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .realtime_audio import (
    AudioInputDevice,
    RealtimeAnalysis,
    RealtimeAudioAnalyzer,
    RealtimeAudioEngine,
)

AUDIO_FILE_FILTER = (
    "오디오 파일 (*.wav *.wave *.mp3 *.m4a *.m4b *.aac *.flac *.ogg *.oga "
    "*.opus *.wma *.aif *.aiff *.aifc *.ac3 *.amr *.caf *.webm *.mp4);;모든 파일 (*)"
)


class AudioAnalysisWindow(QMainWindow):
    """Standalone Link window for Core-backed audio analysis."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Nivelle Link · 오디오 분석")
        self.resize(1180, 900)
        self.live_page = RealtimeAudioAnalysisPage()
        self.page = AudioAnalysisPage()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.live_page, "실시간 분석")
        self.tabs.addTab(self.page, "파일 분석")
        self.setCentralWidget(self.tabs)
        geometry = QSettings("Nivelle", "NivelleLink").value(
            "audio_analysis/geometry"
        )
        if geometry is not None:
            self.restoreGeometry(geometry)

    def set_online(self, online: bool) -> None:
        self.page.set_online(online)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.live_page.shutdown()
        QSettings("Nivelle", "NivelleLink").setValue(
            "audio_analysis/geometry", self.saveGeometry()
        )
        super().closeEvent(event)


def _format_time(seconds: float) -> str:
    total_ms = max(int(seconds * 1_000), 0)
    minutes, remainder = divmod(total_ms, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1_000)
    return f"{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


class WaveformWidget(QWidget):
    seek_requested = Signal(float)

    def __init__(self, *, overview: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._channels: list[dict[str, Any]] = []
        self._duration = 0.0
        self._position = 0.0
        self._zoom = 1.0
        self._selected_channel = 0
        self._overview = overview
        self.setMinimumHeight(90 if overview else 220)
        self.setMouseTracking(True)

    def set_waveform(self, value: object, duration: float) -> None:
        payload = value if isinstance(value, dict) else {}
        channels = payload.get("channels")
        self._channels = [item for item in channels if isinstance(item, dict)] if isinstance(channels, list) else []
        self._duration = max(float(duration), 0.0)
        self._position = 0.0
        self.update()

    def set_position(self, seconds: float) -> None:
        self._position = min(max(float(seconds), 0.0), self._duration)
        self.update()

    def set_zoom(self, zoom: float) -> None:
        self._zoom = max(float(zoom), 1.0)
        self.update()

    def set_channel(self, channel: int) -> None:
        self._selected_channel = max(channel, 0)
        self.update()

    def _visible_range(self) -> tuple[float, float]:
        if self._duration <= 0 or self._overview:
            return 0.0, self._duration
        span = self._duration / self._zoom
        start = min(max(self._position - span / 2, 0.0), max(self._duration - span, 0.0))
        return start, start + span

    def _seconds_at_x(self, x: float) -> float:
        start, end = self._visible_range()
        width = max(self.width() - 1, 1)
        return start + min(max(x / width, 0.0), 1.0) * max(end - start, 0.0)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() is Qt.MouseButton.LeftButton and self._duration > 0:
            self.seek_requested.emit(self._seconds_at_x(event.position().x()))
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111820"))
        if not self._channels or self._duration <= 0:
            painter.setPen(QColor("#9aa7b4"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "파형 데이터가 없습니다")
            return

        if self._selected_channel > 0 and self._selected_channel <= len(self._channels):
            visible_channels = [self._channels[self._selected_channel - 1]]
        else:
            visible_channels = self._channels
        start_seconds, end_seconds = self._visible_range()
        duration = max(self._duration, 1e-9)
        lane_height = self.height() / max(len(visible_channels), 1)
        colors = (QColor("#60d5ff"), QColor("#ff8ec7"), QColor("#a9e66e"), QColor("#ffd166"))
        for lane, channel in enumerate(visible_channels):
            minimum = channel.get("minimum")
            maximum = channel.get("maximum")
            if not isinstance(minimum, list) or not isinstance(maximum, list):
                continue
            point_count = min(len(minimum), len(maximum))
            if point_count < 1:
                continue
            first = max(int(start_seconds / duration * point_count), 0)
            last = min(max(math.ceil(end_seconds / duration * point_count), first + 1), point_count)
            center = lane_height * (lane + 0.5)
            amplitude = lane_height * 0.42
            painter.setPen(QPen(colors[lane % len(colors)], 1))
            pixel_count = max(self.width(), 1)
            visible_points = max(last - first, 1)
            for x in range(pixel_count):
                range_start = min(
                    first + int(x / pixel_count * visible_points), last - 1
                )
                range_end = min(
                    first + math.ceil((x + 1) / pixel_count * visible_points), last
                )
                range_end = max(range_end, range_start + 1)
                try:
                    low = min(float(value) for value in minimum[range_start:range_end])
                    high = max(float(value) for value in maximum[range_start:range_end])
                except (TypeError, ValueError):
                    continue
                painter.drawLine(QPointF(x, center - high * amplitude), QPointF(x, center - low * amplitude))
            painter.setPen(QColor("#cad4de"))
            painter.drawText(
                8,
                int(lane_height * lane + 18),
                str(channel.get("label") or f"채널 {lane + 1}"),
            )

        start, end = self._visible_range()
        if end > start and start <= self._position <= end:
            x = (self._position - start) / (end - start) * max(self.width() - 1, 1)
            painter.setPen(QPen(QColor("#ffcf5c"), 2))
            painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        painter.setPen(QColor("#9aa7b4"))
        painter.drawText(8, self.height() - 8, f"{_format_time(start)} — {_format_time(end)}")


class SpectrogramWidget(QWidget):
    seek_requested = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._times: list[float] = []
        self._frequencies: list[float] = []
        self._matrix: list[list[float]] = []
        self._duration = 0.0
        self._position = 0.0
        self._frequency_mode = "log"
        self._image = QImage()
        self.setMinimumHeight(260)

    @staticmethod
    def _color(decibels: float) -> QColor:
        normalized = min(max((decibels + 100.0) / 100.0, 0.0), 1.0)
        if normalized < 0.33:
            ratio = normalized / 0.33
            return QColor(int(12 + 20 * ratio), int(18 + 65 * ratio), int(45 + 105 * ratio))
        if normalized < 0.66:
            ratio = (normalized - 0.33) / 0.33
            return QColor(int(32 + 210 * ratio), int(83 + 55 * ratio), int(150 - 105 * ratio))
        ratio = (normalized - 0.66) / 0.34
        return QColor(242, int(138 + 95 * ratio), int(45 + 200 * ratio))

    def set_data(self, value: object, duration: float) -> None:
        payload = value if isinstance(value, dict) else {}
        raw_times = payload.get("times_seconds")
        raw_frequencies = payload.get("frequencies_hz")
        raw_matrix = payload.get("power_db")
        self._times = [float(item) for item in raw_times] if isinstance(raw_times, list) else []
        self._frequencies = [float(item) for item in raw_frequencies] if isinstance(raw_frequencies, list) else []
        self._matrix = [row for row in raw_matrix if isinstance(row, list)] if isinstance(raw_matrix, list) else []
        self._duration = max(float(duration), 0.0)
        self._position = 0.0
        self._rebuild_image()

    def set_frequency_mode(self, mode: str) -> None:
        self._frequency_mode = "linear" if mode == "linear" else "log"
        self._rebuild_image()

    def set_position(self, seconds: float) -> None:
        self._position = min(max(float(seconds), 0.0), self._duration)
        self.update()

    def _source_bin(self, output_row: int, height: int) -> int:
        if not self._frequencies:
            return 0
        maximum = max(self._frequencies[-1], 1.0)
        ratio = (height - 1 - output_row) / max(height - 1, 1)
        if self._frequency_mode == "linear":
            target = maximum * ratio
        else:
            minimum = next((value for value in self._frequencies if value > 0), 20.0)
            target = math.exp(math.log(minimum) + ratio * (math.log(maximum) - math.log(minimum)))
        index = bisect.bisect_left(self._frequencies, target)
        return min(max(index, 0), len(self._frequencies) - 1)

    def _rebuild_image(self) -> None:
        width = len(self._matrix)
        height = len(self._frequencies)
        if width < 1 or height < 1:
            self._image = QImage()
            self.update()
            return
        image = QImage(width, height, QImage.Format.Format_RGB32)
        for x, column in enumerate(self._matrix):
            for y in range(height):
                source = self._source_bin(y, height)
                try:
                    decibels = float(column[source])
                except (IndexError, TypeError, ValueError):
                    decibels = -120.0
                image.setPixelColor(x, y, self._color(decibels))
        self._image = image
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() is Qt.MouseButton.LeftButton and self._duration > 0:
            seconds = event.position().x() / max(self.width() - 1, 1) * self._duration
            self.seek_requested.emit(seconds)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#10151d"))
        if self._image.isNull():
            painter.setPen(QColor("#9aa7b4"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "스펙트로그램 데이터가 없습니다")
            return
        painter.drawImage(QRectF(self.rect()), self._image)
        if self._duration > 0:
            x = self._position / self._duration * max(self.width() - 1, 1)
            painter.setPen(QPen(QColor("#ffffff"), 1))
            painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        painter.setPen(QColor("#ffffff"))
        scale = "선형 주파수" if self._frequency_mode == "linear" else "로그 주파수"
        maximum = self._frequencies[-1] if self._frequencies else 0.0
        painter.drawText(8, 18, f"{scale} · 0–{maximum:,.0f} Hz · dBFS")


class RealtimeSpectrumWidget(QWidget):
    """Log-frequency spectrum renderer fed at the UI refresh rate."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frequencies: tuple[float, ...] = ()
        self._levels: tuple[float, ...] = ()
        self.setMinimumHeight(210)

    def set_spectrum(
        self, frequencies: tuple[float, ...], levels: tuple[float, ...]
    ) -> None:
        self._frequencies = frequencies
        self._levels = levels
        self.update()

    def clear(self) -> None:
        self._frequencies = ()
        self._levels = ()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111820"))
        plot = self.rect().adjusted(58, 12, -14, -28)
        painter.setPen(QPen(QColor("#34424f"), 1))
        for decibels in (-100, -80, -60, -40, -20, 0):
            y = plot.bottom() - (decibels + 100.0) / 100.0 * plot.height()
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor("#9aa7b4"))
            painter.drawText(4, round(y + 4), f"{decibels} dB")
            painter.setPen(QPen(QColor("#34424f"), 1))
        for frequency in (20, 100, 1_000, 10_000, 20_000):
            ratio = math.log10(frequency / 20.0) / math.log10(20_000.0 / 20.0)
            x = plot.left() + ratio * plot.width()
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(QColor("#9aa7b4"))
            label = f"{frequency // 1_000}k" if frequency >= 1_000 else str(frequency)
            painter.drawText(round(x - 10), self.height() - 8, label)
            painter.setPen(QPen(QColor("#34424f"), 1))
        if not self._frequencies or not self._levels:
            painter.setPen(QColor("#9aa7b4"))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "주파수 데이터가 없습니다")
            return
        points: list[QPointF] = []
        for frequency, decibels in zip(
            self._frequencies, self._levels, strict=False
        ):
            if frequency < 20.0 or frequency > 20_000.0:
                continue
            x_ratio = math.log10(frequency / 20.0) / math.log10(1_000.0)
            y_ratio = min(max((decibels + 100.0) / 100.0, 0.0), 1.0)
            points.append(
                QPointF(
                    plot.left() + x_ratio * plot.width(),
                    plot.bottom() - y_ratio * plot.height(),
                )
            )
        if points:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor("#60d5ff"), 1.5))
            painter.drawPolyline(QPolygonF(points))
        painter.setPen(QColor("#cad4de"))
        painter.drawText(plot.left() + 4, plot.top() + 16, "주파수 (Hz, 로그) / 레벨 (dBFS)")


class RealtimeSpectrogramWidget(QWidget):
    """Bounded rolling image; only one new STFT column is painted per update."""

    MAX_COLUMNS = 300

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._frequencies: tuple[float, ...] = ()
        self._column_count = 0
        self._seconds_per_column = 0.0
        self.setMinimumHeight(260)

    @property
    def column_count(self) -> int:
        return self._column_count

    def clear(self) -> None:
        self._image = QImage()
        self._frequencies = ()
        self._column_count = 0
        self._seconds_per_column = 0.0
        self.update()

    def append_column(
        self,
        frequencies: tuple[float, ...],
        levels: tuple[float, ...],
        *,
        sample_rate: int,
    ) -> None:
        bins = min(len(frequencies), len(levels))
        if bins < 1:
            return
        if self._image.isNull() or self._image.height() != bins:
            self._image = QImage(
                self.MAX_COLUMNS, bins, QImage.Format.Format_RGB32
            )
            self._image.fill(QColor("#0c122d"))
            self._column_count = 0
        if self._column_count >= self.MAX_COLUMNS:
            shifted = QImage(
                self.MAX_COLUMNS, bins, QImage.Format.Format_RGB32
            )
            shifted.fill(QColor("#0c122d"))
            image_painter = QPainter(shifted)
            image_painter.drawImage(
                QRect(0, 0, self.MAX_COLUMNS - 1, bins),
                self._image,
                QRect(1, 0, self.MAX_COLUMNS - 1, bins),
            )
            image_painter.end()
            self._image = shifted
            x = self.MAX_COLUMNS - 1
        else:
            x = self._column_count
            self._column_count += 1
        for index, decibels in enumerate(levels[:bins]):
            self._image.setPixelColor(
                x, bins - index - 1, SpectrogramWidget._color(float(decibels))
            )
        self._frequencies = frequencies[:bins]
        self._seconds_per_column = (
            RealtimeAudioAnalyzer.HOP_SIZE / sample_rate if sample_rate > 0 else 0.0
        )
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111820"))
        plot = self.rect().adjusted(58, 12, -14, -28)
        if self._image.isNull() or self._column_count < 1:
            painter.setPen(QColor("#9aa7b4"))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "스펙트로그램 데이터가 없습니다")
            return
        visible_width = max(
            round(plot.width() * self._column_count / self.MAX_COLUMNS), 1
        )
        destination = QRectF(
            plot.right() - visible_width + 1,
            plot.top(),
            visible_width,
            plot.height(),
        )
        source = QRectF(0, 0, self._column_count, self._image.height())
        painter.drawImage(destination, self._image, source)
        painter.setPen(QColor("#cad4de"))
        maximum = self._frequencies[-1] if self._frequencies else 0.0
        painter.drawText(plot.left() + 4, plot.top() + 16, f"{maximum:,.0f} Hz")
        painter.drawText(plot.left() + 4, plot.bottom() - 4, "20 Hz")
        elapsed = self._column_count * self._seconds_per_column
        painter.drawText(
            plot.left(), self.height() - 8, f"최근 {elapsed:.1f}초 · 시간 →"
        )


class RealtimeAudioAnalysisPage(QWidget):
    """Local microphone analysis UI; server connectivity is not required."""

    UI_INTERVAL_MS = 40

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        engine: RealtimeAudioEngine | None = None,
    ) -> None:
        super().__init__(parent)
        self.engine = engine or RealtimeAudioEngine(self)
        self._pending_result: RealtimeAnalysis | None = None
        self._known_devices: dict[bytes, AudioInputDevice] = {}

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)

        title = QLabel("실시간 오디오 분석")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(title)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("입력 장치"))
        self.device_select = QComboBox()
        self.device_select.setMinimumWidth(320)
        self.device_select.currentIndexChanged.connect(self._device_changed)
        controls.addWidget(self.device_select, 1)
        self.refresh_button = QPushButton("장치 새로고침")
        self.refresh_button.clicked.connect(self.refresh_devices)
        controls.addWidget(self.refresh_button)
        self.start_button = QPushButton("분석 시작")
        self.start_button.clicked.connect(self.start_analysis)
        controls.addWidget(self.start_button)
        self.stop_button = QPushButton("분석 중지")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_analysis)
        controls.addWidget(self.stop_button)
        layout.addLayout(controls)

        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("상태"))
        self.status = QLabel("● 중지됨")
        self.status.setStyleSheet("color: #777;")
        self.status.setWordWrap(True)
        status_row.addWidget(self.status, 1)
        layout.addLayout(status_row)

        metrics = QFrame()
        metric_layout = QGridLayout(metrics)
        self.peak_frequency = self._metric(metric_layout, 0, 0, "주요 주파수")
        self.fundamental_frequency = self._metric(metric_layout, 0, 1, "기본 주파수")
        self.current_volume = self._metric(metric_layout, 0, 2, "현재 음량")
        self.peak_level = self._metric(metric_layout, 1, 0, "최대 레벨")
        self.sample_rate = self._metric(metric_layout, 1, 1, "샘플레이트")
        self.clipping_status = self._metric(metric_layout, 1, 2, "입력 상태")
        layout.addWidget(metrics)

        layout.addWidget(QLabel("실시간 파형"))
        self.waveform = WaveformWidget()
        self.waveform.setMinimumHeight(170)
        layout.addWidget(self.waveform)

        layout.addWidget(QLabel("주파수 스펙트럼"))
        self.spectrum = RealtimeSpectrumWidget()
        layout.addWidget(self.spectrum)

        layout.addWidget(QLabel("스펙트로그램"))
        self.spectrogram = RealtimeSpectrogramWidget()
        layout.addWidget(self.spectrogram)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(self.UI_INTERVAL_MS)
        self.ui_timer.timeout.connect(self._render_pending)
        self.engine.analysis_ready.connect(self._queue_result)
        self.engine.state_changed.connect(self._state_changed)
        self.engine.error_occurred.connect(self._show_error)
        self.engine.devices_changed.connect(self.refresh_devices)
        application = QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.shutdown)
        self.refresh_devices()

    @staticmethod
    def _metric(layout: QGridLayout, row: int, column: int, title: str) -> QLabel:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame_layout = QVBoxLayout(frame)
        heading = QLabel(title)
        heading.setStyleSheet("color: #8fa1b3;")
        value = QLabel("감지되지 않음")
        value.setStyleSheet("font-size: 18px; font-weight: 650;")
        value.setWordWrap(True)
        frame_layout.addWidget(heading)
        frame_layout.addWidget(value)
        layout.addWidget(frame, row, column)
        return value

    @staticmethod
    def _device_id(value: object) -> bytes | None:
        if isinstance(value, bytes):
            return value
        try:
            return bytes(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def refresh_devices(self) -> None:
        selected = self._device_id(self.device_select.currentData())
        devices = self.engine.available_devices()
        self._known_devices = {device.identifier: device for device in devices}
        self.device_select.blockSignals(True)
        self.device_select.clear()
        selected_index = -1
        default_index = -1
        for index, device in enumerate(devices):
            suffix = " (기본)" if device.is_default else ""
            self.device_select.addItem(f"{device.description}{suffix}", device.identifier)
            if device.identifier == selected:
                selected_index = index
            if device.is_default:
                default_index = index
        if devices:
            self.device_select.setCurrentIndex(
                selected_index if selected_index >= 0 else max(default_index, 0)
            )
        else:
            self.device_select.addItem("사용 가능한 입력 장치 없음", None)
        self.device_select.blockSignals(False)
        self.start_button.setEnabled(bool(devices) and not self.engine.running)
        if not devices and not self.engine.running:
            self.status.setText("● 입력 장치 없음")
            self.status.setStyleSheet("color: #b00020;")

    def start_analysis(self) -> None:
        device_id = self._device_id(self.device_select.currentData())
        if device_id is None:
            self._show_error("사용 가능한 오디오 입력 장치가 없습니다.")
            return
        self._pending_result = None
        self.spectrogram.clear()
        self.spectrum.clear()
        if self.engine.start(device_id):
            self.ui_timer.start()

    def stop_analysis(self) -> None:
        self.ui_timer.stop()
        self._pending_result = None
        self.engine.stop()

    def shutdown(self) -> None:
        self.ui_timer.stop()
        self._pending_result = None
        self.engine.stop(emit_state=False)

    def _device_changed(self, _index: int) -> None:
        if self.engine.running:
            self.start_analysis()

    def _queue_result(self, value: object) -> None:
        if isinstance(value, RealtimeAnalysis):
            self._pending_result = value

    @staticmethod
    def _frequency_text(value: float | None) -> str:
        return f"{value:,.1f} Hz" if value is not None else "감지되지 않음"

    def _render_pending(self) -> None:
        result = self._pending_result
        self._pending_result = None
        if result is None:
            return
        self.peak_frequency.setText(self._frequency_text(result.peak_frequency_hz))
        self.fundamental_frequency.setText(
            self._frequency_text(result.fundamental_frequency_hz)
        )
        self.current_volume.setText(f"{result.rms_dbfs:,.1f} dBFS\nRMS {result.rms:.4f}")
        self.peak_level.setText(f"{result.peak_dbfs:,.1f} dBFS")
        self.sample_rate.setText(f"{result.sample_rate:,} Hz")
        if result.clipping:
            self.clipping_status.setText("클리핑 감지")
            self.clipping_status.setStyleSheet(
                "font-size: 18px; font-weight: 650; color: #d32f2f;"
            )
        elif result.peak_dbfs >= -3.0:
            self.clipping_status.setText("클리핑 주의")
            self.clipping_status.setStyleSheet(
                "font-size: 18px; font-weight: 650; color: #d98200;"
            )
        else:
            self.clipping_status.setText("정상")
            self.clipping_status.setStyleSheet(
                "font-size: 18px; font-weight: 650; color: #176b2c;"
            )
        self.waveform.set_waveform(
            {
                "channels": [
                    {
                        "channel": 1,
                        "label": "모노 입력",
                        "minimum": list(result.waveform_minimum),
                        "maximum": list(result.waveform_maximum),
                    }
                ]
            },
            result.waveform_duration_seconds,
        )
        self.spectrum.set_spectrum(
            result.spectrum_frequencies_hz, result.spectrum_dbfs
        )
        self.spectrogram.append_column(
            result.spectrogram_frequencies_hz,
            result.spectrogram_column_dbfs,
            sample_rate=result.sample_rate,
        )

    def _state_changed(self, state: str) -> None:
        running = state == "분석 중"
        self.status.setText(f"● {state}")
        self.status.setStyleSheet("color: #176b2c;" if running else "color: #777;")
        self.start_button.setEnabled(not running and bool(self._known_devices))
        self.stop_button.setEnabled(running)
        self.device_select.setEnabled(True)
        if not running:
            self.ui_timer.stop()

    def _show_error(self, message: str) -> None:
        self.status.setText(f"● {message}")
        self.status.setStyleSheet("color: #b00020;")


class AudioAnalysisPage(QWidget):
    file_selected = Signal(str)
    cancellation_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._online = True
        self._local_path: Path | None = None
        self._duration = 0.0
        self._timeline: list[dict[str, Any]] = []
        self._timeline_times: list[float] = []

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._playback_state_changed)

        outer_layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        controls = QHBoxLayout()
        self.select_button = QPushButton("오디오 파일 선택")
        self.select_button.clicked.connect(self._select_file)
        self.cancel_button = QPushButton("분석 취소")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancellation_requested.emit)
        self.file_label = QLabel(
            "M4A, MP3, WAV 등 일반 오디오 파일을 선택하거나 놓으세요."
        )
        self.file_label.setWordWrap(True)
        controls.addWidget(self.select_button)
        controls.addWidget(self.cancel_button)
        controls.addWidget(self.file_label, 1)
        layout.addLayout(controls)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1_000)
        self.progress.setValue(0)
        self.status = QLabel("분석 대기 중")
        self.status.setWordWrap(True)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)

        playback = QHBoxLayout()
        self.play_button = QPushButton("재생")
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self._toggle_playback)
        self.stop_button = QPushButton("정지")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.player.stop)
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderMoved.connect(self._seek_milliseconds)
        self.position_label = QLabel("00:00.000 / 00:00.000")
        playback.addWidget(self.play_button)
        playback.addWidget(self.stop_button)
        playback.addWidget(self.position_slider, 1)
        playback.addWidget(self.position_label)
        layout.addLayout(playback)

        waveform_controls = QHBoxLayout()
        waveform_controls.addWidget(QLabel("파형 확대"))
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(1, 20)
        self.zoom_slider.setValue(1)
        self.zoom_slider.valueChanged.connect(self._set_zoom)
        waveform_controls.addWidget(self.zoom_slider, 1)
        waveform_controls.addWidget(QLabel("채널"))
        self.channel_select = QComboBox()
        self.channel_select.addItem("전체", 0)
        self.channel_select.currentIndexChanged.connect(self._set_channel)
        waveform_controls.addWidget(self.channel_select)
        layout.addLayout(waveform_controls)

        self.waveform = WaveformWidget()
        self.overview = WaveformWidget(overview=True)
        self.waveform.seek_requested.connect(self._seek_seconds)
        self.overview.seek_requested.connect(self._seek_seconds)
        layout.addWidget(self.waveform)
        layout.addWidget(self.overview)

        spectrum_controls = QHBoxLayout()
        spectrum_controls.addWidget(QLabel("스펙트로그램 주파수 축"))
        self.frequency_scale = QComboBox()
        self.frequency_scale.addItem("로그", "log")
        self.frequency_scale.addItem("선형", "linear")
        self.frequency_scale.currentIndexChanged.connect(self._set_frequency_scale)
        spectrum_controls.addWidget(self.frequency_scale)
        spectrum_controls.addStretch()
        layout.addLayout(spectrum_controls)
        self.spectrogram = SpectrogramWidget()
        self.spectrogram.seek_requested.connect(self._seek_seconds)
        layout.addWidget(self.spectrogram)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        metrics_frame = QFrame()
        metrics_layout = QVBoxLayout(metrics_frame)
        metrics_layout.addWidget(QLabel("오디오 지표"))
        self.metrics = QTextBrowser()
        self.metrics.setPlainText("분석 결과가 아직 없습니다.")
        metrics_layout.addWidget(self.metrics)
        live_frame = QFrame()
        live_layout = QFormLayout(live_frame)
        self.live_rms = QLabel("-")
        self.live_peak = QLabel("-")
        self.live_frequency = QLabel("-")
        self.live_frequency.setWordWrap(True)
        live_layout.addRow("현재 RMS", self.live_rms)
        live_layout.addRow("현재 최대값", self.live_peak)
        live_layout.addRow("주파수 활동도", self.live_frequency)
        splitter.addWidget(metrics_frame)
        splitter.addWidget(live_frame)
        splitter.setSizes([650, 300])
        layout.addWidget(splitter)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)

    def _select_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "오디오 파일 선택",
            str(self._local_path.parent if self._local_path is not None else Path.home()),
            AUDIO_FILE_FILTER,
        )
        if path:
            self.begin_file(Path(path))

    def begin_file(self, path: Path) -> None:
        self.player.stop()
        self._local_path = path
        self.file_label.setText(str(path))
        self.clear_result()
        self.status.setStyleSheet("color: #555;")
        self.status.setText("Core로 업로드하는 중…")
        self.cancel_button.setEnabled(True)
        self.file_selected.emit(str(path))

    def clear_result(self) -> None:
        self._duration = 0.0
        self._timeline = []
        self._timeline_times = []
        self.waveform.set_waveform({}, 0)
        self.overview.set_waveform({}, 0)
        self.spectrogram.set_data({}, 0)
        self.metrics.setPlainText("분석 중…")
        self.progress.setValue(0)
        self.position_slider.setRange(0, 0)
        self.play_button.setEnabled(False)
        self.stop_button.setEnabled(False)

    def set_job_progress(self, status: str, progress: float, stage: str) -> None:
        self.progress.setValue(round(min(max(progress, 0.0), 1.0) * 1_000))
        stage_labels = {
            "metadata": "정보 확인",
            "decoded": "오디오 변환",
            "waveform": "파형 계산",
            "spectrogram": "스펙트로그램 계산",
            "complete": "마무리",
        }
        labels = {
            "queued": "분석 대기 중",
            "running": f"분석 중 · {stage_labels.get(stage, stage)}",
            "cancelling": "취소 요청 중…",
            "cancelled": "분석이 취소되었습니다.",
            "completed": "분석 완료",
            "failed": "분석 실패",
        }
        self.status.setText(labels.get(status, status))
        self.cancel_button.setEnabled(status in {"queued", "running", "cancelling"})

    def set_upload_progress(self, sent: int, total: int) -> None:
        ratio = sent / max(total, 1)
        self.progress.setValue(round(ratio * 100))
        self.status.setText(f"Core로 업로드하는 중… {ratio * 100:.0f}%")

    def set_error(self, message: str) -> None:
        self.cancel_button.setEnabled(False)
        self.status.setStyleSheet("color: #b00020;")
        self.status.setText(message)
        self.metrics.setPlainText("분석 결과를 표시할 수 없습니다.")

    def set_analysis_result(self, value: object, *, cache_hit: bool = False) -> None:
        result = value if isinstance(value, dict) else {}
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        try:
            self._duration = max(float(metadata.get("duration_seconds", 0.0)), 0.0)
        except (TypeError, ValueError):
            self._duration = 0.0
        waveform = result.get("waveform")
        self.waveform.set_waveform(waveform, self._duration)
        overview_waveform = waveform
        if isinstance(waveform, dict) and isinstance(
            waveform.get("overview_channels"), list
        ):
            overview_waveform = {"channels": waveform["overview_channels"]}
        self.overview.set_waveform(overview_waveform, self._duration)
        self.spectrogram.set_data(result.get("spectrogram"), self._duration)
        raw_timeline = result.get("timeline")
        self._timeline = [item for item in raw_timeline if isinstance(item, dict)] if isinstance(raw_timeline, list) else []
        self._timeline_times = [float(item.get("time_seconds", 0.0)) for item in self._timeline]
        self.position_slider.setRange(0, max(round(self._duration * 1_000), 0))
        self.channel_select.blockSignals(True)
        self.channel_select.clear()
        self.channel_select.addItem("전체", 0)
        raw_channels = result.get("waveform", {}).get("channels", []) if isinstance(result.get("waveform"), dict) else []
        if isinstance(raw_channels, list):
            for index, channel in enumerate(raw_channels, 1):
                if isinstance(channel, dict):
                    self.channel_select.addItem(
                        str(channel.get("label") or f"채널 {index}"), index
                    )
        self.channel_select.blockSignals(False)
        self._set_channel()
        self.metrics.setPlainText(self._metrics_text(metadata, metrics))
        self.progress.setValue(1_000)
        self.cancel_button.setEnabled(False)
        self.status.setStyleSheet("color: #176b2c;")
        suffix = " · 캐시 사용" if cache_hit else ""
        self.status.setText(f"분석 완료{suffix}")
        if self._local_path is not None:
            self.player.setSource(QUrl.fromLocalFile(str(self._local_path)))
            self.play_button.setEnabled(True)
            self.stop_button.setEnabled(True)
        self._position_changed(0)

    @staticmethod
    def _metrics_text(metadata: dict[str, Any], metrics: dict[str, Any]) -> str:
        frequency_energy = metrics.get("frequency_energy") if isinstance(metrics.get("frequency_energy"), dict) else {}
        dominant = metrics.get("dominant_frequency_ranges")
        dominant_lines = []
        if isinstance(dominant, list):
            for item in dominant:
                if isinstance(item, dict):
                    range_name = str(item.get("range") or "")
                    for english, korean in (
                        ("low", "저역"),
                        ("mid", "중역"),
                        ("high", "고역"),
                    ):
                        if range_name.startswith(english):
                            range_name = range_name.replace(english, korean, 1)
                            break
                    dominant_lines.append(
                        f"  - {range_name}: {float(item.get('energy_ratio', 0)) * 100:.1f}%"
                    )
        lines = [
            f"파일: {metadata.get('filename', '-')}",
            f"형식/코덱: {metadata.get('format', '-')} / {metadata.get('codec', '-')}",
            f"길이: {float(metadata.get('duration_seconds', 0)):.3f}초",
            f"샘플레이트: {metadata.get('sample_rate_hz', '-')} Hz",
            f"채널: {metadata.get('channels', '-')}",
            f"비트 깊이: {metadata.get('bit_depth') or metadata.get('source_bit_depth') or '-'}",
            f"최대 진폭: {float(metrics.get('peak_amplitude', 0)):.6f}",
            f"RMS: {float(metrics.get('rms', 0)):.6f} ({float(metrics.get('rms_dbfs', -120)):.2f} dBFS)",
            f"클리핑: {'감지됨' if metrics.get('clipping_detected') else '감지되지 않음'} ({metrics.get('clipped_samples', 0)}개 샘플)",
            f"무음 비율 (≤ -60 dBFS): {float(metrics.get('silence_ratio', 0)) * 100:.2f}%",
            f"스펙트럼 중심: {float(metrics.get('spectral_centroid_hz', 0)):.1f} Hz",
            f"스펙트럼 평탄도: {float(metrics.get('spectral_flatness', 0)):.3f}",
            f"주기성 경향: {float(metrics.get('periodic_tendency', 0)):.3f}",
            f"순간 변화량: {float(metrics.get('transient_activity', 0)):.3f}",
            f"음조성 / 소음성: {float(metrics.get('tonal_tendency', 0)):.3f} / {float(metrics.get('noise_like_tendency', 0)):.3f}",
            f"저역 / 중역 / 고역 에너지: {float(frequency_energy.get('low', 0)) * 100:.1f}% / {float(frequency_energy.get('mid', 0)) * 100:.1f}% / {float(frequency_energy.get('high', 0)) * 100:.1f}%",
            "주요 주파수 대역:",
            *(dominant_lines or ["  - 정보 없음"]),
            "음성/음악/환경 분류: 구현되지 않음",
        ]
        return "\n".join(lines)

    def set_online(self, online: bool) -> None:
        self._online = online
        self.select_button.setEnabled(online)
        if not online:
            self.cancel_button.setEnabled(False)

    def _toggle_playback(self) -> None:
        if self.player.playbackState() is QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.play_button.setText("일시정지" if state is QMediaPlayer.PlaybackState.PlayingState else "재생")

    def _duration_changed(self, milliseconds: int) -> None:
        if milliseconds > 0:
            self.position_slider.setMaximum(milliseconds)

    def _seek_milliseconds(self, milliseconds: int) -> None:
        self.player.setPosition(milliseconds)
        self._position_changed(milliseconds)

    def _seek_seconds(self, seconds: float) -> None:
        self._seek_milliseconds(round(seconds * 1_000))

    def _position_changed(self, milliseconds: int) -> None:
        seconds = milliseconds / 1_000.0
        self.position_slider.blockSignals(True)
        self.position_slider.setValue(milliseconds)
        self.position_slider.blockSignals(False)
        self.position_label.setText(f"{_format_time(seconds)} / {_format_time(self._duration)}")
        self.waveform.set_position(seconds)
        self.overview.set_position(seconds)
        self.spectrogram.set_position(seconds)
        if not self._timeline:
            self.live_rms.setText("-")
            self.live_peak.setText("-")
            self.live_frequency.setText("-")
            return
        index = min(bisect.bisect_left(self._timeline_times, seconds), len(self._timeline) - 1)
        item = self._timeline[index]
        self.live_rms.setText(f"{float(item.get('rms', 0)):.6f}")
        self.live_peak.setText(f"{float(item.get('peak', 0)):.6f}")
        self.live_frequency.setText(
            f"저역 {float(item.get('low', 0)) * 100:.1f}% · "
            f"중역 {float(item.get('mid', 0)) * 100:.1f}% · "
            f"고역 {float(item.get('high', 0)) * 100:.1f}%"
        )

    def _set_zoom(self, value: int) -> None:
        self.waveform.set_zoom(float(value))

    def _set_channel(self, _index: int | None = None) -> None:
        data = self.channel_select.currentData()
        self.waveform.set_channel(int(data) if isinstance(data, int) else 0)

    def _set_frequency_scale(self, _index: int | None = None) -> None:
        mode = self.frequency_scale.currentData()
        self.spectrogram.set_frequency_mode(str(mode or "log"))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            self.begin_file(Path(urls[0].toLocalFile()))
            event.acceptProposedAction()


__all__ = [
    "AudioAnalysisPage",
    "AudioAnalysisWindow",
    "RealtimeAudioAnalysisPage",
    "RealtimeSpectrogramWidget",
    "RealtimeSpectrumWidget",
    "SpectrogramWidget",
    "WaveformWidget",
]
