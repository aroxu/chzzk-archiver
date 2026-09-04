# CHZZK Archive

여러 사용자가 치지직 채널을 구독하고 자신에게 귀속된 녹화만 보는, 중복 제거형 라이브 아카이버입니다. 같은 방송을 여러 명이 구독해도 Streamlink/FFmpeg 작업과 실제 파일은 하나만 생성됩니다.

- 구독 채널의 라이브 방송은 자동으로 감지해 녹화합니다.
- 라이브, 다시보기(VOD), 클립 URL은 라이브러리에서 수동 다운로드할 수 있습니다.
- 완료된 MP4는 로그인한 사용자의 웹 플레이어에서 HTTP Range 스트리밍으로 재생됩니다.
- 녹화가 끝나면 기본적으로 로컬 FFmpeg가 HEVC CRF 23으로 압축합니다.

## 실행

1. `.env.example`을 `.env`로 복사하고 `ARCHIVER_SECRET_KEY`와 Fernet 키를 설정합니다.
2. `docker compose up --build -d`를 실행합니다.
3. `http://localhost:8000`에서 최초 관리자 계정을 만듭니다.
4. 관리 화면에서 초대 코드를 발급해 사용자를 초대합니다.
5. `내 채널`에서 라이브 채널을 구독하거나 `라이브러리`에 `/live/`, `/video/`, `/clips/` URL을 입력합니다.

공개 서버에서는 TLS 리버스 프록시를 사용하고 `ARCHIVER_SECURE_COOKIES=true`로 설정해야 합니다. `/data`와 `/recordings`를 함께 백업하십시오.

## Chrome 확장

Chrome의 `chrome://extensions`에서 개발자 모드를 켜고 `extension` 디렉터리를 "압축해제된 확장 프로그램"으로 로드합니다. 웹의 설정 화면에서 페어링 코드를 만든 뒤 서버 주소와 함께 확장 팝업에 입력합니다. 확장은 `NID_AUT`, `NID_SES`만 읽으며 변경 시와 15분마다 동기화합니다.

## 로컬 개발

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[test]"
.venv\Scripts\uvicorn app.main:app --app-dir backend --reload
cd web
npm install
npm run dev
```

테스트는 `.venv\Scripts\pytest`로 실행합니다. 녹화에는 PATH에 `ffmpeg`와 `streamlink`가 필요합니다.

## HEVC 인코딩

기본값은 `auto / quality 23 / preset auto`와 원본 오디오 복사입니다. `auto`는 짧은 시험
인코딩을 통과한 GPU 인코더를 우선 사용하고, 없으면 `libx265`로 대체합니다.
CPU 인코더는 CRF, NVENC는 CQP를 사용합니다. `ARCHIVER_ENCODING_AUDIO=flac24`를 지정하면
24-bit FLAC 오디오를 담기 위해 결과 컨테이너가 MKV로 바뀝니다. 이미 손실 압축된 라이브 AAC를
FLAC으로 변환해도 음질이 복원되지는 않으며 용량은 증가할 수 있습니다.

```dotenv
ARCHIVER_ENCODING_MODE=local
ARCHIVER_ENCODING_VIDEO_ENCODER=auto
ARCHIVER_ENCODING_QUALITY=23
ARCHIVER_ENCODING_PRESET=medium
ARCHIVER_ENCODING_AUDIO=copy
ARCHIVER_MAX_ENCODINGS=1
```

인코딩에 실패하면 캡처 원본을 삭제하지 않고 라이브러리에 그대로 게시합니다. 인코딩 결과가
`ffprobe` 검증을 통과한 경우에만 원본과 원자적으로 교체합니다.

## 원격 실시간 인코딩 워커

원격 모드에서는 HTTP가 작업 lease와 완료 보고만 담당합니다. 실제 미디어는 8011번 전이중 TCP
소켓으로 전송합니다. 컨트롤러는 녹화 중인 MPEG-TS 파일을 끝까지 따라 읽어 워커에 보내는 동시에,
같은 연결의 반대 방향으로 FFmpeg 결과(MPEG-TS, FLAC 모드는 Matroska)를 받습니다. 따라서 녹화 완료를 기다리지 않고 인코딩이
시작되며 워커가 늦게 연결돼도 로컬 원본의 처음부터 처리할 수 있습니다.

```dotenv
# Controller
ARCHIVER_ENCODING_MODE=remote
ARCHIVER_WORKER_TOKEN=replace-with-a-long-random-token
ARCHIVER_WORKER_STREAM_HOST=controller.vpn.example
ARCHIVER_WORKER_STREAM_PORT=8011
```

제어 API에는 HTTPS를 사용하고 TCP 8011 포트는 WireGuard/Tailscale 같은 신뢰할 수 있는 사설망
내에서만 노출하십시오. 워커는 컨트롤러로 아웃바운드 연결만 생성합니다.

Python이 설치된 Windows/Linux 호스트에서는 다음처럼 실행합니다.

```powershell
# 현재 디렉터리의 .env를 자동으로 읽습니다.
archiver-worker --doctor
archiver-worker
```

원격 워커에서도 `ARCHIVER_ENCODING_VIDEO_ENCODER=hevc_nvenc`처럼 설정할 수 있습니다. 특정
인코더가 실제 시험 인코딩에 실패하면 워커는 작업을 받지 않고 오류를 출력합니다. `auto`는
NVENC/QSV/AMF/VAAPI를 실제로 시험한 뒤 가능한 GPU를 고르고, 마지막으로 libx265를 사용합니다.

Windows 단일 실행 파일은 `scripts/build-worker.ps1`로 생성됩니다. 설치된 Python의 유무와 버전에
상관없이 스크립트가 고정된 Python 3.12 런타임과 프로젝트의 `.venv`를 자동 준비합니다. `uv`가
없으면 공식 Windows 실행 파일을 내려받고 SHA-256 체크섬을 검증한 뒤 캐시합니다. 따라서 Python,
`uv`, 가상환경을 수동으로 준비할 필요가 없습니다. 결과는 `dist/worker/archiver-worker.exe`입니다.

```powershell
.\scripts\build-worker.ps1
.\dist\worker\archiver-worker.exe --doctor
```

로그인 사용자 작업으로 설치하려면:

```powershell
.\scripts\install-worker-task.ps1 -Server https://archive.example -Token YOUR_TOKEN -Encoder hevc_nvenc
```

24시간 상시 서비스는 WinSW 실행 파일을 준비한 뒤 다음 스크립트로 설치합니다. 관리자 PowerShell이
필요하며 서비스 실패 시 WinSW가 자동 재시작합니다.

```powershell
.\scripts\install-worker-service.ps1 `
  -Server https://archive.example `
  -Token YOUR_TOKEN `
  -Encoder hevc_nvenc `
  -WinSW C:\path\to\WinSW-x64.exe
```

전용 계정으로 서비스를 돌리려면 `-ServiceAccount`와 `-ServicePassword`를 함께 넘깁니다.
제거는 `.\scripts\uninstall-worker.ps1 -Service` 또는 `-Task`를 사용하며, 설치 파일까지
지우려면 `-RemoveFiles`를 추가합니다.

### Linux

`scripts/build-worker.sh`가 PyInstaller로 단일 실행 파일을 만듭니다. 바이너리는 빌드 머신의
glibc에 링크되므로 지원하려는 배포판 중 가장 오래된 곳에서 빌드하십시오. 대상 호스트에는
`ffmpeg`만 있으면 됩니다.

```bash
./scripts/build-worker.sh
sudo ./scripts/install-worker-systemd.sh \
  --server https://archive.example \
  --token YOUR_TOKEN \
  --encoder hevc_nvenc
```

설치 스크립트는 `chzzk-worker` 시스템 계정을 만들고 바이너리를 `/opt/chzzk-archiver-worker`에
복사한 뒤 systemd 유닛을 등록합니다. 토큰은 `root:chzzk-worker 0640` 환경 파일에만 저장되어
프로세스 목록에 노출되지 않습니다. 상태는 `systemctl status archiver-worker`,
로그는 `journalctl -u archiver-worker -f`로 확인하고, 제거는
`sudo ./scripts/uninstall-worker-systemd.sh`를 실행합니다.

### Docker

```bash
docker build -f worker/Dockerfile -t chzzk-archiver-worker .
docker run --rm \
  -e ARCHIVER_WORKER_SERVER=https://archive.example \
  -e ARCHIVER_WORKER_TOKEN=YOUR_TOKEN \
  -e ARCHIVER_ENCODING_VIDEO_ENCODER=hevc_nvenc \
  --gpus all \
  chzzk-archiver-worker
```

이미지는 비특권 `worker` 계정으로 실행되며 `--doctor` 헬스체크로 FFmpeg와 컨트롤러 연결을
주기적으로 점검합니다. Intel/AMD GPU 인코딩이 필요하면 `--device /dev/dri:/dev/dri`를 추가하고,
NVENC는 NVIDIA Container Toolkit과 `--gpus all`이 필요합니다.

동일한 Compose 네트워크에서 테스트할 때는 컨트롤러를 원격 모드로 설정하고
`docker compose --profile remote-worker up --build`를 사용합니다.

환경 변수 전체 목록, 방화벽 구성, Windows 서비스·작업 스케줄러, Linux systemd, Docker GPU
전달과 장애 진단은 [원격 워커 설치 및 사용 가이드](docs/remote-worker-guide.md)를 참고하십시오.

## GHCR 이미지

`main` 브랜치와 `v*` 태그가 GitHub에 푸시되면 GitHub Actions가 amd64/arm64 이미지를 빌드해
다음 경로로 게시합니다.

```text
ghcr.io/aroxu/chzzk-archiver:latest
ghcr.io/aroxu/chzzk-archiver-worker:latest
```

버전 태그(예: `v0.2.0`)와 커밋 태그(예: `sha-abcdef0`)도 함께 생성됩니다. 공개 패키지는 바로
받을 수 있고, 비공개 패키지는 먼저 `docker login ghcr.io`로 로그인해야 합니다.

```bash
docker compose pull
docker compose up -d --no-build

# 원격 워커까지 실행
docker compose --profile remote-worker pull
docker compose --profile remote-worker up -d --no-build
```

## 백엔드 구조

FastAPI 애플리케이션은 기능별 모듈로 나뉘어 있으며 `app/main.py`는 조립만 담당합니다.

| 모듈 | 역할 |
| --- | --- |
| `app/config.py` | `ARCHIVER_` 접두사 환경변수 설정 |
| `app/db.py` | Peewee SQLite 핸들과 연결 수명주기 |
| `app/models.py` | ORM 모델과 UTC 정규화 `DateTimeField` |
| `app/schema_migrations.py` | 시작 시 테이블 생성과 컬럼 추가 |
| `app/security.py` | 비밀번호 해싱, 세션 토큰, 쿠키 암호화, 인증 의존성 |
| `app/lifecycle.py` | 라이브 감시 스케줄러와 백필 작업 |
| `app/services/chzzk.py` | 치지직 URL 파싱과 공개 API 조회 |
| `app/services/recorder.py` | 중복 제거, 캡처, remux, 상태 관리 |
| `app/services/encoding.py` | 로컬/원격 인코딩 작업, lease, 검증과 원본 교체 |
| `app/services/stream_transport.py` | 실시간 전이중 TCP 미디어 전송 |
| `app/worker.py` | Windows/Linux/Docker 원격 인코딩 워커 |
| `app/services/downloads.py` | progressive 다운로드와 진행률 |
| `app/services/media.py` | 썸네일 생성과 녹화 직렬화 |
| `app/routers/*.py` | 인증, 구독, 녹화, 미디어, 확장, 관리자 엔드포인트 |

ORM은 Peewee를 사용합니다. `ARCHIVER_DATABASE_URL`은 기존 `sqlite:///./data/archiver.db` 형식을 그대로 받습니다.

## 보안 모델

- 일반 사용자의 목록, 재생, 다운로드, 삭제는 모두 entitlement로 제한됩니다.
- 관리자는 전체 통계를 볼 수 있으며 초대 코드를 발급합니다.
- 쿠키는 Fernet으로 암호화되고 로그에 원문을 남기지 않습니다.
- 초대와 페어링 코드는 일회용이며 만료됩니다.
- DRM 우회는 지원하지 않습니다. 녹화 권한이 있는 방송에만 사용하십시오.
