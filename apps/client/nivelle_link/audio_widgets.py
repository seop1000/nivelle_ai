from __future__ import annotations

import bisect
import math
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, QUrl, Signal
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
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

AUDIO_FILE_FILTER = (
    "Audio files (*.wav *.wave *.mp3 *.m4a *.m4b *.aac *.flac *.ogg *.oga "
    "*.opus *.wma *.aif *.aiff *.aifc *.ac3 *.amr *.caf *.webm *.mp4);;All files (*)"
)


class AudioAnalysisWindow(QMainWindow):
    """Standalone Link window for Core-backed audio analysis."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Nivelle Link · 오디오 분석")
        self.resize(1120, 820)
        self.page = AudioAnalysisPage()
        self.setCentralWidget(self.page)
        geometry = QSettings("Nivelle", "NivelleLink").value(
            "audio_analysis/geometry"
        )
        if geometry is not None:
            self.restoreGeometry(geometry)

    def set_online(self, online: bool) -> None:
        self.page.set_online(online)

    def closeEvent(self, event: QCloseEvent) -> None:
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
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Waveform data is not loaded")
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
            painter.drawText(8, int(lane_height * lane + 18), str(channel.get("label") or f"CH {lane + 1}"))

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
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Spectrogram data is not loaded")
            return
        painter.drawImage(QRectF(self.rect()), self._image)
        if self._duration > 0:
            x = self._position / self._duration * max(self.width() - 1, 1)
            painter.setPen(QPen(QColor("#ffffff"), 1))
            painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        painter.setPen(QColor("#ffffff"))
        scale = "Linear frequency" if self._frequency_mode == "linear" else "Log frequency"
        maximum = self._frequencies[-1] if self._frequencies else 0.0
        painter.drawText(8, 18, f"{scale} · 0–{maximum:,.0f} Hz · dBFS")


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
        waveform_controls.addWidget(QLabel("Waveform zoom"))
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(1, 20)
        self.zoom_slider.setValue(1)
        self.zoom_slider.valueChanged.connect(self._set_zoom)
        waveform_controls.addWidget(self.zoom_slider, 1)
        waveform_controls.addWidget(QLabel("Channel"))
        self.channel_select = QComboBox()
        self.channel_select.addItem("All", 0)
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
        spectrum_controls.addWidget(QLabel("Spectrogram frequency scale"))
        self.frequency_scale = QComboBox()
        self.frequency_scale.addItem("Log", "log")
        self.frequency_scale.addItem("Linear", "linear")
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
        metrics_layout.addWidget(QLabel("Audio metrics"))
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
        live_layout.addRow("현재 Peak", self.live_peak)
        live_layout.addRow("주파수 activity", self.live_frequency)
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
        labels = {
            "queued": "분석 대기 중",
            "running": f"분석 중 · {stage}",
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
        self.channel_select.addItem("All", 0)
        raw_channels = result.get("waveform", {}).get("channels", []) if isinstance(result.get("waveform"), dict) else []
        if isinstance(raw_channels, list):
            for index, channel in enumerate(raw_channels, 1):
                if isinstance(channel, dict):
                    self.channel_select.addItem(str(channel.get("label") or f"CH {index}"), index)
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
                    dominant_lines.append(f"  - {item.get('range')}: {float(item.get('energy_ratio', 0)) * 100:.1f}%")
        lines = [
            f"파일: {metadata.get('filename', '-')}",
            f"형식/codec: {metadata.get('format', '-')} / {metadata.get('codec', '-')}",
            f"길이: {float(metadata.get('duration_seconds', 0)):.3f} s",
            f"Sample rate: {metadata.get('sample_rate_hz', '-')} Hz",
            f"Channels: {metadata.get('channels', '-')}",
            f"Bit depth: {metadata.get('bit_depth') or metadata.get('source_bit_depth') or '-'}",
            f"Peak: {float(metrics.get('peak_amplitude', 0)):.6f}",
            f"RMS: {float(metrics.get('rms', 0)):.6f} ({float(metrics.get('rms_dbfs', -120)):.2f} dBFS)",
            f"Clipping: {'detected' if metrics.get('clipping_detected') else 'not detected'} ({metrics.get('clipped_samples', 0)} samples)",
            f"Silence ratio (≤ -60 dBFS): {float(metrics.get('silence_ratio', 0)) * 100:.2f}%",
            f"Spectral centroid: {float(metrics.get('spectral_centroid_hz', 0)):.1f} Hz",
            f"Spectral flatness: {float(metrics.get('spectral_flatness', 0)):.3f}",
            f"Periodic tendency: {float(metrics.get('periodic_tendency', 0)):.3f}",
            f"Transient activity: {float(metrics.get('transient_activity', 0)):.3f}",
            f"Tonal / noise-like: {float(metrics.get('tonal_tendency', 0)):.3f} / {float(metrics.get('noise_like_tendency', 0)):.3f}",
            f"Low / mid / high energy: {float(frequency_energy.get('low', 0)) * 100:.1f}% / {float(frequency_energy.get('mid', 0)) * 100:.1f}% / {float(frequency_energy.get('high', 0)) * 100:.1f}%",
            "Dominant frequency ranges:",
            *(dominant_lines or ["  - unavailable"]),
            "Speech/music/environment classification: not implemented",
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
            f"low {float(item.get('low', 0)) * 100:.1f}% · "
            f"mid {float(item.get('mid', 0)) * 100:.1f}% · "
            f"high {float(item.get('high', 0)) * 100:.1f}%"
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


__all__ = ["AudioAnalysisPage", "SpectrogramWidget", "WaveformWidget"]
