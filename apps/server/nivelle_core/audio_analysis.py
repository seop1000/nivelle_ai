from __future__ import annotations

import asyncio
import cmath
import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from array import array
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

AUDIO_ANALYSIS_VERSION = 1
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_DECODED_BYTES = 2 * 1024 * 1024 * 1024
MAX_WAVEFORM_POINTS = 40_000
MAX_WAVEFORM_OVERVIEW_POINTS = 4_000
MAX_WAVEFORM_POINTS_ACROSS_CHANNELS = 80_000
MAX_SPECTROGRAM_COLUMNS = 384
SPECTROGRAM_BINS = 64
FFT_SIZE = 512
MAX_RETAINED_JOBS = 32
SUPPORTED_EXTENSIONS = frozenset(
    {".wav", ".wave", ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".wma"}
)
_FFMPEG_EXTENSIONS = SUPPORTED_EXTENSIONS - {".wav", ".wave"}


class AudioAnalysisError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class AudioAnalysisCancelled(AudioAnalysisError):
    def __init__(self) -> None:
        super().__init__("cancelled", "Audio analysis was cancelled.")


def audio_capabilities() -> dict[str, Any]:
    ffmpeg_available = shutil.which("ffmpeg") is not None
    formats = ["WAV"]
    if ffmpeg_available:
        formats.extend(["MP3", "FLAC", "OGG", "M4A", "AAC", "WMA"])
    return {
        "analysis_version": AUDIO_ANALYSIS_VERSION,
        "formats": formats,
        "ffmpeg_available": ffmpeg_available,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "waveform_points_per_channel": MAX_WAVEFORM_POINTS,
        "spectrogram_columns": MAX_SPECTROGRAM_COLUMNS,
        "spectrogram_bins": SPECTROGRAM_BINS,
    }


def _raise_if_cancelled(cancellation: threading.Event) -> None:
    if cancellation.is_set():
        raise AudioAnalysisCancelled()


def _round_values(values: list[float], digits: int = 6) -> list[float]:
    return [round(value, digits) for value in values]


def _reduce_waveform(
    minimum: list[float], maximum: list[float], target_points: int
) -> tuple[list[float], list[float]]:
    point_count = min(len(minimum), len(maximum))
    if point_count <= target_points:
        return minimum, maximum
    group_size = math.ceil(point_count / target_points)
    reduced_minimum: list[float] = []
    reduced_maximum: list[float] = []
    for start in range(0, point_count, group_size):
        end = min(start + group_size, point_count)
        reduced_minimum.append(min(minimum[start:end]))
        reduced_maximum.append(max(maximum[start:end]))
    return reduced_minimum, reduced_maximum


def _decode_pcm(data: bytes, sample_width: int, channels: int) -> list[list[float]]:
    if channels < 1:
        raise AudioAnalysisError("unsupported_audio", "The audio file has no channels.")
    if sample_width == 1:
        flat = [(value - 128) / 128.0 for value in data]
    elif sample_width == 2:
        values = array("h")
        values.frombytes(data[: len(data) - (len(data) % 2)])
        if sys.byteorder != "little":
            values.byteswap()
        flat = [value / 32768.0 for value in values]
    elif sample_width == 3:
        flat = []
        end = len(data) - (len(data) % 3)
        for offset in range(0, end, 3):
            value = data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16)
            if value & 0x800000:
                value -= 1 << 24
            flat.append(value / 8_388_608.0)
    elif sample_width == 4:
        values = array("i")
        values.frombytes(data[: len(data) - (len(data) % 4)])
        if sys.byteorder != "little":
            values.byteswap()
        flat = [value / 2_147_483_648.0 for value in values]
    else:
        raise AudioAnalysisError(
            "unsupported_audio", f"Unsupported PCM sample width: {sample_width * 8} bit."
        )
    frame_count = len(flat) // channels
    return [flat[channel : frame_count * channels : channels] for channel in range(channels)]


def _fft_power(samples: list[float]) -> list[float]:
    size = FFT_SIZE
    values = [0j] * size
    usable = min(len(samples), size)
    if usable:
        for index in range(usable):
            window = 0.5 - 0.5 * math.cos(2 * math.pi * index / max(usable - 1, 1))
            values[index] = complex(samples[index] * window, 0.0)

    target = 0
    for index in range(1, size):
        bit = size >> 1
        while target & bit:
            target ^= bit
            bit >>= 1
        target ^= bit
        if index < target:
            values[index], values[target] = values[target], values[index]

    length = 2
    while length <= size:
        step = cmath.exp(-2j * math.pi / length)
        half = length // 2
        for start in range(0, size, length):
            rotation = 1 + 0j
            for offset in range(half):
                even = values[start + offset]
                odd = values[start + offset + half] * rotation
                values[start + offset] = even + odd
                values[start + offset + half] = even - odd
                rotation *= step
        length *= 2
    scale = float(size * size)
    return [(abs(value) ** 2) / scale for value in values[: size // 2 + 1]]


def _frequency_bins(power: list[float], sample_rate: int) -> tuple[list[float], list[float]]:
    source_bins = len(power)
    nyquist = sample_rate / 2.0
    frequencies: list[float] = []
    values: list[float] = []
    for output_bin in range(SPECTROGRAM_BINS):
        start = output_bin * source_bins // SPECTROGRAM_BINS
        end = max((output_bin + 1) * source_bins // SPECTROGRAM_BINS, start + 1)
        end = min(end, source_bins)
        values.append(sum(power[start:end]) / max(end - start, 1))
        center = (start + end - 1) / 2.0
        frequencies.append(center * nyquist / max(source_bins - 1, 1))
    return frequencies, values


def _band_energies(frequencies: list[float], values: list[float]) -> dict[str, float]:
    bands = {"low": 0.0, "mid": 0.0, "high": 0.0}
    for frequency, value in zip(frequencies, values, strict=True):
        if frequency < 250:
            bands["low"] += value
        elif frequency < 2_000:
            bands["mid"] += value
        else:
            bands["high"] += value
    total = sum(bands.values()) or 1.0
    return {name: value / total for name, value in bands.items()}


def _spectrogram(
    stream: wave.Wave_read,
    *,
    frame_count: int,
    sample_rate: int,
    channels: int,
    sample_width: int,
    cancellation: threading.Event,
    progress: Callable[[float, str], None],
) -> tuple[dict[str, Any], list[dict[str, float]], dict[str, Any]]:
    if frame_count <= 0:
        return (
            {"times_seconds": [], "frequencies_hz": [], "power_db": []},
            [],
            {
                "spectral_centroid_hz": 0.0,
                "spectral_flatness": 0.0,
                "transient_activity": 0.0,
                "frequency_energy": {"low": 0.0, "mid": 0.0, "high": 0.0},
                "dominant_frequency_ranges": [],
            },
        )
    desired_columns = max(1, math.ceil(frame_count / max(sample_rate // 20, 1)))
    column_count = min(MAX_SPECTROGRAM_COLUMNS, desired_columns)
    positions = [
        min(
            max(int((index + 0.5) * frame_count / column_count) - FFT_SIZE // 2, 0),
            max(frame_count - 1, 0),
        )
        for index in range(column_count)
    ]
    times: list[float] = []
    matrix_db: list[list[float]] = []
    timeline: list[dict[str, float]] = []
    average_power = [0.0] * SPECTROGRAM_BINS
    frequencies: list[float] = []
    previous_rms = 0.0
    rms_deltas: list[float] = []

    for index, position in enumerate(positions):
        _raise_if_cancelled(cancellation)
        stream.setpos(position)
        raw = stream.readframes(FFT_SIZE)
        decoded = _decode_pcm(raw, sample_width, channels)
        mono = [
            sum(channel[sample] for channel in decoded) / channels
            for sample in range(min((len(channel) for channel in decoded), default=0))
        ]
        square_sum = sum(value * value for value in mono)
        rms = math.sqrt(square_sum / max(len(mono), 1))
        peak = max((abs(value) for value in mono), default=0.0)
        power = _fft_power(mono)
        frequencies, binned = _frequency_bins(power, sample_rate)
        for frequency_index, value in enumerate(binned):
            average_power[frequency_index] += value
        band_energy = _band_energies(frequencies, binned)
        timeline.append(
            {
                "time_seconds": round((position + FFT_SIZE / 2) / sample_rate, 6),
                "rms": round(rms, 6),
                "peak": round(peak, 6),
                "low": round(band_energy["low"], 6),
                "mid": round(band_energy["mid"], 6),
                "high": round(band_energy["high"], 6),
            }
        )
        if index:
            rms_deltas.append(abs(rms - previous_rms))
        previous_rms = rms
        times.append(round((position + FFT_SIZE / 2) / sample_rate, 6))
        matrix_db.append(
            [round(max(-120.0, min(0.0, 10 * math.log10(value + 1e-12))), 1) for value in binned]
        )
        if index % 8 == 0 or index + 1 == column_count:
            progress(0.55 + 0.4 * (index + 1) / column_count, "spectrogram")

    average_power = [value / column_count for value in average_power]
    total_power = sum(average_power)
    centroid = (
        sum(frequency * value for frequency, value in zip(frequencies, average_power, strict=True))
        / total_power
        if total_power > 0
        else 0.0
    )
    arithmetic_mean = total_power / max(len(average_power), 1)
    geometric_mean = math.exp(
        sum(math.log(value + 1e-15) for value in average_power) / max(len(average_power), 1)
    )
    flatness = min(geometric_mean / arithmetic_mean, 1.0) if arithmetic_mean > 0 else 0.0
    frequency_energy = _band_energies(frequencies, average_power)
    named_ranges = {
        "low (20–250 Hz)": frequency_energy["low"],
        "mid (250–2,000 Hz)": frequency_energy["mid"],
        "high (2,000 Hz–Nyquist)": frequency_energy["high"],
    }
    dominant = [
        {"range": name, "energy_ratio": round(value, 6)}
        for name, value in sorted(named_ranges.items(), key=lambda item: item[1], reverse=True)
    ]
    mean_rms = sum(item["rms"] for item in timeline) / max(len(timeline), 1)
    transient = min(
        (sum(rms_deltas) / max(len(rms_deltas), 1)) / max(mean_rms, 1e-9), 1.0
    )
    features = {
        "spectral_centroid_hz": round(centroid, 3),
        "spectral_flatness": round(flatness, 6),
        "transient_activity": round(transient, 6),
        "periodic_tendency": round(max(0.0, 1.0 - flatness), 6),
        "tonal_tendency": round(max(0.0, 1.0 - flatness), 6),
        "noise_like_tendency": round(flatness, 6),
        "frequency_energy": {name: round(value, 6) for name, value in frequency_energy.items()},
        "dominant_frequency_ranges": dominant,
        "classification": "not_implemented",
    }
    return (
        {
            "times_seconds": times,
            "frequencies_hz": _round_values(frequencies, 3),
            "power_db": matrix_db,
            "scale": "dBFS",
        },
        timeline,
        features,
    )


def _analyze_pcm_wave(
    path: Path,
    *,
    display_name: str,
    source_format: str,
    source_metadata: dict[str, Any] | None,
    cancellation: threading.Event,
    progress: Callable[[float, str], None],
) -> dict[str, Any]:
    try:
        stream = wave.open(str(path), "rb")  # noqa: SIM115 - open errors need a safe mapping.
    except (EOFError, wave.Error, OSError) as exc:
        raise AudioAnalysisError(
            "unsupported_audio", "The audio file is corrupt or uses an unsupported WAV encoding."
        ) from exc
    with stream:
        if stream.getcomptype() != "NONE":
            raise AudioAnalysisError(
                "unsupported_audio", "Only uncompressed PCM WAV data is supported natively."
            )
        channels = stream.getnchannels()
        sample_rate = stream.getframerate()
        frame_count = stream.getnframes()
        sample_width = stream.getsampwidth()
        if channels < 1 or channels > 32 or sample_rate < 1 or frame_count < 0:
            raise AudioAnalysisError("unsupported_audio", "The audio metadata is invalid.")
        if sample_width not in {1, 2, 3, 4}:
            raise AudioAnalysisError("unsupported_audio", "The PCM bit depth is unsupported.")
        duration = frame_count / sample_rate
        point_limit = min(
            MAX_WAVEFORM_POINTS,
            max(MAX_WAVEFORM_OVERVIEW_POINTS, MAX_WAVEFORM_POINTS_ACROSS_CHANNELS // channels),
        )
        bucket_size = max(1, math.ceil(frame_count / point_limit))
        minima: list[list[float]] = [[] for _ in range(channels)]
        maxima: list[list[float]] = [[] for _ in range(channels)]
        bucket_min = [1.0] * channels
        bucket_max = [-1.0] * channels
        bucket_count = 0
        square_sums = [0.0] * channels
        peaks = [0.0] * channels
        clipped = [0] * channels
        silent = [0] * channels
        samples_seen = [0] * channels
        clipping_threshold = 1.0 - 1.0 / float(1 << max(sample_width * 8 - 1, 1))
        silence_threshold = 10 ** (-60 / 20)
        processed_frames = 0

        while processed_frames < frame_count:
            _raise_if_cancelled(cancellation)
            raw = stream.readframes(min(8_192, frame_count - processed_frames))
            if not raw:
                break
            decoded = _decode_pcm(raw, sample_width, channels)
            decoded_frames = min((len(channel) for channel in decoded), default=0)
            for frame_index in range(decoded_frames):
                for channel_index, channel in enumerate(decoded):
                    value = channel[frame_index]
                    absolute = abs(value)
                    square_sums[channel_index] += value * value
                    peaks[channel_index] = max(peaks[channel_index], absolute)
                    clipped[channel_index] += int(absolute >= clipping_threshold)
                    silent[channel_index] += int(absolute <= silence_threshold)
                    samples_seen[channel_index] += 1
                    bucket_min[channel_index] = min(bucket_min[channel_index], value)
                    bucket_max[channel_index] = max(bucket_max[channel_index], value)
                bucket_count += 1
                if bucket_count >= bucket_size:
                    for channel_index in range(channels):
                        minima[channel_index].append(bucket_min[channel_index])
                        maxima[channel_index].append(bucket_max[channel_index])
                        bucket_min[channel_index] = 1.0
                        bucket_max[channel_index] = -1.0
                    bucket_count = 0
            processed_frames += decoded_frames
            if frame_count:
                progress(0.05 + 0.45 * processed_frames / frame_count, "waveform")
        if bucket_count:
            for channel_index in range(channels):
                minima[channel_index].append(bucket_min[channel_index])
                maxima[channel_index].append(bucket_max[channel_index])

        if processed_frames != frame_count:
            raise AudioAnalysisError("corrupt_audio", "The WAV data ended before its metadata indicated.")
        rms_by_channel = [
            math.sqrt(square_sums[index] / max(samples_seen[index], 1))
            for index in range(channels)
        ]
        sample_total = sum(samples_seen)
        combined_rms = math.sqrt(sum(square_sums) / max(sample_total, 1))
        peak = max(peaks, default=0.0)
        waveform_channels: list[dict[str, Any]] = [
            {
                "channel": index + 1,
                "label": "L" if index == 0 and channels == 2 else "R" if index == 1 and channels == 2 else f"CH {index + 1}",
                "minimum": _round_values(minima[index]),
                "maximum": _round_values(maxima[index]),
            }
            for index in range(channels)
        ]
        overview_channels: list[dict[str, Any]] = []
        for waveform_channel in waveform_channels:
            overview_minimum, overview_maximum = _reduce_waveform(
                waveform_channel["minimum"],
                waveform_channel["maximum"],
                MAX_WAVEFORM_OVERVIEW_POINTS,
            )
            overview_channels.append(
                {
                    "channel": waveform_channel["channel"],
                    "label": waveform_channel["label"],
                    "minimum": overview_minimum,
                    "maximum": overview_maximum,
                }
            )
        waveform = {
            "points_per_channel": max((len(values) for values in minima), default=0),
            "overview_points_per_channel": max(
                (len(item["minimum"]) for item in overview_channels), default=0
            ),
            "bucket_frames": bucket_size,
            "channels": waveform_channels,
            "overview_channels": overview_channels,
        }

        stream.rewind()
        spectrogram, timeline, features = _spectrogram(
            stream,
            frame_count=frame_count,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            cancellation=cancellation,
            progress=progress,
        )

    metadata = {
        "filename": display_name,
        "format": source_format,
        "codec": "PCM" if source_metadata is None else source_metadata.get("codec", "unknown"),
        "duration_seconds": round(duration, 6),
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "bit_depth": sample_width * 8,
        "frame_count": frame_count,
    }
    if source_metadata:
        metadata.update({key: value for key, value in source_metadata.items() if value is not None})
        metadata["duration_seconds"] = round(duration, 6)
        metadata["sample_rate_hz"] = sample_rate
        metadata["channels"] = channels
        metadata["decoded_bit_depth"] = sample_width * 8
        metadata["bit_depth"] = source_metadata.get("source_bit_depth") or sample_width * 8
    metrics = {
        "peak_amplitude": round(peak, 6),
        "peak_by_channel": _round_values(peaks),
        "rms": round(combined_rms, 6),
        "rms_by_channel": _round_values(rms_by_channel),
        "rms_dbfs": round(20 * math.log10(max(combined_rms, 1e-12)), 3),
        "clipping_detected": sum(clipped) > 0,
        "clipped_samples": sum(clipped),
        "clipped_samples_by_channel": clipped,
        "silence_ratio": round(sum(silent) / max(sample_total, 1), 6),
        **features,
    }
    progress(1.0, "complete")
    return {
        "analysis_version": AUDIO_ANALYSIS_VERSION,
        "metadata": metadata,
        "waveform": waveform,
        "spectrogram": spectrogram,
        "metrics": metrics,
        "timeline": timeline,
        "notes": [
            "Signal features are measurements; no seismic P-wave/S-wave label is inferred.",
            "rms_dbfs is an amplitude estimate, not standards-compliant LUFS loudness.",
        ],
    }


def _probe_audio(path: Path, cancellation: threading.Event) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return {}
    _raise_if_cancelled(cancellation)
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels,bits_per_sample",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout)
        stream = payload.get("streams", [])[0]
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(stream, dict):
        return {}
    def positive_int(value: object) -> int | None:
        text = str(value or "")
        parsed = int(text) if text.isdigit() else 0
        return parsed if parsed > 0 else None

    return {
        "codec": str(stream.get("codec_name") or "unknown"),
        "source_sample_rate_hz": positive_int(stream.get("sample_rate")),
        "source_channels": positive_int(stream.get("channels")),
        "source_bit_depth": positive_int(stream.get("bits_per_sample")),
    }


def _decode_with_ffmpeg(
    path: Path, cancellation: threading.Event, progress: Callable[[float, str], None]
) -> tuple[Path, dict[str, Any]]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AudioAnalysisError(
            "decoder_unavailable",
            "This format requires FFmpeg on the Core server. PCM WAV is always supported.",
        )
    metadata = _probe_audio(path, cancellation)
    descriptor, decoded_name = tempfile.mkstemp(prefix="nivelle-audio-", suffix=".wav")
    os.close(descriptor)
    decoded = Path(decoded_name)
    command = [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        "-c:a",
        "pcm_s16le",
        str(decoded),
    ]
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        while process.poll() is None:
            if cancellation.wait(0.05):
                process.kill()
                process.wait(timeout=5)
                raise AudioAnalysisCancelled()
            if time.monotonic() - started > 600:
                process.kill()
                process.wait(timeout=5)
                raise AudioAnalysisError("timed_out", "Audio decoding exceeded ten minutes.")
            try:
                if decoded.stat().st_size > MAX_DECODED_BYTES:
                    process.kill()
                    process.wait(timeout=5)
                    raise AudioAnalysisError(
                        "audio_too_large", "Decoded audio exceeds the 2 GiB safety limit."
                    )
            except FileNotFoundError:
                pass
        if process.returncode != 0:
            raise AudioAnalysisError(
                "unsupported_audio",
                "FFmpeg could not decode the selected audio file.",
            )
        progress(0.04, "decoded")
        return decoded, metadata
    except OSError as exc:
        raise AudioAnalysisError("decoder_failed", "The Core audio decoder could not start.") from exc
    except Exception:
        decoded.unlink(missing_ok=True)
        raise


def analyze_audio_file(
    path: Path,
    *,
    display_name: str | None = None,
    cancellation: threading.Event | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    cancel = cancellation or threading.Event()
    update = progress or (lambda _value, _stage: None)
    suffix = path.suffix.casefold()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise AudioAnalysisError("unsupported_audio", "The selected file type is not supported.")
    _raise_if_cancelled(cancel)
    update(0.01, "metadata")
    source_format = suffix.removeprefix(".").upper()
    decoded: Path | None = None
    try:
        if suffix in _FFMPEG_EXTENSIONS:
            decoded, source_metadata = _decode_with_ffmpeg(path, cancel, update)
            analysis_path = decoded
        else:
            analysis_path = path
            source_metadata = None
        return _analyze_pcm_wave(
            analysis_path,
            display_name=display_name or path.name,
            source_format=source_format,
            source_metadata=source_metadata,
            cancellation=cancel,
            progress=update,
        )
    finally:
        if decoded is not None:
            decoded.unlink(missing_ok=True)


class AudioAnalysisCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, content_hash: str) -> Path:
        return self.directory / f"v{AUDIO_ANALYSIS_VERSION}-{content_hash}.json"

    def load(self, content_hash: str) -> dict[str, Any] | None:
        path = self._path(content_hash)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("analysis_version") != AUDIO_ANALYSIS_VERSION:
            return None
        return value

    def store(self, content_hash: str, result: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".audio-analysis-", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(content_hash))
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(slots=True)
class AudioAnalysisJob:
    job_id: str
    source_path: Path
    filename: str
    content_hash: str
    size_bytes: int
    status: str = "queued"
    progress: float = 0.0
    stage: str = "queued"
    cache_hit: bool = False
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    cancellation: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "progress": round(self.progress, 4),
            "stage": self.stage,
            "cache_hit": self.cache_hit,
            "result": self.result,
            "error": (
                {"code": self.error_code, "message": self.error_message}
                if self.error_code is not None
                else None
            ),
        }


class AudioAnalysisManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.upload_directory = root / "uploads"
        self.cache = AudioAnalysisCache(root / "cache")
        self._jobs: dict[str, AudioAnalysisJob] = {}
        self._jobs_by_hash: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._worker_slot = asyncio.Semaphore(1)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="nivelle-audio-analysis"
        )

    def start(
        self, source_path: Path, *, filename: str, content_hash: str, size_bytes: int
    ) -> dict[str, Any]:
        self._prune_jobs()
        current_id = self._jobs_by_hash.get(content_hash)
        if current_id is not None:
            current = self._jobs.get(current_id)
            if current is not None and current.status in {"queued", "running", "cancelling"}:
                source_path.unlink(missing_ok=True)
                return current.snapshot()
        job = AudioAnalysisJob(
            job_id=str(uuid4()),
            source_path=source_path,
            filename=filename,
            content_hash=content_hash,
            size_bytes=size_bytes,
        )
        cached = self.cache.load(content_hash)
        if cached is not None:
            cached = copy.deepcopy(cached)
            metadata = cached.get("metadata")
            if isinstance(metadata, dict):
                metadata["filename"] = filename
            job.status = "completed"
            job.progress = 1.0
            job.stage = "cache"
            job.cache_hit = True
            job.result = cached
            source_path.unlink(missing_ok=True)
        else:
            self._tasks[job.job_id] = asyncio.create_task(
                self._run(job), name=f"nivelle-audio-analysis-{job.job_id}"
            )
        self._jobs[job.job_id] = job
        self._jobs_by_hash[content_hash] = job.job_id
        return job.snapshot()

    def _prune_jobs(self) -> None:
        if len(self._jobs) < MAX_RETAINED_JOBS:
            return
        terminal = sorted(
            (
                job
                for job in self._jobs.values()
                if job.status in {"completed", "failed", "cancelled"}
            ),
            key=lambda job: job.created_at,
        )
        remove_count = len(self._jobs) - MAX_RETAINED_JOBS + 1
        for job in terminal[:remove_count]:
            self._jobs.pop(job.job_id, None)
            if self._jobs_by_hash.get(job.content_hash) == job.job_id:
                self._jobs_by_hash.pop(job.content_hash, None)

    async def _run(self, job: AudioAnalysisJob) -> None:
        def update(value: float, stage: str) -> None:
            job.progress = max(job.progress, min(max(value, 0.0), 1.0))
            job.stage = stage

        try:
            async with self._worker_slot:
                _raise_if_cancelled(job.cancellation)
                job.status = "running"
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    self._executor,
                    partial(
                        analyze_audio_file,
                        job.source_path,
                        display_name=job.filename,
                        cancellation=job.cancellation,
                        progress=update,
                    ),
                )
                result["cache_key"] = job.content_hash
                await loop.run_in_executor(
                    self._executor,
                    self.cache.store,
                    job.content_hash,
                    result,
                )
                job.result = result
                job.progress = 1.0
                job.stage = "complete"
                job.status = "completed"
        except AudioAnalysisCancelled as exc:
            job.status = "cancelled"
            job.stage = "cancelled"
            job.error_code = exc.code
            job.error_message = exc.safe_message
        except AudioAnalysisError as exc:
            job.status = "failed"
            job.stage = "failed"
            job.error_code = exc.code
            job.error_message = exc.safe_message
        except Exception:  # noqa: BLE001 - worker failures must become bounded job errors.
            job.status = "failed"
            job.stage = "failed"
            job.error_code = "analysis_failed"
            job.error_message = "The Core audio analysis failed unexpectedly."
        finally:
            job.source_path.unlink(missing_ok=True)
            self._tasks.pop(job.job_id, None)

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        return job.snapshot() if job is not None else None

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.status in {"queued", "running", "cancelling"}:
            job.cancellation.set()
            job.status = "cancelling"
            job.stage = "cancelling"
        return job.snapshot()

    async def shutdown(self) -> None:
        for job in self._jobs.values():
            job.cancellation.set()
        tasks = tuple(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "AUDIO_ANALYSIS_VERSION",
    "MAX_UPLOAD_BYTES",
    "SUPPORTED_EXTENSIONS",
    "AudioAnalysisCancelled",
    "AudioAnalysisError",
    "AudioAnalysisManager",
    "analyze_audio_file",
    "audio_capabilities",
    "sha256_file",
]
