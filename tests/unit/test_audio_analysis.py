import hashlib
import math
import struct
import threading
import wave
from pathlib import Path

import pytest
from nivelle_core.audio_analysis import (
    AudioAnalysisCancelled,
    AudioAnalysisError,
    AudioAnalysisManager,
    analyze_audio_file,
)


def write_wav(
    path: Path,
    *,
    channels: int = 1,
    sample_rate: int = 8_000,
    duration: float = 0.25,
    kind: str = "tone",
) -> Path:
    frame_count = max(round(sample_rate * duration), 1)
    samples: list[int] = []
    for frame in range(frame_count):
        if kind == "silent":
            values = [0] * channels
        elif kind == "clipped":
            values = [32_767 if frame % 2 == 0 else -32_768] * channels
        else:
            left = round(math.sin(2 * math.pi * 440 * frame / sample_rate) * 16_000)
            right = round(math.sin(2 * math.pi * 880 * frame / sample_rate) * 8_000)
            values = [left, right][:channels] if channels <= 2 else [left] * channels
        samples.extend(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


def test_analyzes_valid_wav_with_waveform_spectrogram_and_measured_features(
    tmp_path: Path,
) -> None:
    path = write_wav(tmp_path / "tone.wav")

    result = analyze_audio_file(path)

    assert result["metadata"] == {
        "filename": "tone.wav",
        "format": "WAV",
        "codec": "PCM",
        "duration_seconds": 0.25,
        "sample_rate_hz": 8_000,
        "channels": 1,
        "bit_depth": 16,
        "frame_count": 2_000,
    }
    assert 0.45 < result["metrics"]["peak_amplitude"] < 0.5
    assert result["metrics"]["rms"] > 0
    assert result["metrics"]["clipping_detected"] is False
    assert result["metrics"]["spectral_centroid_hz"] > 0
    assert result["metrics"]["classification"] == "not_implemented"
    assert len(result["waveform"]["channels"]) == 1
    assert len(result["spectrogram"]["power_db"]) <= 384
    assert len(result["spectrogram"]["frequencies_hz"]) == 64
    assert len(result["timeline"]) == len(result["spectrogram"]["times_seconds"])
    assert "P-wave" in result["notes"][0] and "S-wave" in result["notes"][0]


def test_stereo_unicode_filename_keeps_channels_separate(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "한국어 녹음.wav", channels=2)

    result = analyze_audio_file(path)

    assert result["metadata"]["filename"] == "한국어 녹음.wav"
    assert result["metadata"]["channels"] == 2
    assert [item["label"] for item in result["waveform"]["channels"]] == ["L", "R"]
    assert result["metrics"]["rms_by_channel"][0] > result["metrics"]["rms_by_channel"][1]


@pytest.mark.parametrize("sample_width", [1, 3, 4])
def test_native_wav_decoder_reports_supported_pcm_bit_depths(
    tmp_path: Path, sample_width: int
) -> None:
    path = tmp_path / f"pcm-{sample_width * 8}.wav"
    frame_count = 128
    if sample_width == 1:
        payload = bytes([128, 192, 64, 255] * (frame_count // 4))
    elif sample_width == 3:
        values = [0, 4_000_000, -4_000_000, 8_000_000] * (frame_count // 4)
        payload = b"".join(
            (value if value >= 0 else value + (1 << 24)).to_bytes(3, "little")
            for value in values
        )
    else:
        values = [0, 1_000_000_000, -1_000_000_000, 2_000_000_000] * (
            frame_count // 4
        )
        payload = struct.pack(f"<{len(values)}i", *values)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(sample_width)
        stream.setframerate(8_000)
        stream.writeframes(payload)

    result = analyze_audio_file(path)

    assert result["metadata"]["bit_depth"] == sample_width * 8
    assert result["metadata"]["frame_count"] == frame_count


@pytest.mark.parametrize(
    ("kind", "expected_clipping", "minimum_silence"),
    [("silent", False, 1.0), ("clipped", True, 0.0)],
)
def test_silence_and_clipping_metrics(
    tmp_path: Path,
    kind: str,
    expected_clipping: bool,
    minimum_silence: float,
) -> None:
    result = analyze_audio_file(write_wav(tmp_path / f"{kind}.wav", kind=kind))

    assert result["metrics"]["clipping_detected"] is expected_clipping
    assert result["metrics"]["silence_ratio"] >= minimum_silence


def test_short_and_long_files_use_bounded_analysis_data(tmp_path: Path) -> None:
    short = analyze_audio_file(write_wav(tmp_path / "short.wav", duration=0.001))
    long = analyze_audio_file(write_wav(tmp_path / "long.wav", duration=12.0))

    assert short["metadata"]["frame_count"] == 8
    assert len(short["spectrogram"]["power_db"]) == 1
    assert long["waveform"]["points_per_channel"] <= 40_000
    assert long["waveform"]["overview_points_per_channel"] <= 4_000
    assert len(long["spectrogram"]["power_db"]) <= 384


def test_corrupt_unsupported_and_cancelled_audio_fail_safely(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.wav"
    corrupt.write_bytes(b"not a wave")
    unsupported = tmp_path / "sample.bin"
    unsupported.write_bytes(b"data")

    with pytest.raises(AudioAnalysisError, match="corrupt|unsupported"):
        analyze_audio_file(corrupt)
    with pytest.raises(AudioAnalysisError, match="not supported"):
        analyze_audio_file(unsupported)
    cancellation = threading.Event()
    cancellation.set()
    with pytest.raises(AudioAnalysisCancelled):
        analyze_audio_file(write_wav(tmp_path / "cancel.wav"), cancellation=cancellation)


@pytest.mark.asyncio
async def test_manager_caches_completed_content_and_removes_uploads(tmp_path: Path) -> None:
    first = write_wav(tmp_path / "first.wav")
    payload = first.read_bytes()
    content_hash = hashlib.sha256(payload).hexdigest()
    manager = AudioAnalysisManager(tmp_path / "analysis")

    created = manager.start(
        first,
        filename="first.wav",
        content_hash=content_hash,
        size_bytes=len(payload),
    )
    for _ in range(200):
        state = manager.get(created["job_id"])
        assert state is not None
        if state["status"] == "completed":
            break
        await __import__("asyncio").sleep(0.01)
    else:
        pytest.fail("audio analysis job did not complete")

    second = tmp_path / "second.wav"
    second.write_bytes(payload)
    cached = manager.start(
        second,
        filename="second.wav",
        content_hash=content_hash,
        size_bytes=len(payload),
    )
    assert cached["status"] == "completed"
    assert cached["cache_hit"] is True
    assert cached["result"]["metadata"]["filename"] == "second.wav"
    assert not second.exists()
    await manager.shutdown()
