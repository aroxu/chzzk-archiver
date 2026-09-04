# 저장소 백엔드 추상화 설계 계획서

> 상태: **제안(Proposal)** — 이 문서는 설계 계획서이며, 여기에 등장하는 모든 코드 블록은
> "제안된 시그니처"일 뿐 실제 구현물이 아니다. 구현은 9장의 단계별 계획에 따라 별도 PR로 진행한다.
>
> 기준 코드: `backend/app` 모듈 분리 및 Peewee 전환이 완료된 시점의 트리.

## 목차

1. [목표와 비목표](#1-목표와-비목표)
2. [현재 로컬 파일시스템 결합 지점](#2-현재-로컬-파일시스템-결합-지점)
3. [StorageBackend 추상 인터페이스](#3-storagebackend-추상-인터페이스)
4. [LocalStorage / S3Storage 구현체 설계](#4-localstorage--s3storage-구현체-설계)
5. [라이브 녹화의 근본적 난점과 해법](#5-라이브-녹화의-근본적-난점과-해법)
6. [데이터 모델 변경](#6-데이터-모델-변경)
7. [/api/media/{id} 스트리밍 전략](#7-apimediaid-스트리밍-전략)
8. [설정 스키마](#8-설정-스키마)
9. [단계별 실행 계획](#9-단계별-실행-계획)
10. [테스트 전략](#10-테스트-전략)
11. [리스크와 미해결 질문](#11-리스크와-미해결-질문)

---

## 1. 목표와 비목표

### 목표

- **저장 위치를 설정으로 교체 가능하게 만든다.** `ARCHIVER_STORAGE_BACKEND=local`이 기본값이고,
  `s3`로 바꾸면 완료된 녹화물이 오브젝트 스토리지에 저장된다.
- **기존 동작의 완전한 보존.** `local` 백엔드에서는 현재의 디렉터리 레이아웃
  (`{recordings_dir}/{채널명}/{연도}/{월}/{타임스탬프}-{broadcast_id}.mp4`), 썸네일 파일명 규칙,
  Range 응답 바이트가 모두 동일해야 한다. 마이그레이션 없이 기존 배포가 그대로 떠야 한다.
- **외부 프로세스와의 경계를 명확히 한다.** streamlink / ffmpeg / ffprobe / aria2c는 모두
  로컬 파일 디스크립터를 요구한다. 이 제약을 인터페이스 수준에서 "로컬 스테이징" 개념으로 승격시킨다.
- **능력 차이를 코드로 표현한다.** presigned URL을 발급할 수 있는 백엔드와 못 하는 백엔드를
  capability flag로 구분해, 라우터가 `isinstance` 분기 없이 최적 경로를 선택한다.
- **DB가 저장 위치를 잃지 않게 한다.** `Recording.path` 단일 문자열 대신
  `storage_backend` + `storage_key`로 분리해 백엔드 이관·혼재 상태를 표현한다.

### 비목표 (non-goals)

- **라이브 캡처 스트림을 S3로 직접 흘려보내는 것.** streamlink stdout을 S3 multipart로 직결하는
  구조는 5장에서 다루듯 비용·복잡도 대비 이득이 없다. 로컬 스테이징을 유지한다.
- **기존 로컬 파일의 자동 대량 이관.** 백엔드를 `s3`로 바꿔도 이미 존재하는 로컬 녹화물은
  로컬에서 계속 서빙된다(하위 호환). 일괄 이관은 선택적 관리 명령으로 남기고 이 계획의 범위 밖이다.
- **다중 백엔드 동시 쓰기 / 복제.** 한 인스턴스는 한 시점에 하나의 쓰기 백엔드만 갖는다.
  읽기는 레코드별 `storage_backend` 값에 따라 혼재를 허용한다.
- **CDN 연동, 라이프사이클 정책, 스토리지 클래스 최적화(Glacier 등).** 후속 과제.
- **DB 자체의 원격화**(SQLite → Postgres)나 다중 워커 수평 확장. 별개의 작업이다.
- **암호화(SSE-KMS/클라이언트 사이드) 설계.** 8장에 설정 훅만 열어두고 정책은 다루지 않는다.

---

## 2. 현재 로컬 파일시스템 결합 지점

아래는 실제 모듈을 읽고 확인한 결합 지점이다. 각 항목은 "무엇이 왜 로컬 전용인지"를 기준으로 분류했다.

### 2.1 경로 생성 및 partial 재개 — `services/recorder.py`

| 위치 | 결합 내용 |
| --- | --- |
| `_prepare_paths()` | `settings.recordings_dir / safe / 연도 / 월`로 `Path`를 조립하고 `folder.mkdir(parents=True)`를 호출한다. 오브젝트 스토리지에는 "디렉터리 생성"이 없으므로 그대로 옮길 수 없다. |
| `_prepare_paths()` | `folder.glob(f"*-{safe_broadcast_id}.ts")`로 재시작 시 이어받을 partial을 찾고, `max(..., key=lambda p: p.stat().st_size)`로 가장 큰 것을 고른다. glob + stat은 로컬 디렉터리 스캔에 의존한다. |
| `_prepare_paths()` | `temp.with_suffix(".mp4")`로 최종 경로를 파생한다. 키 파생 규칙이 `Path` API에 묶여 있다. |
| `run_recording()` | `rec.path = str(temp)` — 진행 중에는 `.ts` 스테이징 경로가, 완료 후에는 `.mp4` 경로가 같은 컬럼에 들어간다. 6장의 근거. |

### 2.2 캡처 프로세스의 파일 디스크립터 요구 — `services/recorder.py`

- `run_recording()`의 라이브 분기는 `output_handle = temp.open("ab")`를 만들어
  streamlink의 `--stdout`을 **로컬 파일 핸들에 직접 append**한다. 이 핸들은 OS 파일이어야 한다.
- VOD/클립 분기는 ffmpeg에 `"-f", "mpegts", str(temp)`로 **출력 경로 문자열**을 넘긴다.
- 완료 후 remux도 `ffmpeg -i str(temp) ... str(final)`로 **입력과 출력 모두 로컬 경로**를 요구한다.
- `monitor_live_progress()`는 1초 주기로 `path.stat().st_size`를 폴링해 진행률을 계산한다.
  즉 "쓰는 중인 파일의 현재 크기"를 읽을 수 있어야 한다.

### 2.3 다운로더 — `services/downloads.py`

- `download_progressive_aria2()`는 `f"--dir={destination.parent}"`, `f"--out={destination.name}"`로
  aria2c가 **로컬 디렉터리에 직접 쓰게** 한다. 완료 후 `Path(f"{destination}.aria2").unlink()`로
  aria2 컨트롤 파일을 지운다.
- `download_progressive()`는 `destination.stat().st_size`로 오프셋을 구해 `Range: bytes={offset}-`를
  보내고, `destination.open("ab" if offset else "wb")`로 append 재개한다.
  **로컬 파일 크기가 곧 재개 지점**이라는 가정이 박혀 있다.

### 2.4 썸네일 — `services/media.py`

- `thumbnail_path()`는 `video_path.with_suffix(".thumbnail.jpg")` — 썸네일 키가 비디오 경로에서
  순수 문자열 연산으로 파생된다. 이 규칙 자체는 오브젝트 키로도 이식 가능하다.
- `generate_thumbnail()`은 `ffprobe`로 duration을 읽고 `ffmpeg -ss ... -i str(video_path)`로
  중간 프레임을 뽑는다. **원본 비디오가 로컬에 있어야** 한다.
  `temporary.replace(destination)`로 원자적 교체를 하는데, 이는 로컬 파일시스템의 rename 보장에 의존한다.
- `recording_json()`은 `Path(r.path).stat().st_size`로 진행 중 크기를 읽고,
  `thumbnail_path(Path(r.path)).exists()`로 썸네일 URL 노출 여부를 정한다.
  **목록 API가 레코드마다 파일시스템 stat을 때린다** — 원격 백엔드에서는 N회 네트워크 호출이 된다.

### 2.5 HTTP 서빙 — `routers/media.py`

- `media()`는 `Path(rec.path).exists()`로 404를 판정하고, Range 헤더가 없으면
  `FileResponse(path, media_type="video/mp4")`를 반환한다.
- Range가 있으면 `path.stat().st_size`로 끝 오프셋을 clamp하고,
  `path.open("rb")` + `handle.seek(start)` + `handle.read(...)`로 한 청크를 읽어 206을 만든다.
  단일 청크 상한은 1 MiB(`start + 1024 * 1024 - 1`)다.
- `thumbnail()`은 `thumbnail_path(Path(rec.path))`에 `.exists()`를 확인하고 `FileResponse`로 넘긴다.

### 2.6 삭제 — `routers/recordings.py`

- 수동 재요청 경로에서 `rec.path`가 `.ts`가 아니면 `Path(rec.path).unlink(missing_ok=True)`로 지운다.
- 삭제 엔드포인트는 `Path(rec.path).unlink(missing_ok=True)`와
  `thumbnail_path(Path(rec.path)).unlink(missing_ok=True)`를 연달아 호출한다.
- `run_recording()`의 취소/실패 경로도 `temp.unlink()`, `final.unlink()`,
  `Path(f"{temp}.aria2").unlink()`, `thumbnail_path(final).unlink()`로 로컬 삭제를 한다.

### 2.7 용량 통계 — `routers/admin.py`

- `overview()`가 `shutil.disk_usage(settings.recordings_dir)`로 `total/used/percent`를 만든다.
  오브젝트 스토리지에는 "디스크 총량"이라는 개념이 없어 **의미 자체를 재정의**해야 한다.

### 2.8 기동 시 백필 — `lifecycle.py`

- `backfill_thumbnails()`가 완료된 모든 레코드의 `Path(row.path)`에 대해
  `video_path.exists()`와 `thumbnail_path(video_path).exists()`를 확인한 뒤
  `generate_thumbnail`을 돌린다. 전체 스캔 + 로컬 존재 확인 패턴이다.

### 2.9 설정 — `config.py`

- `Settings.recordings_dir: Path = Path("./recordings")`이며, 모듈 임포트 시점에
  `settings.recordings_dir.mkdir(parents=True, exist_ok=True)`가 **무조건 실행**된다.
  S3 전용 배포에서도 빈 디렉터리가 생긴다(스테이징 용도로는 여전히 필요하므로 치명적이진 않다).

---

## 3. StorageBackend 추상 인터페이스

### 3.1 설계 원칙

1. **키는 문자열, 경로는 구현 세부사항.** 인터페이스는 `Path`가 아니라 POSIX 스타일 상대 키
   (`{채널}/{연도}/{월}/{base}.mp4`)를 다룬다. `LocalStorage`가 키를
   `recordings_dir / key`로 해석하고, `S3Storage`는 `prefix + key`로 해석한다.
2. **쓰기 경로를 두 종류로 분리한다.** 라이브 캡처는 "장시간 append", VOD 완료본은
   "이미 존재하는 로컬 파일의 일괄 업로드"다. 하나의 API로 억지로 합치면 양쪽 모두 어색해진다.
3. **외부 프로세스에는 로컬 파일만 준다.** ffmpeg/ffprobe/aria2c가 개입하는 모든 지점은
   `materialize`/`stage` 컨텍스트 매니저를 통과하게 만들어, 원격 백엔드에서도 코드가 동일하게 흐른다.
4. **동기 인터페이스를 기본으로 한다.** 현재 코드가 `asyncio.to_thread`로 블로킹 I/O를 감싸는
   패턴(`generate_thumbnail`, `download_progressive`)을 이미 쓰고 있고, boto3도 동기 API다.
   async 래핑은 호출부(라우터/레코더)에서 `to_thread`로 처리한다.
   단, Range 읽기만은 스트리밍 응답을 위해 **제너레이터**를 반환한다.

### 3.2 능력 플래그 (capabilities)

백엔드마다 할 수 있는 일이 다르다. `isinstance` 분기 대신 선언적 플래그로 노출한다.

```python
# 제안 — backend/app/storage/base.py
from dataclasses import dataclass

@dataclass(frozen=True)
class StorageCapabilities:
    presigned_urls: bool      # 클라이언트를 302로 리다이렉트할 수 있는가
    random_write: bool        # 열린 파일 핸들에 직접 append 가능한가 (로컬만 True)
    cheap_stat: bool          # stat 이 네트워크 왕복 없이 저렴한가 (목록 API 최적화 판단용)
    disk_usage: bool          # shutil.disk_usage 류의 총량/사용량 통계가 의미 있는가
    server_side_copy: bool    # move/rename 이 서버 사이드로 가능한가 (S3 CopyObject)
```

`routers/media.py`는 `if storage.capabilities.presigned_urls: -> 302`,
`routers/admin.py`는 `if not storage.capabilities.disk_usage: -> 집계 쿼리로 대체`처럼 쓴다.

### 3.3 보조 타입

```python
# 제안
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class StorageStat:
    key: str
    size: int
    modified_at: datetime | None
    etag: str | None = None      # S3 무결성 검증 / 로컬은 None
```

### 3.4 핵심 Protocol

```python
# 제안 — 실제 구현이 아니라 계약(contract) 스케치
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

@runtime_checkable
class StorageBackend(Protocol):
    capabilities: StorageCapabilities

    # --- 메타데이터 ---
    def exists(self, key: str) -> bool: ...
    def stat(self, key: str) -> StorageStat | None: ...
    def list_prefix(self, prefix: str, suffix: str | None = None) -> list[StorageStat]: ...
    def total_bytes(self, prefix: str = "") -> int: ...

    # --- 읽기 ---
    def open_range(self, key: str, start: int, end: int, chunk_size: int = 1 << 20) -> Iterator[bytes]:
        """[start, end] 폐구간을 chunk_size 단위로 흘려보낸다. HTTP 206 응답용."""

    def read_all(self, key: str) -> bytes:
        """썸네일처럼 작은 객체 전용. 대용량에는 쓰지 않는다."""

    def presigned_url(self, key: str, expires_in: int = 3600, *, download_name: str | None = None) -> str | None:
        """capabilities.presigned_urls 가 False 면 None."""

    # --- 쓰기 (일괄) ---
    def upload_file(self, local_path: Path, key: str, *, content_type: str | None = None,
                    on_progress: "ProgressHook | None" = None) -> StorageStat:
        """완료된 로컬 파일을 업로드한다. S3 는 내부적으로 multipart 를 쓴다."""

    # --- 쓰기 (스트리밍 append) ---
    def open_append(self, key: str) -> AbstractContextManager[BinaryIO]:
        """append 가능한 바이너리 핸들. capabilities.random_write 가 False 면 NotSupportedError."""

    # --- 로컬 materialize ---
    def materialize(self, key: str) -> AbstractContextManager[Path]:
        """key 를 로컬에서 읽을 수 있는 경로로 만든다.
        LocalStorage: 실제 경로를 그대로 yield (복사 없음).
        S3Storage: 임시 디렉터리에 내려받아 yield 하고, 종료 시 삭제."""

    def stage(self, key: str) -> AbstractContextManager[Path]:
        """key 에 최종 저장될 내용을 로컬에서 '쓰기 위한' 경로를 만든다.
        LocalStorage: 최종 경로 자체 (부모 디렉터리 생성 후 yield, 업로드 없음).
        S3Storage: 스테이징 경로를 yield 하고, 정상 종료 시 upload_file 후 로컬 삭제."""

    # --- 변경/삭제 ---
    def move(self, source_key: str, target_key: str) -> None: ...
    def delete(self, key: str, *, missing_ok: bool = True) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...
```

### 3.5 진행률 훅

업로드 진행률을 `Recording.speed_bps`/`size`에 반영하려면 콜백이 필요하다.
기존 `downloads.update_progress()`와 형태를 맞춘다.

```python
# 제안
from typing import Protocol

class ProgressHook(Protocol):
    def __call__(self, transferred: int, total: int) -> None: ...
```

boto3의 `upload_file(Callback=...)`은 "누적 증가분"을 넘기므로, 어댑터에서 누적합으로 변환한다.

### 3.6 예외 계층

```python
# 제안
class StorageError(RuntimeError): ...
class ObjectNotFound(StorageError): ...
class NotSupportedError(StorageError): ...   # random_write=False 인데 open_append 호출 등
class UploadFailed(StorageError): ...
```

라우터는 `ObjectNotFound`를 404로, `NotSupportedError`를 500(설정 오류)으로 매핑한다.

### 3.7 키 파생 규칙을 인터페이스 밖으로 분리

`_prepare_paths()`의 경로 조립 로직은 저장소와 무관한 **명명 정책**이므로 별도 순수 함수로 뺀다.

```python
# 제안 — backend/app/storage/keys.py (순수 문자열 연산, I/O 없음)
def recording_key(channel_name: str, started_at: datetime, broadcast_id: str, ext: str) -> str:
    """예: "채널명/2026/09/20260903-141500-abc123.mp4" """

def thumbnail_key(video_key: str) -> str:
    """video_key 의 확장자를 ".thumbnail.jpg" 로 교체 — 현재 thumbnail_path() 규칙과 동일."""
```

이렇게 하면 `thumbnail_path()`의 `with_suffix(".thumbnail.jpg")` 규칙이 로컬/S3에서 동일하게 재사용되고,
순수 함수라 단위 테스트가 쉽다.

---

## 4. LocalStorage / S3Storage 구현체 설계

### 4.1 `LocalStorage` — 현재 동작의 무손실 보존

루트는 `settings.recordings_dir`. 키는 루트 기준 상대 POSIX 경로이고,
`_resolve(key)`가 `(root / key).resolve()`를 만든 뒤 **루트 밖으로 탈출했는지 검증**한다
(경로 순회 방어. 채널명이 사용자 데이터에서 오므로 `_sanitize`와 별개로 한 겹 더 둔다).

| 메서드 | 동작 |
| --- | --- |
| `capabilities` | `presigned_urls=False, random_write=True, cheap_stat=True, disk_usage=True, server_side_copy=True` |
| `exists/stat` | `Path.exists()`, `Path.stat()` — 현재 코드와 동일 |
| `open_range` | `path.open("rb")` + `seek(start)` + 청크 루프. 현재는 1 MiB 한 방에 `read`하지만, 제너레이터로 바꿔 큰 Range도 메모리 폭증 없이 처리 |
| `open_append` | `path.open("ab")` 그대로. `run_recording()`의 streamlink 분기가 이걸 쓴다 |
| `upload_file` | 같은 파일시스템이면 `Path.replace()`(원자적 rename), 아니면 `shutil.move` |
| `materialize` | **복사 없이 실제 경로를 yield.** 이게 로컬 성능 회귀를 막는 핵심 |
| `stage` | 부모 디렉터리 `mkdir(parents=True, exist_ok=True)` 후 최종 경로 yield. 업로드 단계 없음 |
| `move` | `Path.replace()` |
| `delete` | `Path.unlink(missing_ok=missing_ok)` — 현재와 동일. 빈 부모 디렉터리 정리는 선택 |
| `list_prefix` | `(root / prefix).glob(f"*{suffix}")` — `_prepare_paths()`의 partial 탐색을 대체 |
| `total_bytes` | `shutil.disk_usage` 기반 통계는 별도 메서드로 유지 (7·8장 참조) |

**중요:** `LocalStorage`가 반환하는 Range 바이트와 헤더는 현재와 **바이트 단위로 동일**해야 한다.
9단계 1에서 골든 테스트로 고정한다.

### 4.2 `S3Storage` — boto3 기반

클라이언트는 `boto3.client("s3", endpoint_url=..., region_name=..., config=Config(...))`로 만든다.
`endpoint_url`을 설정 가능하게 두는 것이 S3 호환 서비스 지원의 전부에 가깝다.

```python
# 제안 — 클라이언트 구성 개요
Config(
    signature_version="s3v4",
    s3={"addressing_style": "virtual"},   # MinIO 는 "path" 필요할 수 있음 → 설정 노출
    retries={"max_attempts": 5, "mode": "standard"},
)
```

#### 메서드별 매핑

| 메서드 | S3 API |
| --- | --- |
| `exists/stat` | `head_object` (404 → `None`). `ContentLength`, `LastModified`, `ETag` |
| `open_range` | `get_object(Range=f"bytes={start}-{end}")` → `response["Body"].iter_chunks(chunk_size)` |
| `read_all` | `get_object()["Body"].read()` — 썸네일 전용 |
| `presigned_url` | `generate_presigned_url("get_object", ExpiresIn=..., Params={"ResponseContentDisposition": ...})` |
| `upload_file` | `upload_file(Config=TransferConfig(multipart_threshold=..., multipart_chunksize=..., max_concurrency=...), Callback=...)` — boto3가 multipart를 자동 처리 |
| `open_append` | `NotSupportedError`. **S3는 append가 불가능하다** (5장) |
| `materialize` | `download_file`로 `tempfile.mkdtemp()`에 내려받고 finally에서 정리 |
| `stage` | 로컬 임시 경로 yield → 정상 종료 시 `upload_file` → 로컬 삭제 |
| `move` | `copy_object(CopySource=...)` + `delete_object`. 5 GiB 초과는 `upload_part_copy` 필요 → boto3 `copy()` 사용 |
| `delete` | `delete_object` (S3는 없는 키 삭제도 200이므로 `missing_ok`가 자연스럽다) |
| `delete_prefix` | `list_objects_v2` 페이지네이션 + `delete_objects` (1000개 배치) |
| `list_prefix` | `list_objects_v2` 페이지네이터 |
| `total_bytes` | `list_objects_v2` 전체 순회 합산 — **비싸다.** 캐시하거나 DB 집계로 대체 |

#### multipart 업로드 파라미터

- `multipart_threshold`: 64 MiB. 이 미만은 단일 PUT.
- `multipart_chunksize`: 16 MiB 기본. S3 파트 상한은 10,000개이므로
  16 MiB × 10,000 = 약 156 GiB까지 커버된다. 장시간 라이브에도 충분하다.
- `max_concurrency`: 4. 업로드가 라이브 캡처 대역폭을 잡아먹지 않도록 보수적으로 둔다.

#### S3 호환 서비스 고려사항

| 서비스 | 고려사항 |
| --- | --- |
| **AWS S3** | 기준 구현. `region_name` 필수, `endpoint_url` 생략 가능 |
| **MinIO** | 자체 호스팅 1순위 타깃. `endpoint_url` 필수, **path-style addressing** 필요(가상 호스트 DNS가 없는 경우가 많다). `region_name`은 `us-east-1` 더미로 둔다 |
| **Cloudflare R2** | `endpoint_url=https://{account_id}.r2.cloudflarestorage.com`, `region_name="auto"`. **egress 무료**라 presigned redirect 전략과 궁합이 가장 좋다. `GetObjectAttributes` 등 일부 API 미지원 → 사용 API를 head/get/put/copy/delete/list로 제한 |
| **Backblaze B2** | S3 호환 엔드포인트(`s3.{region}.backblazeb2.com`) 사용. 멀티파트 최소 파트 크기가 5 MiB이고 일부 응답 헤더가 다르다. presigned URL은 지원 |

**결론:** 위 4개를 모두 지원하려면 (a) `endpoint_url` 노출, (b) `addressing_style` 노출,
(c) 사용 API를 최소 공통 집합으로 제한, (d) `region` 기본값을 유연하게 — 이 네 가지만 지키면 된다.
CI에서는 MinIO를 기준 호환성 타깃으로 삼는다(10장).

### 4.3 팩토리

```python
# 제안 — backend/app/storage/__init__.py
def build_storage(settings: Settings) -> StorageBackend:
    if settings.storage_backend == "s3":
        return S3Storage(...)
    return LocalStorage(settings.recordings_dir)

storage: StorageBackend = build_storage(settings)   # 모듈 레벨 싱글턴
```

FastAPI 의존성(`Depends(get_storage)`)으로도 노출해 테스트에서 override 할 수 있게 한다.
`services/recorder.py`처럼 라우터 밖에서 쓰는 곳은 모듈 싱글턴을 직접 임포트한다.

---

## 5. 라이브 녹화의 근본적 난점과 해법

### 5.1 문제의 핵심

현재 라이브 캡처는 이렇게 동작한다 (`services/recorder.py`의 `run_recording()`):

```
streamlink --stdout  ──파이프──▶  temp.open("ab")  ──▶  {...}.ts (로컬)
                                        ▲
                        monitor_live_progress()가 1초마다 stat().st_size 폴링
```

이 구조가 S3에 직접 안 되는 이유는 세 가지다.

1. **오브젝트 스토리지에 append가 없다.** 객체는 불변이다. 이어 쓰려면 전체를 다시 PUT해야 한다.
2. **multipart로 흉내내도 파트 제약이 걸린다.** 파트는 최소 5 MiB(마지막 파트 제외), 최대 10,000개다.
   방송 길이를 미리 모르는 상태에서 파트 크기를 정해야 하고, 업로드 중 프로세스가 죽으면
   `UploadId`를 잃어버려 미완성 파트가 과금되며 떠돈다.
3. **ffmpeg/ffprobe가 로컬 파일을 요구한다.** 캡처가 끝나면 반드시
   `ffmpeg -i temp -c copy -movflags +faststart final`(remux)과
   `ffprobe`(duration) + `ffmpeg -ss`(썸네일 추출)이 돌아간다.
   `+faststart`는 moov atom을 파일 앞으로 옮기려고 **출력 파일 전체를 다시 쓴다** — 랜덤 시크가 필수다.
   S3를 원본으로 직접 다루는 건 사실상 불가능하다.

### 5.2 채택 전략: 로컬 스테이징 → 완료 후 업로드

```
[1] 캡처      streamlink/ffmpeg/aria2c  ──▶  {staging_dir}/{base}.ts     (항상 로컬)
[2] remux     ffmpeg -c copy +faststart ──▶  {staging_dir}/{base}.mp4    (항상 로컬)
[3] 썸네일    ffprobe + ffmpeg -ss      ──▶  {staging_dir}/{base}.thumbnail.jpg
[4] 업로드    storage.upload_file()     ──▶  s3://bucket/{prefix}/{key}  (mp4 + jpg)
[5] 정리      로컬 스테이징 삭제 + DB 커밋 (storage_backend/storage_key 갱신)
```

**단계 1~3은 백엔드와 무관하게 완전히 동일하다.** 즉 `recorder.py`의 캡처·remux·썸네일 로직은
거의 손대지 않는다. 변경은 (a) 스테이징 루트를 `recordings_dir`가 아닌
`ARCHIVER_STAGING_DIR`(기본값은 `recordings_dir`)에서 가져오는 것, (b) 4~5단계 추가뿐이다.

`LocalStorage`에서는 스테이징 디렉터리 == 최종 디렉터리이므로 4단계가 `Path.replace()`(또는 no-op)로
축약되고, 현재 동작이 그대로 보존된다.

### 5.3 상태 모델 확장

업로드는 실패할 수 있고 시간이 걸리므로, 지금의 `recording → completed` 직행으로는 부족하다.

| 상태 | 의미 |
| --- | --- |
| `recording` | 캡처 진행 중 (로컬 `.ts`) |
| `processing` | **신규.** remux/썸네일 중 (로컬) |
| `uploading` | **신규.** 원격 업로드 중. `size`/`speed_bps`에 업로드 진행률 반영 |
| `completed` | 최종 위치에 존재. `storage_backend`/`storage_key` 확정 |
| `failed` | 실패. 스테이징 파일은 보존해 재시도 가능하게 |

`local` 백엔드에서는 `uploading`이 즉시 지나가므로 UI 영향이 사실상 없다.
`processing`/`uploading`도 `lifecycle.requeue_interrupted()`의 재큐 대상에 포함시켜야 한다
(현재는 `["recording", "interrupted"]`만 본다).

### 5.4 재시작 시 partial resume

현재 `_prepare_paths()`는 `folder.glob(f"*-{safe_broadcast_id}.ts")`로 가장 큰 partial을 찾아 이어받는다.
**이 로직은 스테이징 디렉터리에 그대로 유지된다.** partial은 항상 로컬이므로 원격 백엔드와 무관하다.

다만 두 가지를 보강한다.

- **스테이징 디렉터리는 절대 원격이 되지 않는다**는 불변식을 명시한다.
  즉 `_prepare_paths()`는 `StorageBackend`를 쓰지 않고 로컬 `Path`를 계속 다룬다.
  이렇게 하면 aria2c의 `--dir`/`--out`과 `download_progressive()`의
  `stat().st_size` 기반 Range 재개 로직을 **전혀 수정하지 않아도 된다.**
- S3 모드에서는 업로드 성공 후에만 스테이징을 지우므로, 업로드 중 죽으면 `.mp4`가 로컬에 남아 있다.
  재기동 시 `processing`/`uploading` 상태 레코드는 캡처를 다시 하지 않고
  **로컬 스테이징 파일 존재를 먼저 확인해 업로드만 재시도**한다. 이게 재개의 핵심이다.

### 5.5 multipart upload 재개

세 가지 선택지를 검토했다.

| 방안 | 평가 |
| --- | --- |
| A. `UploadId`/파트 ETag를 DB에 저장하고 정확히 재개 | 이론적으로 최선이지만 파트 상태 테이블·정합성 관리 비용이 크다. 업로드 시간이 분 단위인 이 워크로드에는 과설계 |
| B. **처음부터 다시 업로드 (채택)** | 스테이징 `.mp4`가 로컬에 온전히 남아 있으므로 재업로드가 항상 가능하다. 구현이 단순하고 실패 모드가 하나뿐 |
| C. 업로드 중 스테이징 삭제 후 원격에서 재개 | 원본이 사라져 복구 불가. 배제 |

**방안 B를 채택한다.** 대신 고아 파트 누수를 막기 위해:

- 업로드 실패 시 boto3의 `abort_multipart_upload`를 명시적으로 호출한다
  (boto3 `upload_file`은 예외 시 자동 abort를 시도하지만 프로세스가 죽으면 못 한다).
- **버킷 라이프사이클 규칙으로 `AbortIncompleteMultipartUpload: DaysAfterInitiation=1`을 설정**하는 것을
  운영 필수 사항으로 문서화한다. 프로세스 강제 종료로 생긴 고아 파트를 스토리지가 알아서 청소해준다.
  (R2/B2도 유사 기능을 제공하나 명칭이 다르므로 8장 주석으로 남긴다.)
- 재업로드 시 키가 동일하므로 최종 상태는 멱등하다.

### 5.6 업로드 진행률 표시

`upload_file(Callback=...)`이 넘기는 증가분을 누적해 `downloads.update_progress()`와 같은 방식으로
`Recording.size`/`speed_bps`를 갱신한다. 단 `state == "uploading"`일 때만 반영하도록
`update_progress`의 상태 가드(`recording.state == "recording"`)를 확장해야 한다.

`recording_json()`의 `progress` 계산은 `size/total_size`이므로,
업로드 중에는 `total_size`를 최종 파일 크기로 미리 채워두면 진행률이 자연스럽게 나온다.

---

## 6. 데이터 모델 변경

### 6.1 `Recording.path` 만으로 부족한 이유

현재 `models.py`의 `Recording.path`는 `TextField(null=True)`이며, 코드를 보면 **네 가지 서로 다른 의미**를
같은 컬럼에 담고 있다.

1. **진행 중 스테이징 경로** — `run_recording()`이 `rec.path = str(temp)`로 `.ts` 경로를 넣는다.
2. **완료된 최종 경로** — 성공 시 `rec.path = str(final)`로 `.mp4` 경로로 덮어쓴다.
3. **실패 시 잔존 파일 경로** — 실패 핸들러가 `rec.path = str(temp) if temp.exists() else None`.
4. **부재(None)** — 취소 시 `rec.path = None`.

여기서 파생되는 구체적 문제:

- **확장자로 상태를 추론한다.** `routers/recordings.py`가 `rec.path.endswith(".ts")`로
  "스테이징인가 완료본인가"를 판단한다. 상태가 문자열 접미사에 인코딩되어 있어 취약하다.
- **저장 위치 정보가 없다.** 값이 로컬 절대경로 문자열이라 S3 객체를 표현할 수 없다.
  `s3://bucket/key` 같은 URI를 넣으면 `Path(rec.path)`를 호출하는 모든 코드가 조용히 오동작한다.
- **혼재를 표현할 수 없다.** 백엔드를 `s3`로 바꾼 뒤에도 기존 레코드는 로컬에 남는다.
  레코드별로 "어디에 있는지"를 알아야 읽기 경로를 고를 수 있다.
- **절대경로가 이식성을 깬다.** `recordings_dir`가 바뀌거나 컨테이너 마운트 경로가 달라지면
  저장된 절대경로가 전부 무효가 된다.
- **썸네일 존재 여부가 파일시스템 조회에 묶인다.** `recording_json()`이 레코드마다
  `thumbnail_path(Path(r.path)).exists()`를 호출한다. 원격에서는 목록 API가 N번 왕복한다.

### 6.2 제안 스키마

`Recording`에 컬럼 4개를 추가한다. 모두 nullable 또는 기본값이 있어 **추가만으로 하위 호환**이 된다.

```python
# 제안 — models.py 의 Recording 에 추가될 필드 (구현 아님)
class Recording(BaseModel):
    # ... 기존 필드 유지 ...
    path = TextField(null=True)                                    # 유지 (deprecated, 6.4 참조)
    storage_backend = CharField(max_length=16, null=True)          # "local" | "s3"
    storage_key = TextField(null=True)                             # 루트/프리픽스 기준 상대 키
    thumbnail_key = TextField(null=True)                           # 썸네일 존재 여부 + 위치
    staging_path = TextField(null=True)                            # 진행 중 로컬 .ts 절대경로
```

역할 분리가 핵심이다.

| 컬럼 | 의미 | 생애주기 |
| --- | --- | --- |
| `staging_path` | **로컬 전용** 작업 파일 경로 (`.ts`, 업로드 대기 `.mp4`) | `recording`~`uploading` 중 유효, 완료 시 `None` |
| `storage_backend` | 완료본이 사는 백엔드 | `completed` 시 확정, 이후 불변(이관 시에만 변경) |
| `storage_key` | 백엔드 루트 기준 **상대** 키 | `completed` 시 확정 |
| `thumbnail_key` | 썸네일 키 (없으면 `None`) | 썸네일 생성 성공 시 설정 |

이렇게 하면:

- `endswith(".ts")` 추론이 사라진다 → `staging_path is not None`으로 명시적 판정.
- `recording_json()`의 썸네일 분기가 `r.thumbnail_key is not None`이 되어 **파일시스템 조회가 0회**가 된다.
  이건 로컬 배포에서도 목록 API 성능을 개선한다.
- 상대 키를 쓰므로 `recordings_dir` 변경/재마운트에 영향받지 않는다.

### 6.3 Peewee 마이그레이션 전략

프로젝트는 이미 `schema_migrations.py`에 **기동 시 additive 마이그레이션** 패턴을 갖고 있다:
`database.create_tables(ALL_MODELS)` → `PRAGMA table_info(recordings)`로 기존 컬럼 조회 →
없는 컬럼만 `ALTER TABLE ... ADD COLUMN` → 데이터 백필 `UPDATE`.

**이 기존 패턴을 확장하는 것을 권한다.** 새 프레임워크를 도입하지 않는 이유:

- 추가하려는 변경이 전부 **컬럼 추가 + 백필**이라 `playhouse.migrate`의 표현력이 필요 없다.
- `schema_migrations.py`의 `RECORDING_COLUMNS` 딕셔너리에 항목 4개를 더하면 끝이다.
- 버전 테이블/마이그레이션 파일 관리 오버헤드가 없다. 현재 규모에 적합하다.

```python
# 제안 — schema_migrations.py 의 RECORDING_COLUMNS 확장
RECORDING_COLUMNS = {
    # ... 기존 4개 유지 ...
    "storage_backend": "VARCHAR(16)",
    "storage_key": "TEXT",
    "thumbnail_key": "TEXT",
    "staging_path": "TEXT",
}
```

다만 `playhouse.migrate`를 쓸 여지도 남긴다. **인덱스 추가나 컬럼 삭제(6.4의 `path` 제거)**가 필요해지면
SQLite의 제한 때문에 raw DDL이 번거로워지므로, 그 시점에
`playhouse.migrate.SqliteMigrator` + `migrate(migrator.add_column(...), migrator.drop_column(...))`로
전환한다. 즉 **1차는 기존 방식, `path` 제거 단계에서만 `playhouse.migrate` 도입**이 결론이다.

### 6.4 기존 데이터 백필 (하위 호환)

기동 시 한 번 도는 백필을 추가한다. 순수 SQL로 표현 가능하다.

```sql
-- 제안 — 완료된 기존 레코드는 모두 로컬에 있다
UPDATE recordings SET storage_backend = 'local'
 WHERE storage_backend IS NULL AND path IS NOT NULL;
```

`storage_key`는 절대경로 → 상대 키 변환이 필요해 SQL만으로는 부족하다.
파이썬 백필 잡(`lifecycle.py`의 `backfill_thumbnails()`와 같은 자리)에서:

1. `path`가 `recordings_dir` 하위이면 `Path(path).relative_to(recordings_dir).as_posix()`를 `storage_key`로.
2. 하위가 아니면(외부 경로) `storage_key`를 비우고 `storage_backend='local_absolute'`로 표시해
   `path`를 그대로 쓰는 레거시 경로를 유지한다.
3. `thumbnail_path(Path(path)).exists()`이면 `thumbnail_key`를 채운다.
   이 백필은 `backfill_thumbnails()`가 이미 전체 스캔을 하므로 **같은 루프에 합치면 추가 비용이 없다.**

**읽기 측 폴백 순서**를 명시해 무중단을 보장한다:

```
storage_key 있음  ──▶ storage.open_range(storage_key)
없고 path 있음    ──▶ 레거시 로컬 경로로 직접 서빙 (현재 코드 경로)
둘 다 없음        ──▶ 404
```

이 폴백이 있으면 마이그레이션 실패나 부분 백필 상태에서도 서비스가 죽지 않는다.
`path` 컬럼은 최소 한 릴리스 동안 **쓰기도 병행**(dual-write)한 뒤, 안정화 후 제거를 검토한다.

---

## 7. `/api/media/{id}` 스트리밍 전략

### 7.1 현재 구현의 재확인

`routers/media.py`의 `media()`는 Range 헤더가 없으면 `FileResponse`, 있으면
`start`를 파싱해 `min(end, size-1)`로 clamp하고 최대 1 MiB를 읽어 206을 반환한다.
브라우저 `<video>`는 이 1 MiB 청크를 반복 요청하며 재생한다
(`web/src/main.tsx`가 `src={\`/api/media/${recording.id}\`}`로 직접 참조).

인증은 `Depends(current_user)`이고, 프런트는 `credentials: "include"`로 **쿠키 세션**을 쓴다.
`entitled()`가 `Entitlement`를 확인해 권한을 검사한다. 이 권한 모델이 7.3의 핵심 제약이다.

### 7.2 백엔드별 분기

```
GET /api/media/{id}
  │
  ├─ 인증 + entitled() 권한 검사            (백엔드 무관, 항상 수행)
  ├─ state != "completed" ──▶ 409
  │
  ├─ capabilities.presigned_urls == False   (LocalStorage)
  │     └─▶ 서버 프록시: storage.open_range() 로 206 / FileResponse
  │
  └─ capabilities.presigned_urls == True    (S3Storage)
        ├─ ARCHIVER_S3_PRESIGNED_REDIRECT=true  ──▶ 302 + Location: presigned URL
        └─ false                                 ──▶ 서버 프록시 (get_object Range 중계)
```

즉 S3에서도 **프록시가 기본, presigned redirect는 옵트인**으로 둔다. 이유는 7.3에 있다.

### 7.3 presigned redirect vs 서버 프록시 트레이드오프

| 항목 | presigned redirect (302) | 서버 프록시 스트리밍 |
| --- | --- | --- |
| **대역폭** | 앱 서버를 거치지 않음. R2는 egress 무료라 최적. 홈서버 업링크를 아낀다 | 모든 바이트가 앱 서버를 통과. S3 egress + 서버 업링크 **이중 비용** |
| **지연** | 첫 요청에 302 왕복이 추가되지만 이후는 스토리지 직결 | 왕복은 없지만 서버가 병목 |
| **CPU/메모리** | 거의 0 | 청크마다 앱 워커 점유. 동시 시청자에 선형 증가 |
| **인증 정밀도** | URL을 아는 누구나 만료까지 접근 가능. `entitled()` 검사가 **발급 시점 1회**로 약화 | 매 Range 요청마다 권한 재검증 |
| **쿠키 인증과의 충돌** | 302 후 요청은 **다른 오리진**으로 가므로 `archiver_session` 쿠키가 전송되지 않는다. 서명이 인증을 대체해야 한다 | 동일 오리진 유지. 쿠키가 정상 동작 |
| **CORS** | `<video src>`의 단순 GET은 302 추적에 CORS 프리플라이트가 없어 대체로 동작. 다만 `crossorigin` 속성이나 fetch 기반 플레이어를 쓰면 **버킷 CORS 설정(`Access-Control-Allow-Origin`, `Range` 허용, `Content-Range` expose)이 필수** | CORS 이슈 없음 |
| **URL 유출** | 브라우저 히스토리/개발자도구/Referer에 서명 URL이 남는다. 만료를 짧게(예: 300초) 해야 하지만, 너무 짧으면 장시간 재생 중 만료된다 | 유출 표면 없음 |
| **취소/삭제 반영** | 발급된 URL은 즉시 무효화할 수 없다 | 즉시 반영 |
| **감사 로그** | 앱이 실제 시청을 관측하지 못한다 | 요청 단위 관측 가능 |

### 7.4 권고

- **기본값은 서버 프록시.** 권한 모델(`Entitlement` 기반 사적 아카이브)이 요구하는 보안 수준을 지키고,
  쿠키 인증·CORS·즉시 삭제 반영이 모두 자연스럽게 유지된다.
  현재 코드가 이미 이 형태이므로 회귀 위험도 가장 낮다.
- **presigned redirect는 명시적 옵트인.** 대역폭이 실제 병목이 되는 배포(특히 R2)를 위한 탈출구로 제공한다.
  켤 때는 (a) 만료를 짧게(300~900초), (b) `ResponseContentDisposition`으로 파일명 지정,
  (c) 버킷 CORS에 `Range` 허용 및 `Content-Range`/`Accept-Ranges` expose, 를 함께 문서화한다.
- **장시간 재생 중 만료 문제**는 플레이어가 새 Range 요청 시 다시 `/api/media/{id}`를 호출하면
  새 302를 받으므로 대체로 자연 해결된다. 다만 만료된 URL로 재시도하는 플레이어가 있어
  옵트인 유지 근거를 하나 더 제공한다.
- **프록시 개선 사항:** 현재 `open_range`가 1 MiB를 한 번에 `read`하는데,
  제너레이터 청크 스트리밍으로 바꿔 큰 Range 요청에서도 메모리 사용이 일정하게 유지되도록 한다.
  또한 Range 파싱이 `bytes=0-` 이외의 형식(멀티 Range, suffix range `bytes=-500`)에서 예외를 던지므로,
  이 리팩터링 기회에 `416 Range Not Satisfiable` 처리를 추가한다.
- **썸네일**은 작고 캐시 가능(`Cache-Control: private, max-age=86400`)하므로
  S3에서도 `read_all()` 프록시로 충분하다. presigned를 쓸 이유가 없다.

---

## 8. 설정 스키마

### 8.1 `Settings` 추가 필드

`config.py`의 `Settings`는 `env_prefix="ARCHIVER_"`를 쓰므로 필드명이 그대로 환경변수가 된다.

```python
# 제안 — config.py 의 Settings 에 추가될 필드 (구현 아님)
storage_backend: Literal["local", "s3"] = "local"
staging_dir: Path | None = None          # None 이면 recordings_dir 사용

s3_bucket: str | None = None
s3_prefix: str = ""
s3_endpoint_url: str | None = None       # MinIO / R2 / B2 용. AWS 는 생략
s3_region: str = "us-east-1"
s3_access_key_id: str | None = None
s3_secret_access_key: str | None = None
s3_addressing_style: Literal["auto", "virtual", "path"] = "auto"
s3_presigned_redirect: bool = False
s3_presigned_expires: int = 900
s3_multipart_threshold_mb: int = 64
s3_multipart_chunksize_mb: int = 16
s3_max_concurrency: int = 4
```

**검증 규칙:** `storage_backend == "s3"`인데 `s3_bucket`이 비어 있으면
기동 시 명확한 한국어 에러로 실패해야 한다(pydantic model validator).
자격증명은 미지정 시 boto3 기본 체인(환경변수, `~/.aws`, IAM 역할)으로 폴백하도록
**비워둘 수 있게** 유지한다 — 컨테이너/EC2 배포에서 유용하다.

### 8.2 `.env.example` 추가 항목

```dotenv
# ── 저장소 백엔드 ──────────────────────────────────────────────
# local (기본, 현재 동작 그대로) 또는 s3
ARCHIVER_STORAGE_BACKEND=local

# 라이브 캡처/remux/썸네일 생성용 로컬 작업 디렉터리.
# 비워두면 ARCHIVER_RECORDINGS_DIR 를 사용한다.
# s3 백엔드에서도 반드시 로컬 디스크가 필요하다 (방송 1회분을 담을 여유 공간 필요).
ARCHIVER_STAGING_DIR=

# ── S3 (ARCHIVER_STORAGE_BACKEND=s3 일 때만) ──────────────────
ARCHIVER_S3_BUCKET=
# 버킷 내 키 프리픽스. 예: recordings/
ARCHIVER_S3_PREFIX=
# AWS S3 는 비워둔다. MinIO: http://minio:9000
# Cloudflare R2: https://<account_id>.r2.cloudflarestorage.com
# Backblaze B2: https://s3.<region>.backblazeb2.com
ARCHIVER_S3_ENDPOINT_URL=
# AWS: 실제 리전 / R2: auto / MinIO: us-east-1 (더미)
ARCHIVER_S3_REGION=us-east-1
# 비워두면 boto3 기본 자격증명 체인(IAM 역할 등)을 사용한다
ARCHIVER_S3_ACCESS_KEY_ID=
ARCHIVER_S3_SECRET_ACCESS_KEY=
# MinIO 등 가상 호스트 DNS 가 없는 환경은 path 로 설정
ARCHIVER_S3_ADDRESSING_STYLE=auto

# true 면 /api/media/{id} 가 presigned URL 로 302 리다이렉트한다.
# 대역폭을 절약하지만 요청별 권한 재검증이 사라지고 버킷 CORS 설정이 필요하다.
ARCHIVER_S3_PRESIGNED_REDIRECT=false
ARCHIVER_S3_PRESIGNED_EXPIRES=900

# 업로드 튜닝 (기본값 권장)
ARCHIVER_S3_MULTIPART_THRESHOLD_MB=64
ARCHIVER_S3_MULTIPART_CHUNKSIZE_MB=16
ARCHIVER_S3_MAX_CONCURRENCY=4
```

### 8.3 운영 주의사항 (문서화 대상)

- **버킷 라이프사이클에 `AbortIncompleteMultipartUpload`(1일)를 반드시 설정한다.**
  5.5절의 고아 파트 누수 방지책이다. R2/B2는 동등 기능의 명칭이 다르니 각 콘솔 기준으로 안내한다.
- `docker-compose.yml`의 `./recordings:/recordings` 마운트는 **S3 모드에서도 유지**해야 한다
  (스테이징용). 볼륨을 빼면 캡처가 실패한다.
- `/api/admin/overview`의 디스크 통계 의미가 바뀐다(9단계 5).

---

## 9. 단계별 실행 계획

각 단계는 **독립적으로 머지 가능**하고, 머지 직후 `local` 기본값에서 동작이 변하지 않아야 한다.

### 단계 0 — 회귀 방어선 구축

- 지금의 `/api/media/{id}` 동작을 고정하는 테스트를 먼저 추가한다:
  Range 없음(200 + `Accept-Ranges`), `bytes=0-`, 중간 오프셋, 파일 끝 clamp, 썸네일 200/404.
- **검증:** 코드 변경 없이 새 테스트가 통과. 이후 모든 단계에서 이 테스트가 계속 통과해야 한다.

### 단계 1 — 키 파생 순수 함수 분리

- `storage/keys.py`에 `recording_key()`/`thumbnail_key()`를 만들고,
  `_prepare_paths()`와 `thumbnail_path()`가 이 함수를 쓰도록 리팩터링.
- 저장소 추상화는 아직 없다. 순수한 코드 정리다.
- **검증:** 기존 전체 테스트 통과 + 키 생성 단위 테스트(채널명 sanitize, 연/월 구분, 확장자 교체).
  실제 생성되는 경로 문자열이 리팩터링 전과 **동일**함을 확인.

### 단계 2 — `StorageBackend` 인터페이스 + `LocalStorage`

- `storage/base.py`(Protocol, capabilities, 예외), `storage/local.py`, `storage/__init__.py`(팩토리).
- `ARCHIVER_STORAGE_BACKEND` 설정 추가 (`local`만 유효).
- 아직 어떤 호출부도 바꾸지 않는다. 순수 추가.
- **검증:** `LocalStorage` 자체 단위 테스트(tmp_path 기반) — round-trip 쓰기/읽기,
  `open_range` 경계값, `materialize`가 복사 없이 원본 경로를 주는지, 경로 순회 방어.

### 단계 3 — 읽기 경로를 추상화로 전환

- `routers/media.py`가 `storage.open_range()`/`stat()`/`read_all()`을 쓰도록 변경.
- `services/media.py`의 `recording_json()`도 `storage.stat()` 경유로 변경.
- 여전히 `local`만 존재하므로 외부 동작은 불변.
- **검증:** 단계 0의 골든 테스트가 **바이트 단위로** 그대로 통과. 응답 헤더 동일성 확인.

### 단계 4 — 데이터 모델 확장 + 백필

- `storage_backend`/`storage_key`/`thumbnail_key`/`staging_path` 컬럼 추가
  (`schema_migrations.py`의 `RECORDING_COLUMNS` 확장).
- 기동 백필로 기존 완료 레코드에 `local` + 상대 키 + 썸네일 키를 채운다.
- 쓰기 측은 `path`와 새 컬럼을 **동시에 갱신**(dual-write).
- 읽기 측은 6.4의 폴백 순서를 구현.
- `routers/recordings.py`의 `endswith(".ts")` 판정을 `staging_path` 기반으로 교체.
- **검증:** 기존 DB 파일을 복사해 마이그레이션을 돌려 컬럼 추가/백필 확인.
  `storage_key`가 NULL인 레거시 레코드도 정상 재생되는지(폴백) 테스트.

### 단계 5 — 쓰기 경로에 `stage()` 도입 + 상태 확장

- `run_recording()`이 스테이징 디렉터리를 `staging_dir`에서 가져오고,
  remux/썸네일 후 `storage.stage()`/`upload_file()`을 통과하도록 변경.
- `processing`/`uploading` 상태 추가, `requeue_interrupted()`의 재큐 대상 확장,
  `update_progress()`의 상태 가드 확장.
- 삭제 경로(`routers/recordings.py`, 취소/실패 핸들러)를 `storage.delete()`로 전환.
- `/api/admin/overview`를 `capabilities.disk_usage` 분기로 변경
  (원격이면 `SUM(size)` 집계 + 스테이징 디스크 여유 공간을 별도 필드로 노출).
- **검증:** `local`에서 라이브/VOD 캡처 E2E가 기존과 동일하게 완료되고,
  파일이 **정확히 같은 경로**에 생기는지 확인. 취소/실패 시 잔존 파일 정리 확인.
  재시작 partial resume(`.ts` glob)이 여전히 동작하는지 확인.

### 단계 6 — `S3Storage` 구현

- `storage/s3.py` + boto3 의존성 추가(`pyproject.toml`), 8장 설정 필드 전체 추가.
- 팩토리에서 `s3` 분기 활성화. `open_append`는 `NotSupportedError`.
- **검증:** 10장의 MinIO 기반 통합 테스트. `local`/`s3` 양쪽에 대해
  동일한 계약 테스트 스위트(parametrize)를 돌려 인터페이스 준수를 확인.

### 단계 7 — presigned redirect 옵트인

- `ARCHIVER_S3_PRESIGNED_REDIRECT` 처리, `/api/media/{id}`의 302 분기.
- 버킷 CORS 및 만료 설정 문서화.
- **검증:** 플래그 off에서 프록시 동작 불변, on에서 302 + `Location`에 서명 파라미터 존재.
  실제 브라우저에서 시크(seek) 동작 수동 확인.

### 단계 8 — 정리 (선택)

- dual-write 종료, `path` 컬럼 제거 검토.
- 이 시점에 `playhouse.migrate.SqliteMigrator`의 `drop_column`을 도입한다(6.3).
- 로컬 → S3 일괄 이관 관리 명령(별도 과제).
- **검증:** `path` 없이 전체 테스트 통과, 기존 배포 업그레이드 리허설.

---

## 10. 테스트 전략

### 10.1 현재 테스트 환경

테스트는 `backend/tests/test_app.py` 한 파일이고, `pyproject.toml`에
`pytest` + `pytest-asyncio`만 test extra로 잡혀 있다. 외부 서비스 의존이 전무하다.
이 "의존성 가벼움"을 최대한 지키는 방향으로 결정한다.

### 10.2 moto vs MinIO 컨테이너

| 기준 | moto (인프로세스 목) | MinIO (실제 컨테이너) |
| --- | --- | --- |
| 실행 속도 | 매우 빠름 (수십 ms) | 컨테이너 기동 수 초 |
| CI 요구사항 | pip 설치만 | Docker 필요 |
| 로컬 개발자 경험 | `pytest` 한 번으로 끝 | Docker 없으면 스킵 |
| multipart 재현도 | 구현되어 있으나 파트 크기 제약 등 실제와 미묘한 차이 | 실제 동작 |
| presigned URL | 서명 생성은 되지만 실제 HTTP 소비 검증은 제한적 | 실제 HTTP GET으로 검증 가능 |
| `endpoint_url`/addressing style 호환성 | **검증 불가** (moto 자체가 목이라 R2/MinIO 특성을 안 드러냄) | path-style 등 실제 검증 가능 |
| Range GET 정확성 | 대체로 정확 | 실제 |

### 10.3 권고: **둘 다 쓰되 역할을 분리한다**

단일 선택을 강요하면 어느 쪽이든 사각지대가 생긴다. 계층을 나눈다.

**(1) 기본 계층 — moto (`moto[s3]`를 test extra에 추가)**

- 항상 실행된다. `S3Storage`의 **로직** 검증을 담당한다:
  키 조립, 프리픽스 처리, `ObjectNotFound` 매핑, `open_range` 경계값,
  `delete_prefix` 페이지네이션, `upload_file` 진행률 콜백 누적, `materialize`/`stage` 정리 동작.
- 근거: 이 프로젝트의 S3 사용 범위는 head/get/put/copy/delete/list라는 **좁은 최소 공통 집합**이고,
  moto의 재현도가 충분히 높은 영역이다. Docker 없이 전체 스위트가 도는 현재 개발 경험을 지킬 수 있다.

**(2) 통합 계층 — MinIO 컨테이너 (opt-in 마커)**

- `@pytest.mark.integration` + `ARCHIVER_TEST_S3_ENDPOINT` 환경변수가 있을 때만 실행.
  없으면 `pytest.skip`. CI에서는 서비스 컨테이너로 띄워 별도 잡으로 돌린다.
- 담당: multipart 실제 업로드(임계값 초과 파일), presigned URL을 **실제 HTTP로 GET**해서
  바이트가 일치하는지, path-style addressing, 5 GiB 근처가 아닌 현실적 대용량(수백 MiB) 경로.
- 근거: 4.2에서 정리한 S3 호환 서비스 차이(addressing style, 엔드포인트)는
  **moto로는 원리적으로 검증할 수 없다.** MinIO를 기준 호환성 타깃으로 삼으면
  자체 호스팅 사용자(이 프로젝트의 주 사용자층)의 실제 환경을 가장 잘 대변한다.

### 10.4 계약 테스트 (가장 중요)

`StorageBackend`를 구현하는 모든 백엔드에 **같은 테스트를 parametrize로 돌린다.**

```python
# 제안 — 개념 스케치
@pytest.fixture(params=["local", "s3_moto"])
def storage(request, tmp_path): ...

# 이 스위트가 두 백엔드 모두에서 통과해야 한다
def test_upload_then_range_read_roundtrip(storage): ...
def test_stat_missing_returns_none(storage): ...
def test_delete_is_idempotent(storage): ...
def test_materialize_yields_readable_local_path(storage): ...
def test_move_preserves_bytes(storage): ...
```

이게 있으면 `LocalStorage`와 `S3Storage`의 **의미론적 동등성**이 보장되고,
세 번째 백엔드를 추가할 때도 비용이 낮다.

### 10.5 그 밖의 테스트

- **골든 회귀 테스트(단계 0):** `/api/media/{id}` 응답 바이트/헤더 고정. 리팩터링 안전망의 핵심.
- **마이그레이션 테스트:** 구 스키마 SQLite 픽스처 → `migrate()` → 컬럼/백필 검증.
  `storage_key`가 NULL인 레거시 레코드의 폴백 재생도 함께 확인.
- **ffmpeg/ffprobe는 목으로 대체.** 캡처·remux 경로 테스트에서 실제 인코더를 돌리지 않는다
  (현재 테스트도 외부 바이너리에 의존하지 않는 구조를 유지).
- **재개 로직 테스트:** `staging_path`에 파일이 있는 `uploading` 상태 레코드가
  재기동 시 캡처를 다시 하지 않고 업로드만 재시도하는지(5.4).

---

## 11. 리스크와 미해결 질문

### 11.1 리스크

| # | 리스크 | 영향 | 완화 |
| --- | --- | --- | --- |
| R1 | **로컬 동작 회귀.** 리팩터링 범위가 캡처·서빙·삭제 전 구간에 걸친다 | 높음 | 단계 0의 골든 테스트를 먼저 세우고, `LocalStorage.materialize()`가 복사 없이 원본 경로를 주도록 해 성능·의미를 모두 보존. 단계별 독립 머지 |
| R2 | **스테이징 디스크 고갈.** S3 모드에서도 방송 1회분(`.ts` + remux된 `.mp4`)이 로컬에 동시 존재한다 | 높음 | remux는 원본과 결과가 함께 있어야 하므로 **최대 파일 크기의 약 2배** 여유가 필요함을 문서화. 업로드 성공 즉시 스테이징 삭제. `max_recordings`(기본 2)와 곱해 산정 |
| R3 | **목록 API의 원격 stat 폭증.** `recording_json()`이 레코드마다 stat/exists를 호출한다 | 중간 | 단계 4에서 `thumbnail_key` 컬럼으로 전환해 파일시스템/네트워크 조회를 0회로 만든다. 진행 중 크기는 `staging_path`(항상 로컬)로 읽는다 |
| R4 | **고아 multipart 파트 과금.** 프로세스 강제 종료 시 `UploadId`가 유실된다 | 중간 | 명시적 `abort_multipart_upload` + 버킷 라이프사이클 규칙(1일)을 운영 필수로 문서화 |
| R5 | **presigned URL 유출.** 서명 URL이 히스토리/로그/Referer에 남는다 | 중간 | 기본 off(옵트인), 짧은 만료(기본 900초), 프록시를 기본 경로로 유지 |
| R6 | **S3 호환 서비스 간 미묘한 차이.** R2의 미지원 API, B2의 파트 크기 제약 등 | 중간 | 사용 API를 최소 공통 집합으로 제한. MinIO 통합 테스트를 기준 타깃으로 삼고, 서비스별 주의사항을 `.env.example` 주석에 명시 |
| R7 | **업로드 지연으로 인한 사용자 혼란.** 캡처 종료 후에도 재생이 안 되는 구간이 생긴다 | 낮음 | `uploading` 상태와 진행률을 UI에 노출. `local`에서는 이 구간이 사실상 0 |
| R8 | **디스크 사용량 통계의 의미 변화.** `shutil.disk_usage`가 S3에서 무의미 | 낮음 | `capabilities.disk_usage` 분기. 원격은 `SUM(size)` 집계 + 스테이징 여유 공간을 별도 필드로 |
| R9 | **경로 순회.** 채널명이 사용자 제어 데이터이고 키 조립에 쓰인다 | 낮음 | 기존 `_sanitize`(`[^\w.-]+` → `_`) 유지 + `LocalStorage._resolve()`에서 루트 이탈 재검증 |
| R10 | **동시 접근.** 다중 워커/인스턴스가 같은 키에 쓰면 충돌 | 낮음 | 현재는 단일 프로세스 + `recording_semaphore` 전제. 이 전제를 문서에 명시하고 수평 확장은 범위 밖으로 둔다 |

### 11.2 미해결 질문

1. **`.ts` 스테이징 파일도 원격에 백업해야 하나?**
   장시간 방송 중 서버 디스크가 죽으면 전체가 사라진다. 주기적 partial 업로드는
   5장에서 배제했지만, "N GiB마다 스냅샷 업로드" 같은 절충안이 필요한지는 열려 있다.
   → 초기에는 하지 않고, 실제 장애 사례가 생기면 재검토.
2. **로컬 → S3 일괄 이관 도구의 형태.** CLI 관리 명령인가, 관리자 API 엔드포인트인가?
   진행률·재시도·부분 실패 처리를 어디까지 만들지 미정.
3. **백엔드 전환 후 신규 녹화만 S3로 가는 혼재 상태를 얼마나 오래 지원하나?**
   `storage_backend` 컬럼이 있으니 영구 지원도 가능하지만, 운영 복잡도가 누적된다.
4. **`local_absolute`(6.4의 2번 케이스)를 정식 백엔드 종류로 둘 것인가,**
   아니면 레거시 플래그로만 취급할 것인가? 전자는 인터페이스가 늘고, 후자는 분기가 남는다.
5. **썸네일을 S3에 둘 것인가, 로컬 캐시로 둘 것인가?**
   작고 요청이 잦아(목록 화면) 원격 왕복이 아깝다. 로컬 캐시 디렉터리를 두는 편이
   나을 수 있으나 캐시 무효화 복잡도가 생긴다.
6. **`total_bytes()`의 비용.** S3에서 전체 `list_objects_v2` 순회는 객체가 많아지면 느리다.
   DB `SUM(size)`로 대체하면 DB와 실제 스토리지의 드리프트를 감지할 수 없다. 정합성 검사 잡이 필요한가?
7. **presigned redirect와 `Entitlement` 취소의 상호작용.**
   구독 해지 즉시 접근을 끊어야 하는 요구가 있다면 presigned는 원리적으로 부적합하다.
   제품 요구사항 확인이 필요하다.
8. **boto3 의존성 추가에 대한 판단.** 설치 크기가 작지 않다.
   `local` 전용 사용자를 위해 optional extra(`pip install .[s3]`)로 분리할지,
   기본 의존성으로 넣을지 결정해야 한다. → optional extra를 선호하되 Docker 이미지에는 포함.
