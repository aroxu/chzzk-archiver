# CHZZK Archive

여러 사용자가 치지직 채널을 구독하고 자신에게 귀속된 녹화만 보는, 중복 제거형 라이브 아카이버입니다. 같은 방송을 여러 명이 구독해도 Streamlink/FFmpeg 작업과 실제 파일은 하나만 생성됩니다.

- 구독 채널의 라이브 방송은 자동으로 감지해 녹화합니다.
- 라이브, 다시보기(VOD), 클립 URL은 라이브러리에서 수동 다운로드할 수 있습니다.
- 완료된 MP4는 로그인한 사용자의 웹 플레이어에서 HTTP Range 스트리밍으로 재생됩니다.

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

## 보안 모델

- 일반 사용자의 목록, 재생, 다운로드, 삭제는 모두 entitlement로 제한됩니다.
- 관리자는 전체 통계를 볼 수 있으며 초대 코드를 발급합니다.
- 쿠키는 Fernet으로 암호화되고 로그에 원문을 남기지 않습니다.
- 초대와 페어링 코드는 일회용이며 만료됩니다.
- DRM 우회는 지원하지 않습니다. 녹화 권한이 있는 방송에만 사용하십시오.
