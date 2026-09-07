"""Range-aware video streaming and thumbnail delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from ..config import logger
from ..db import db
from ..models import User
from ..security import current_user
from ..services.media import (
    archive_directory,
    generate_aac_hls,
    generate_download_mp4,
    generate_flac_asset,
    generate_thumbnail,
    migrate_legacy_recording,
    thumbnail_path,
    valid_hls_bundle,
)
from .recordings import entitled

router = APIRouter()

CHUNK_SIZE = 1024 * 1024


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Resolve an RFC 7233 byte range, or None when it is unsatisfiable.

    Handles the suffix form (``bytes=-500``) that Safari and several players
    send, and rejects malformed or out-of-bounds ranges instead of raising.
    """
    spec = header.strip().removeprefix("bytes=")
    if "," in spec or "-" not in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    try:
        if not start_s:
            # Suffix range: the last N bytes of the resource.
            suffix = int(end_s)
            if suffix <= 0:
                return None
            return max(0, size - suffix), size - 1
        start = int(start_s)
        end = int(end_s) if end_s else start + CHUNK_SIZE - 1
    except ValueError:
        return None
    if start < 0 or start >= size:
        return None
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


@router.get("/api/media/{recording_id}")
def media(recording_id: int, request: Request, user: User = Depends(current_user), _=Depends(db)):
    rec = entitled(user, recording_id)
    if rec.state != "completed":
        raise HTTPException(409, "다운로드가 완료되지 않았습니다")
    if not rec.path or not Path(rec.path).exists():
        raise HTTPException(404, "파일이 없습니다")
    try:
        source = Path(rec.path)
        path = generate_download_mp4(source) if source.name == "master.m3u8" else source
    except Exception as exc:
        raise HTTPException(422, f"MP4 다운로드 파일을 만들 수 없습니다: {str(exc)[-500:]}") from exc
    range_header = request.headers.get("range")
    size = path.stat().st_size
    if not range_header:
        return FileResponse(path, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})
    resolved = parse_range(range_header, size) if size else None
    if resolved is None:
        raise HTTPException(
            416,
            "요청한 범위를 재생할 수 없습니다",
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    start, end = resolved

    def chunks():
        with path.open("rb") as handle:
            handle.seek(start)
            yield handle.read(end - start + 1)

    return StreamingResponse(
        chunks(),
        206,
        {
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        },
        "video/mp4",
    )


@router.get("/api/thumbnails/{recording_id}")
def thumbnail(recording_id: int, user: User = Depends(current_user), _=Depends(db)):
    rec = entitled(user, recording_id)
    if rec.state != "completed" or not rec.path:
        raise HTTPException(404, "썸네일이 없습니다")
    path = thumbnail_path(Path(rec.path))
    if not path.exists():
        try:
            path = generate_thumbnail(Path(rec.path))
        except Exception as exc:
            logger.exception("thumbnail generation failed recording=%s path=%s", recording_id, rec.path)
            raise HTTPException(422, f"썸네일을 만들 수 없습니다: {str(exc)[-500:]}") from exc
    if not path.exists():
        raise HTTPException(404, "썸네일이 없습니다")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/media/{recording_id}/audio")
def radio_audio(
    recording_id: int,
    requested_format: Literal["aac", "flac"] | None = Query(default=None, alias="format"),
    user: User = Depends(current_user),
    _=Depends(db),
):
    """Serve only the user's selected AAC or FLAC track for radio mode."""
    rec = entitled(user, recording_id)
    if rec.state != "completed":
        raise HTTPException(409, "다운로드가 완료되지 않았습니다")
    if not rec.path or not Path(rec.path).exists():
        raise HTTPException(404, "파일이 없습니다")
    try:
        audio_format = requested_format or (user.audio_format if user.audio_format in {"aac", "flac"} else "aac")
        if audio_format == "aac":
            # Original mirrored HLS can contain muxed A/V segments, so lazily
            # create the audio rendition before redirecting the player to it.
            playlist = generate_aac_hls(Path(rec.path))
            if not playlist.is_file():
                raise RuntimeError("AAC HLS 재생목록이 생성되지 않았습니다")
            return RedirectResponse(f"/api/hls/{recording_id}/audio.m3u8", status_code=307)
        path = generate_flac_asset(Path(rec.path))
    except Exception as exc:
        raise HTTPException(422, f"오디오 전용 스트림을 만들 수 없습니다: {str(exc)[-500:]}") from exc
    media_type = "audio/flac" if audio_format == "flac" else "audio/mp4"
    return FileResponse(
        path,
        media_type=media_type,
        filename=f"recording-{recording_id}.{path.suffix.lstrip('.')}",
        content_disposition_type="inline",
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=86400"},
    )


@router.get("/api/hls/{recording_id}/{asset_path:path}")
def hls_asset(recording_id: int, asset_path: str, user: User = Depends(current_user), _=Depends(db)):
    """Serve an entitled recording's HLS master, rendition playlists, and segments."""
    rec = entitled(user, recording_id)
    if rec.state != "completed" or not rec.path or not Path(rec.path).exists():
        raise HTTPException(404, "재생할 파일이 없습니다")
    media_path = Path(rec.path)
    try:
        if media_path.name != "master.m3u8" or not valid_hls_bundle(media_path.parent):
            media_path = migrate_legacy_recording(recording_id)
    except Exception as exc:
        raise HTTPException(422, f"HLS 스트림을 만들 수 없습니다: {str(exc)[-500:]}") from exc
    root = archive_directory(media_path).resolve()
    target = (root / asset_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "HLS 파일을 찾을 수 없습니다")
    media_type = {
        ".m3u8": "application/vnd.apple.mpegurl",
        ".m4s": "video/iso.segment",
        ".mp4": "video/mp4",
    }.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(target, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"})
