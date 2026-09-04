"""Range-aware video streaming and thumbnail delivery."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from ..db import db
from ..models import User
from ..security import current_user
from ..services.media import thumbnail_path
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
    path = Path(rec.path)
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
        raise HTTPException(404, "썸네일이 없습니다")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
