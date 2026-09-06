# CHZZK Archive

여러 사용자가 치지직 채널을 구독하고 자신에게 귀속된 녹화만 보는 중복 제거형 라이브 아카이버입니다. 같은 방송을 여러 명이 구독해도 실제 캡처와 저장 파일은 하나만 생성됩니다.

- 구독 채널의 라이브를 자동 감지하고 즉시 녹화합니다.
- 라이브, VOD, 클립 URL을 라이브러리에서 수동으로 받을 수 있습니다.
- 입력 HLS의 비디오와 AAC를 재인코딩하지 않고 분리 fMP4 HLS로 바로 저장합니다.
- 라디오 모드는 실제 오디오 rendition만 전송합니다.
- FLAC과 다운로드용 MP4는 사용자가 요청할 때 한 번 생성해 캐시합니다.

## 실행

1. `.env.example`을 `.env`로 복사하고 `ARCHIVER_SECRET_KEY`와 Fernet 키를 설정합니다.
2. `docker compose up --build -d`를 실행합니다.
3. `http://localhost:8000`에서 최초 관리자 계정을 만듭니다.
4. 관리 화면에서 초대 코드를 발급해 사용자를 초대합니다.
5. `내 채널`에서 라이브 채널을 구독하거나 `라이브러리`에 치지직 URL을 입력합니다.

공개 서버에서는 TLS 리버스 프록시와 `ARCHIVER_SECURE_COOKIES=true`를 사용하십시오. `/data`와 `/recordings`는 항상 함께 백업해야 합니다.

GHCR 이미지는 `ghcr.io/aroxu/chzzk-archiver:latest`로 배포됩니다.

```bash
docker compose pull
docker compose up -d --no-build
```

## 저장 구조

새 녹화는 중간 `.ts`, 결합 MP4, 후처리 HEVC 인코딩을 만들지 않습니다. 원본 HLS는 여러 세그먼트를 병렬로 미러링하고, progressive MP4 VOD/클립은 aria2로 분할 다운로드한 뒤 재인코딩 없이 HLS로 패키징합니다.

```text
recording.hls/
├── master.m3u8
├── video.m3u8
├── video-init.mp4
├── video-segment_*.m4s
├── audio.m3u8
├── audio-init.mp4
├── audio-segment_*.m4s
├── thumbnail.jpg
├── audio.flac       # FLAC 라디오 모드를 처음 요청할 때 생성
└── download.mp4     # MP4 다운로드를 처음 요청할 때 stream-copy remux
```

기본 라디오 형식은 AAC입니다. 설정에서 FLAC을 선택하면 원본 AAC에서 24-bit FLAC, 압축 레벨 12로 변환합니다. 손실 압축된 AAC의 음질이 복원되는 것은 아니며, 변환 결과는 이후 요청에 재사용됩니다.

## 기존 자료 자동 마이그레이션

서버 시작 시 `storage_version < 3`인 완료 자료를 하나씩 v3 HLS로 마이그레이션하고 DB에 버전과 새 경로를 저장합니다. 완료된 항목은 다음 시작 때 다시 처리하지 않습니다.

- 기존 v2 비디오가 HEVC/H.265이면 브라우저 호환성을 위해 `libx264 -crf 23 -preset medium`으로 H.264 변환합니다.
- 이미 H.264이면 비디오를 재인코딩하지 않고 stream copy 합니다.
- AAC는 가능한 경우 그대로 복사합니다.
- FLAC과 다운로드 MP4는 마이그레이션 때 만들지 않습니다.
- 새 번들을 검증하고 DB 경로를 바꾼 뒤에만 기존 파일을 삭제하므로 중단된 마이그레이션은 재시도할 수 있습니다.

마이그레이션은 HEVC 자료에서 CPU와 추가 임시 공간을 사용할 수 있습니다. 대용량 라이브러리는 첫 기동 전에 `/recordings` 여유 공간을 확인하십시오.

## Chrome 확장

Chrome의 `chrome://extensions`에서 개발자 모드를 켜고 `extension` 디렉터리를 “압축해제된 확장 프로그램”으로 로드합니다. 웹 설정에서 페어링 코드를 만든 뒤 서버 주소와 함께 확장 팝업에 입력합니다. 확장은 `NID_AUT`, `NID_SES`만 동기화합니다.

## 로컬 개발

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[test]"
.venv\Scripts\uvicorn app.main:app --app-dir backend --reload
```

```powershell
cd web
npm install
npm run dev
```

테스트는 `.venv\Scripts\python -m pytest backend/tests -q`, 프런트 검증은 `npm run build`로 실행합니다. 녹화와 마이그레이션에는 PATH의 `ffmpeg`와 `ffprobe`가 필요합니다. progressive VOD/클립 가속에는 `aria2c`를 사용하며, 없으면 일반 HTTP 스트림으로 자동 대체됩니다. Docker 이미지에는 aria2가 포함됩니다.

## 주요 환경 변수

| 변수 | 설명 |
|---|---|
| `ARCHIVER_SECRET_KEY` | 세션 서명 키 |
| `ARCHIVER_COOKIE_ENCRYPTION_KEY` | 치지직 쿠키 암호화용 Fernet 키 |
| `ARCHIVER_SECURE_COOKIES` | HTTPS 환경에서 `true` |
| `ARCHIVER_DATABASE_URL` | SQLite 연결 문자열 |
| `ARCHIVER_RECORDINGS_DIR` | HLS 번들 저장 경로 |
| `ARCHIVER_POLL_INTERVAL` | 라이브 확인 주기(초) |
| `ARCHIVER_MAX_RECORDINGS` | 동시 캡처 수 |
| `ARCHIVER_DOWNLOAD_CONNECTIONS` | progressive VOD/클립 분할 다운로드 연결 수(기본/최대 16) |
| `ARCHIVER_HLS_DOWNLOAD_CONCURRENCY` | HLS 세그먼트 동시 다운로드 수(기본 16, 최대 32) |

원격 인코딩 워커, 별도 TCP 포트, GPU/HEVC 인코딩 설정은 더 이상 사용하지 않습니다.
