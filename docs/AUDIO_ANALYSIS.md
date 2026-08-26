# Core Audio Analysis

## 경계와 처리 흐름

오디오 분석은 Link의 `≡` 메뉴에 있는 독립 `오디오 분석` 창에서 시작하지만 계산과
캐시는 Core가 담당한다. Core 관리 창과 Core 로컬 보안 UI에는 오디오 화면이 없다.
Link가 선택한 파일을 인증된 관리자 API로 스트리밍하고, Core는 업로드를 전용 데이터
디렉터리에만 저장한다. 임의 서버 경로는 API 입력으로 받지 않는다.

```text
Link select/drop
  -> authenticated bounded upload
  -> Core analysis job
  -> waveform + spectrogram + metrics cache
  -> polling UI update
```

분석 job은 이벤트 루프 밖의 worker thread에서 실행되며 취소 event, 진행률, 상태 조회를
제공한다. 파일 내용의 SHA-256과 분석 버전으로 JSON 결과를 캐시한다. 업로드 임시 파일은
성공, 실패, 취소 모두에서 삭제하며 최근 job 메모리는 32개로 제한한다. 업로드 한도는
256 MiB, FFmpeg로 변환된 PCM 임시 파일 한도는 2 GiB다.

## 포맷

- PCM WAV 8/16/24/32-bit: Python 표준 라이브러리로 항상 지원
- MP3, FLAC, OGG/OGA, M4A, AAC, WMA: Core 호스트에서 `ffmpeg`가 발견될 때 지원

FFmpeg는 고정 인자 배열과 `shell=False` 경계로 호출한다. FFmpeg가 없는 배포에서 비-WAV
파일은 명시적인 decoder unavailable 오류가 되며 성공으로 보고하지 않는다. Qt 재생 지원과
Core 분석 디코더 지원은 별개다.

## 분석 데이터

- 채널별 min/max waveform detail(최대 40,000 bucket, 전체 채널 80,000 budget)와
  최대 채널당 4,000 bucket overview
- 최대 384 time column × 64 frequency bin STFT spectrogram
- duration, sample rate, channels, bit depth/codec
- peak, RMS, RMS dBFS, clipping sample count, -60 dBFS silence ratio
- spectral centroid, spectral flatness, transient activity
- low/mid/high energy와 dominant frequency range
- periodic, tonal, noise-like tendency
- 재생 위치별 RMS, peak, low/mid/high activity timeline

`rms_dbfs`는 amplitude 추정치이며 LUFS가 아니다. 신뢰 가능한 분류 모델이 없으므로
speech/music/environment 분류는 `not_implemented`로 반환한다. 일반 오디오에 지진파
P-wave/S-wave label을 붙이지 않는다.

## API

- `GET /api/v1/audio-analysis/capabilities`
- `POST /api/v1/audio-analysis/jobs`
- `GET /api/v1/audio-analysis/jobs/{job_id}`
- `DELETE /api/v1/audio-analysis/jobs/{job_id}`

모든 endpoint는 기존 Core 관리자 bearer token을 요구한다. 업로드는
`application/octet-stream`, ASCII URL-encoded `X-Nivelle-Filename`, 정확한
`Content-Length`를 사용한다. 오류 응답과 job 결과에는 서버 로컬 경로와 decoder stderr를
노출하지 않는다.

## Agent tool과의 관계

현재 Agent tool은 Link의 로컬 허용 root, 승인, capability 광고를 거치는 닫힌 client-side
실행 경계다. Core 업로드 캐시의 파일을 그 경계에 섞으면 기존 파일 권한 모델을 우회하게
되므로 이번 변경에는 `analyze_audio_file` Agent tool을 등록하지 않는다. 향후 추가하려면
Link의 안전한 `path_ref`를 입력으로 받아 Link 측 분석 구현과 별도 요약 결과 schema를
공유 레지스트리에 추가해야 한다. UI waveform raw data를 모델 결과로 보내서는 안 된다.
