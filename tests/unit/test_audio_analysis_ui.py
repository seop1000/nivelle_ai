from pathlib import Path
from typing import Any

from nivelle_link import app as client_app
from nivelle_link.audio_widgets import (
    AUDIO_FILE_FILTER,
    AudioAnalysisPage,
    AudioAnalysisWindow,
)
from nivelle_link.windows import MainChatWindow, ServerConsoleWindow

RESULT = {
    "metadata": {
        "filename": "한국어.wav",
        "format": "WAV",
        "codec": "PCM",
        "duration_seconds": 2.0,
        "sample_rate_hz": 8_000,
        "channels": 2,
        "bit_depth": 16,
    },
    "waveform": {
        "channels": [
            {"channel": 1, "label": "L", "minimum": [-0.5, -0.25], "maximum": [0.5, 0.25]},
            {"channel": 2, "label": "R", "minimum": [-0.2, -0.1], "maximum": [0.2, 0.1]},
        ]
    },
    "spectrogram": {
        "times_seconds": [0.5, 1.5],
        "frequencies_hz": [0.0, 1_000.0, 2_000.0, 4_000.0],
        "power_db": [[-80.0, -20.0, -40.0, -70.0], [-70.0, -30.0, -25.0, -80.0]],
    },
    "metrics": {
        "peak_amplitude": 0.5,
        "rms": 0.2,
        "rms_dbfs": -13.98,
        "clipping_detected": False,
        "clipped_samples": 0,
        "silence_ratio": 0.1,
        "spectral_centroid_hz": 1_100.0,
        "spectral_flatness": 0.2,
        "periodic_tendency": 0.8,
        "transient_activity": 0.1,
        "tonal_tendency": 0.8,
        "noise_like_tendency": 0.2,
        "frequency_energy": {"low": 0.1, "mid": 0.7, "high": 0.2},
        "dominant_frequency_ranges": [{"range": "mid", "energy_ratio": 0.7}],
    },
    "timeline": [
        {"time_seconds": 0.5, "rms": 0.1, "peak": 0.4, "low": 0.1, "mid": 0.8, "high": 0.1},
        {"time_seconds": 1.5, "rms": 0.2, "peak": 0.5, "low": 0.2, "mid": 0.6, "high": 0.2},
    ],
}


def test_audio_analysis_is_a_standalone_menu_window(qtbot: Any) -> None:
    main = MainChatWindow()
    console = ServerConsoleWindow()
    qtbot.addWidget(main)
    qtbot.addWidget(console)

    sections = [
        console.sections.item(index).text()
        for index in range(console.sections.count())
    ]
    assert "오디오 분석" not in sections
    assert main.audio_action.text() == "오디오 분석"

    with qtbot.waitSignal(main.audio_requested):
        main.audio_action.trigger()
    assert isinstance(main.audio_window, AudioAnalysisWindow)
    assert main.audio_window.isVisible()
    page = main.audio_window.page
    page.set_analysis_result(RESULT, cache_hit=True)
    assert page.channel_select.count() == 3
    assert page.position_slider.maximum() == 2_000
    assert "Spectral centroid" in page.metrics.toPlainText()
    assert "캐시 사용" in page.status.text()
    page._position_changed(1_500)
    assert page.live_rms.text() == "0.200000"
    page.frequency_scale.setCurrentIndex(1)
    assert not page.spectrogram.grab().isNull()


def test_audio_page_emits_unicode_selected_path(
    qtbot: Any, tmp_path: Path
) -> None:
    page = AudioAnalysisPage()
    qtbot.addWidget(page)
    path = tmp_path / "한국어 녹음.wav"

    with qtbot.waitSignal(page.file_selected) as signal:
        page.begin_file(path)

    assert signal.args == [str(path)]
    assert str(path) in page.file_label.text()


def test_audio_file_picker_includes_common_compressed_formats() -> None:
    for extension in ("*.mp3", "*.m4a", "*.flac", "*.ogg", "*.opus", "*.wma"):
        assert extension in AUDIO_FILE_FILTER


def test_application_connects_standalone_audio_window_once(
    qtbot: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(client_app, "load_connection_profiles", lambda: [])
    application = client_app.NivelleLinkApplication()
    qtbot.addWidget(application.window)
    selected: list[str] = []
    application._schedule_audio_analysis = selected.append

    application.window.audio_action.trigger()
    assert application.window.console is None
    assert application.window.audio_window is not None
    application.window.audio_window.page.file_selected.emit("D:/첫 번째.wav")

    application.window.audio_action.trigger()
    application.window.audio_window.page.file_selected.emit("D:/두 번째.wav")

    assert selected == ["D:/첫 번째.wav", "D:/두 번째.wav"]
