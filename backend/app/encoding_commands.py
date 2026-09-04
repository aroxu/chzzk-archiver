"""Dependency-free FFmpeg command construction shared by server and worker."""

from __future__ import annotations

import json
import subprocess
from contextlib import suppress
from pathlib import Path

HEVC_ENCODERS = ("hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_vaapi", "libx265")

# Hardware encoders are tried before libx265 because they are an order of
# magnitude faster; libx265 stays last as the always-available fallback.
HARDWARE_ENCODERS = ("hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_vaapi")


def detect_hevc_encoders(ffmpeg: str = "ffmpeg") -> list[str]:
    """Return HEVC encoders this host can actually use, fastest first.

    ``ffmpeg -encoders`` lists everything the build was compiled with, so a
    machine with only an NVIDIA card still advertises QSV, AMF and VAAPI.
    Selecting one of those fails at encode time, which is far more expensive
    than probing here, so each hardware encoder gets a fractional-second trial
    run and only the survivors are reported.
    """
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False
    )
    output = f"{result.stdout}\n{result.stderr}"
    compiled = [name for name in HEVC_ENCODERS if name in output]
    return [
        name
        for name in compiled
        if name not in HARDWARE_ENCODERS or encoder_works(name, ffmpeg)
    ]


def encoder_works(encoder: str, ffmpeg: str = "ffmpeg") -> bool:
    """Encode a few synthetic frames to prove the encoder really runs."""
    try:
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                "-t", "0.2", "-c:v", encoder, "-f", "null", "-",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def choose_encoder(requested: str, available: list[str]) -> str:
    """Pick the encoder to run, preferring hardware when asked for "auto".

    ``available`` arrives ordered fastest-first, so "auto" naturally lands on a
    GPU when one works and on libx265 otherwise. An explicit request that this
    host cannot honour falls back to software instead of failing the job: a
    slower encode is better than losing the recording.
    """
    if not available:
        raise RuntimeError("사용 가능한 HEVC FFmpeg 인코더가 없습니다")
    if requested in ("auto", ""):
        return available[0]
    if requested in available:
        return requested
    if "libx265" in available:
        return "libx265"
    raise RuntimeError(f"요청한 FFmpeg 인코더를 사용할 수 없습니다: {requested}")


def output_extension(audio_mode: str) -> str:
    # FLAC in ISO-BMFF has poor player support. Matroska is predictable on all
    # worker platforms; copy mode keeps MP4 for the existing web player.
    return ".mkv" if audio_mode == "flac24" else ".mp4"


# Each family names its speed/quality tradeoff differently, so "auto" and any
# preset borrowed from another encoder must be translated before use.
DEFAULT_PRESETS = {
    "libx265": "medium",
    "hevc_nvenc": "p5",
    "hevc_qsv": "medium",
    "hevc_amf": "balanced",
}
VALID_PRESETS = {
    "libx265": {
        "ultrafast", "superfast", "veryfast", "faster", "fast",
        "medium", "slow", "slower", "veryslow", "placebo",
    },
    "hevc_nvenc": {"p1", "p2", "p3", "p4", "p5", "p6", "p7"},
    "hevc_qsv": {"veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"},
    "hevc_amf": {"speed", "balanced", "quality"},
}


def resolve_preset(encoder: str, preset: str) -> str:
    """Map a requested preset onto something this encoder accepts."""
    default = DEFAULT_PRESETS.get(encoder, "medium")
    if preset in ("auto", ""):
        return default
    return preset if preset in VALID_PRESETS.get(encoder, set()) else default


def ffmpeg_encode_command(
    source: Path,
    destination: Path,
    *,
    encoder: str,
    quality: int = 23,
    preset: str = "auto",
    audio_mode: str = "copy",
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Build a bounded HEVC encode command for CPU or common GPU backends."""
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-c:v",
        encoder,
    ]
    if encoder == "libx265":
        # CRF is libx265's quality-targeting mode; CQP is the hardware analogue.
        command += ["-preset", resolve_preset(encoder, preset), "-crf", str(quality)]
    elif encoder == "hevc_nvenc":
        command += [
            "-preset", resolve_preset(encoder, preset),
            "-rc", "constqp", "-qp", str(quality),
        ]
    elif encoder == "hevc_qsv":
        command += [
            "-preset", resolve_preset(encoder, preset),
            "-global_quality", str(quality),
        ]
    elif encoder == "hevc_amf":
        command += [
            "-quality", resolve_preset(encoder, preset),
            "-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality), "-qp_b", str(quality),
        ]
    elif encoder == "hevc_vaapi":
        command += ["-qp", str(quality)]
    else:
        raise ValueError(f"지원하지 않는 인코더입니다: {encoder}")

    if audio_mode == "copy":
        command += ["-c:a", "copy"]
    elif audio_mode == "flac24":
        command += ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
    else:
        raise ValueError(f"지원하지 않는 오디오 모드입니다: {audio_mode}")

    if destination.suffix.lower() == ".mp4":
        command += ["-tag:v", "hvc1", "-movflags", "+faststart"]
    command.append(str(destination))
    return command


def ffmpeg_stream_command(
    *,
    encoder: str,
    quality: int = 23,
    preset: str = "medium",
    audio_mode: str = "copy",
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Encode stdin to a streamable container on stdout."""
    command = ffmpeg_encode_command(
        Path("pipe:0"),
        Path("pipe:1"),
        encoder=encoder,
        quality=quality,
        preset=preset,
        audio_mode=audio_mode,
        ffmpeg=ffmpeg,
    )
    # The file-oriented helper cannot infer a container from pipe:1.
    # AAC copied from an MPEG-TS input remains valid in MPEG-TS without needing
    # codec extradata. FLAC is not supported by MPEG-TS, so that mode uses the
    # equally streamable Matroska container.
    command[-1:-1] = ["-f", "matroska" if audio_mode == "flac24" else "mpegts"]
    return command


def with_progress(command: list[str]) -> list[str]:
    """Ask FFmpeg for machine-readable progress without changing its output."""
    return [command[0], "-nostats", "-progress", "pipe:2", *command[1:]]


def parse_ffmpeg_time(value: str) -> float | None:
    """Convert FFmpeg's HH:MM:SS.microseconds progress timestamp to seconds."""
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    with suppress(ValueError):
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    return None


def probe_duration(path: Path, ffprobe: str = "ffprobe") -> float:
    """Return media duration, or zero for an unfinished/live input."""
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        duration = float(result.stdout.strip())
        return duration if duration > 0 else 0.0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def probe_media(path: Path, ffprobe: str = "ffprobe") -> dict:
    """Read stream metadata and reject missing/empty output."""
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError("인코딩 결과 파일이 비어 있습니다")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,bits_per_raw_sample",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("인코딩 결과에 비디오 스트림이 없습니다")
    if video.get("codec_name") not in {"hevc", "h265"}:
        raise RuntimeError("인코딩 결과가 HEVC가 아닙니다")
    return payload
