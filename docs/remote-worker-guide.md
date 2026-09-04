# 원격 인코딩 워커 설치 및 사용 가이드

원격 워커는 CHZZK Archive 컨트롤러에서 작업을 받아 Windows/Linux/Docker 호스트의 FFmpeg로
HEVC 인코딩합니다. 제어 요청은 HTTP(S), 실제 영상은 별도의 전이중 TCP 연결을 사용합니다.
워커가 컨트롤러 방향으로만 연결하므로 워커 호스트에 수신 포트를 열 필요는 없습니다.

## 1. 컨트롤러 설정

컨트롤러의 `.env`에 다음 값을 넣고 서버를 재시작합니다.

```dotenv
ARCHIVER_ENCODING_MODE=remote
ARCHIVER_ENCODING_VIDEO_ENCODER=auto
ARCHIVER_ENCODING_QUALITY=23
ARCHIVER_ENCODING_PRESET=auto
ARCHIVER_ENCODING_AUDIO=copy
ARCHIVER_WORKER_TOKEN=충분히-긴-임의의-공유-토큰
ARCHIVER_WORKER_STREAM_HOST=controller.example.internal
ARCHIVER_WORKER_STREAM_PORT=8011
```

- `ARCHIVER_ENCODING_VIDEO_ENCODER`: `auto`, `hevc_nvenc`, `hevc_qsv`, `hevc_amf`,
  `hevc_vaapi`, `libx265` 중 하나입니다.
- `auto`: 워커가 보고한 실제 사용 가능 인코더 중 가장 빠른 것을 사용합니다.
- `ARCHIVER_ENCODING_QUALITY=23`: libx265에서는 CRF 23, NVENC/AMF에서는 CQP 23,
  QSV에서는 global quality 23으로 해석합니다.
- `ARCHIVER_ENCODING_PRESET=auto`: 인코더별 균형 프리셋으로 변환됩니다. NVENC는 `p5`,
  libx265/QSV는 `medium`, AMF는 `balanced`입니다.
- `ARCHIVER_ENCODING_AUDIO=copy`: 라이브 원본 음성을 그대로 보존합니다. `flac24`는 MKV와
  24-bit FLAC을 사용하지만 손실 음원의 품질을 복원하지 않으며 용량이 늘 수 있습니다.

컨트롤러의 API 포트와 TCP 8011 포트를 워커에서 접근할 수 있어야 합니다. 인터넷에 직접
노출하기보다 WireGuard/Tailscale 또는 내부망을 권장합니다. API는 HTTPS를 사용하고 TCP 포트는
신뢰할 수 있는 네트워크에서만 허용하십시오.

## 2. 워커 환경 변수

바이너리는 현재 작업 디렉터리의 `.env`를 자동으로 읽습니다. 실제 운영 환경 변수는 `.env`보다
우선합니다. Windows 서비스와 Linux 설치 스크립트는 같은 값을 서비스 설정에 안전하게 저장합니다.

```dotenv
ARCHIVER_WORKER_SERVER=https://archive.example
ARCHIVER_WORKER_TOKEN=컨트롤러와-같은-토큰
ARCHIVER_WORKER_ID=encoder-bedroom-01
ARCHIVER_WORKER_FFMPEG=ffmpeg
ARCHIVER_WORKER_POLL_INTERVAL=5
ARCHIVER_ENCODING_VIDEO_ENCODER=hevc_nvenc
```

`ARCHIVER_ENCODING_VIDEO_ENCODER`는 워커가 받을 작업을 제한합니다. 예를 들어 `hevc_nvenc`로
설정한 워커는 NVENC 시험 인코딩이 성공할 때만 `hevc_nvenc` 작업을 받습니다. 설정하지 않거나
`auto`로 두면 실제로 동작하는 GPU 인코더를 우선 사용하고 `libx265`를 예비 인코더로 둡니다.

먼저 진단을 실행하십시오.

```text
archiver-worker --doctor
```

출력의 `configured_encoder`, `detected_hevc_encoders`, `advertised_hevc_encoders`를 확인합니다.
FFmpeg가 인코더 이름만 나열하는 것으로는 충분하지 않아, 워커는 약 0.2초 분량의 시험 인코딩을
실행해 실제 사용 가능 여부를 확인합니다.

환경 변수 대신 일회성으로 `--encoder hevc_nvenc`를 줄 수도 있습니다. 명령줄 옵션이 환경 변수보다
우선합니다. 공유 토큰은 프로세스 목록에 노출되지 않도록 명령줄보다 환경 변수 사용을 권장합니다.

## 3. Windows 바이너리

### 빌드

프로젝트 루트의 PowerShell에서 실행합니다.

```powershell
.\scripts\build-worker.ps1
.\dist\worker\archiver-worker.exe --doctor
```

Python 3.12 이상만 설치되어 있으면 됩니다. `.venv`가 없는 새 환경에서는 빌드 스크립트가
`python3`, `python`, Windows Python Launcher(`py`) 순서로 Python을 찾아 `.venv`를 자동 생성하고
전체 런타임 의존성을 격리된 빌드 환경에 설치합니다. `uv`나 수동 가상환경 준비는 필요 없습니다.

생성된 `dist\worker\archiver-worker.exe`와 FFmpeg가 필요합니다. FFmpeg가 PATH에 없다면
`ARCHIVER_WORKER_FFMPEG=C:\ffmpeg\bin\ffmpeg.exe`로 지정합니다.

### 작업 스케줄러 설치

GPU 드라이버가 로그인 세션에서만 안정적이거나 관리자 권한 없는 운영에 적합합니다.

```powershell
.\scripts\install-worker-task.ps1 `
  -Server https://archive.example `
  -Token YOUR_TOKEN `
  -Encoder hevc_nvenc
```

현재 Windows 사용자의 환경 변수에 설정을 저장하고 로그인 시 시작되는 작업을 등록합니다.
설정 변경 후 같은 명령으로 다시 설치하거나 사용자 환경 변수를 바꾼 뒤 작업을 재시작합니다.

### Windows 서비스 설치

24시간 상시 호스트에는 WinSW 서비스가 적합합니다. 관리자 PowerShell에서 실행합니다.

```powershell
.\scripts\install-worker-service.ps1 `
  -Server https://archive.example `
  -Token YOUR_TOKEN `
  -Encoder hevc_nvenc `
  -WinSW C:\tools\WinSW-x64.exe
```

설정은 `%ProgramData%\CHZZKArchiveWorker` 아래에 저장되며 관리자와 SYSTEM만 읽을 수 있습니다.
서비스 계정을 별도로 쓰려면 `-ServiceAccount`와 `-ServicePassword`를 추가하십시오. 네트워크 공유
드라이브 문자는 서비스에서 보이지 않을 수 있지만, 이 워커는 네트워크 스트림을 사용하므로 공유
드라이브가 필요하지 않습니다.

제거:

```powershell
.\scripts\uninstall-worker.ps1 -Service
.\scripts\uninstall-worker.ps1 -Task
```

## 4. Linux 바이너리와 systemd

지원하려는 배포판 중 가장 오래된 glibc 환경에서 빌드합니다.

```bash
./scripts/build-worker.sh
sudo ./scripts/install-worker-systemd.sh \
  --server https://archive.example \
  --token YOUR_TOKEN \
  --encoder hevc_nvenc
```

설치 스크립트는 `/opt/chzzk-archiver-worker/worker.env`를 `root:chzzk-worker 0640`으로 만들고,
서비스 계정을 `video`와 `render` 그룹에 추가합니다. 상태와 로그:

```bash
systemctl status archiver-worker
journalctl -u archiver-worker -f
sudo -u chzzk-worker bash -c \
  'set -a; . /opt/chzzk-archiver-worker/worker.env; exec /opt/chzzk-archiver-worker/archiver-worker --doctor'
```

마지막 명령은 토큰을 프로세스 명령줄에 펼치지 않고 서비스 계정과 동일한 환경으로 검사합니다.
제거는 `sudo ./scripts/uninstall-worker-systemd.sh`입니다.

## 5. Docker

미리 빌드된 원격 워커 이미지는 `ghcr.io/aroxu/chzzk-archiver-worker:latest`에서 받을 수 있습니다.
고정 배포가 필요하면 `latest` 대신 릴리스 버전 또는 `sha-...` 태그를 사용하십시오.

### CPU 또는 자동 선택

```bash
docker pull ghcr.io/aroxu/chzzk-archiver-worker:latest
docker run -d --name chzzk-worker --restart unless-stopped \
  --env-file .env.worker \
  ghcr.io/aroxu/chzzk-archiver-worker:latest
```

`.env.worker`에는 2절의 환경 변수를 넣습니다.

### NVIDIA NVENC

호스트에 NVIDIA 드라이버와 NVIDIA Container Toolkit을 설치한 뒤 실행합니다.

```bash
docker run -d --name chzzk-worker --restart unless-stopped \
  --gpus all \
  --env-file .env.worker \
  -e ARCHIVER_ENCODING_VIDEO_ENCODER=hevc_nvenc \
  ghcr.io/aroxu/chzzk-archiver-worker:latest
```

### Intel QSV / AMD VAAPI

```bash
docker run -d --name chzzk-worker --restart unless-stopped \
  --device /dev/dri:/dev/dri \
  --env-file .env.worker \
  -e ARCHIVER_ENCODING_VIDEO_ENCODER=hevc_qsv \
  ghcr.io/aroxu/chzzk-archiver-worker:latest
```

AMD 환경은 FFmpeg와 드라이버에 따라 `hevc_vaapi` 또는 `hevc_amf`를 선택합니다. 현재 기본 Docker
이미지는 Debian FFmpeg를 사용하므로 Linux AMD에서는 일반적으로 VAAPI가 맞습니다.

프로젝트의 Compose 구성은 동일한 `.env`에서 다음 값을 전달합니다.

```dotenv
ARCHIVER_ENCODING_MODE=remote
ARCHIVER_ENCODING_VIDEO_ENCODER=hevc_nvenc
ARCHIVER_WORKER_TOKEN=YOUR_TOKEN
ARCHIVER_WORKER_ID=docker-gpu-01
```

```bash
docker compose --profile remote-worker up -d --build
```

NVENC를 Compose로 사용할 때는 `encoder-worker` 서비스에 Docker Compose의 GPU 장치 예약을 추가하거나
호스트 환경에 맞게 `gpus: all`을 설정해야 합니다.

## 6. 동작 확인과 문제 해결

1. 워커에서 `archiver-worker --doctor`가 설정한 인코더를 `advertised_hevc_encoders`에 표시하는지
   확인합니다.
2. 컨트롤러 로그에서 워커 등록과 작업 lease를 확인합니다.
3. 녹화를 시작하고 인코딩 작업의 `used_encoder`가 기대한 값인지 확인합니다.
4. GPU 사용률은 NVIDIA의 `nvidia-smi`, Intel/AMD의 `intel_gpu_top`/`radeontop`으로 확인합니다.

자주 발생하는 문제:

- `설정한 HEVC 인코더를 사용할 수 없습니다`: FFmpeg 빌드, GPU 드라이버, 컨테이너 장치 전달을
  확인합니다. `auto`로 바꾸면 가능한 인코더로 자동 전환합니다.
- 워커가 작업을 받지 않음: 컨트롤러와 워커의 명시적 인코더 설정이 서로 다른지 확인합니다.
- API 연결 실패: `ARCHIVER_WORKER_SERVER`, TLS 인증서, 방화벽을 확인합니다.
- 스트림 연결 실패: 워커에서 `ARCHIVER_WORKER_STREAM_HOST:8011`에 접근 가능한지 확인합니다.
- 서비스에서는 FFmpeg를 못 찾음: 절대 경로를 `ARCHIVER_WORKER_FFMPEG`로 지정합니다.
- Docker에서 NVENC가 안 보임: `--gpus all`, NVIDIA Container Toolkit, 컨테이너에서 보이는
  `/dev/nvidia*` 장치를 확인합니다.
