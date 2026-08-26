import math
import struct
import time
import wave
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from nivelle_core import audio_analysis
from nivelle_core.app import create_app


def write_wav(path: Path, *, duration: float = 0.1) -> bytes:
    sample_rate = 8_000
    samples = [
        round(math.sin(2 * math.pi * 440 * frame / sample_rate) * 12_000)
        for frame in range(max(round(sample_rate * duration), 1))
    ]
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path.read_bytes()


def pair(client: TestClient, app: object) -> dict[str, str]:
    code = app.state.services.pairing.code
    response = client.post(
        "/api/v1/pairing/complete",
        json={"code": code, "device_name": "audio-admin"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def wait_for_job(client: TestClient, headers: dict[str, str], job_id: str) -> dict[str, object]:
    for _ in range(300):
        response = client.get(f"/api/v1/audio-analysis/jobs/{job_id}", headers=headers)
        assert response.status_code == 200
        value = response.json()
        if value["status"] in {"completed", "failed", "cancelled"}:
            return value
        time.sleep(0.01)
    pytest.fail("audio analysis API job did not terminate")


def test_audio_analysis_api_is_admin_only_and_analyzes_unicode_wav(tmp_path: Path) -> None:
    app = create_app(tmp_path / "data")
    payload = write_wav(tmp_path / "실제 녹음.wav")
    with TestClient(app) as client:
        assert client.get("/api/v1/audio-analysis/capabilities").status_code == 401
        headers = pair(client, app)
        capabilities = client.get(
            "/api/v1/audio-analysis/capabilities", headers=headers
        ).json()
        assert "WAV" in capabilities["formats"]

        upload_headers = headers | {
            "Content-Type": "application/octet-stream",
            "X-Nivelle-Filename": quote("실제 녹음.wav", safe=""),
        }
        created = client.post(
            "/api/v1/audio-analysis/jobs", headers=upload_headers, content=payload
        )
        assert created.status_code == 202
        result = wait_for_job(client, headers, created.json()["job_id"])
        assert result["status"] == "completed"
        assert result["result"]["metadata"]["filename"] == "실제 녹음.wav"
        assert result["result"]["waveform"]["channels"]
        assert result["result"]["spectrogram"]["power_db"]


def test_audio_analysis_api_rejects_bad_uploads_and_reports_corrupt_file(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        headers = pair(client, app)
        unsupported = client.post(
            "/api/v1/audio-analysis/jobs",
            headers=headers | {"X-Nivelle-Filename": "payload.exe"},
            content=b"MZ",
        )
        assert unsupported.status_code == 415
        traversal = client.post(
            "/api/v1/audio-analysis/jobs",
            headers=headers | {"X-Nivelle-Filename": quote("../escape.wav", safe="")},
            content=b"not a wave",
        )
        assert traversal.status_code == 400
        empty = client.post(
            "/api/v1/audio-analysis/jobs",
            headers=headers | {"X-Nivelle-Filename": "empty.wav"},
            content=b"",
        )
        assert empty.status_code in {400, 413}
        corrupt = client.post(
            "/api/v1/audio-analysis/jobs",
            headers=headers | {"X-Nivelle-Filename": "corrupt.wav"},
            content=b"not a wave file",
        )
        assert corrupt.status_code == 202
        result = wait_for_job(client, headers, corrupt.json()["job_id"])
        assert result["status"] == "failed"
        assert result["error"]["code"] == "unsupported_audio"
        assert "D:\\" not in result["error"]["message"]


def test_audio_analysis_api_cancellation_is_cooperative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(tmp_path / "data")
    payload = write_wav(tmp_path / "cancel.wav")

    def wait_for_cancellation(
        _path: Path,
        *,
        display_name: str,
        cancellation: object,
        progress: object,
    ) -> dict[str, object]:
        del display_name, progress
        while not cancellation.wait(0.01):
            pass
        raise audio_analysis.AudioAnalysisCancelled()

    monkeypatch.setattr(audio_analysis, "analyze_audio_file", wait_for_cancellation)
    with TestClient(app) as client:
        headers = pair(client, app)
        created = client.post(
            "/api/v1/audio-analysis/jobs",
            headers=headers | {"X-Nivelle-Filename": "cancel.wav"},
            content=payload,
        ).json()
        cancelled = client.delete(
            f"/api/v1/audio-analysis/jobs/{created['job_id']}", headers=headers
        )
        assert cancelled.status_code == 200
        result = wait_for_job(client, headers, created["job_id"])
        assert result["status"] == "cancelled"
        assert result["error"]["code"] == "cancelled"
