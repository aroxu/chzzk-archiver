"""Standalone Windows/Linux remote encoder worker."""

from __future__ import annotations

import argparse
import json
import platform
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urljoin

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from .encoding_commands import (
    detect_hevc_encoders,
    ffmpeg_stream_command,
    parse_ffmpeg_time,
    with_progress,
)

VERSION = "0.1.0"


class WorkerSettings(BaseSettings):
    """Worker options shared by binaries, services and containers.

    The same ``ARCHIVER_ENCODING_VIDEO_ENCODER`` name is used by the
    controller and remote workers.  A standalone binary also reads a ``.env``
    file from its working directory, while real environment variables take
    precedence over that file.
    """

    model_config = SettingsConfigDict(
        env_prefix="ARCHIVER_", env_file=".env", extra="ignore"
    )
    worker_server: str | None = None
    worker_token: str | None = None
    worker_id: str | None = None
    worker_ffmpeg: str = "ffmpeg"
    worker_poll_interval: float = 5.0
    encoding_video_encoder: str = "auto"


def configured_encoders(detected: list[str], requested: str) -> list[str]:
    """Restrict advertised encoders when an operator selects one explicitly."""
    requested = requested.strip().lower()
    if requested in {"", "auto"}:
        return detected
    if requested not in {"hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_vaapi", "libx265"}:
        raise ValueError(f"지원하지 않는 HEVC 인코더입니다: {requested}")
    return [requested] if requested in detected else []


def default_worker_id(configured: str | None = None) -> str:
    return configured or f"{socket.gethostname()}-{uuid.getnode():012x}"


def headers(token: str, worker_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Worker-ID": worker_id}


def _feed_ffmpeg(stream, process: subprocess.Popen) -> None:
    try:
        try:
            while chunk := stream.read(1024 * 1024):
                process.stdin.write(chunk)
        except (BrokenPipeError, OSError):
            pass
    finally:
        if process.stdin:
            process.stdin.close()


def _read_ffmpeg_progress(stream, callback, errors: list[str]) -> None:
    values: dict[str, str] = {}
    for raw_line in iter(stream.readline, b""):
        line = raw_line.decode(errors="replace").strip()
        if "=" in line:
            key, value = line.split("=", 1)
            if key in {"out_time", "speed", "progress"}:
                values[key] = value
                if key == "progress" and callback:
                    processed = parse_ffmpeg_time(values.get("out_time", ""))
                    try:
                        speed = float(values.get("speed", "0").rstrip("x"))
                    except ValueError:
                        speed = 0.0
                    if processed is not None:
                        try:
                            callback(processed, speed)
                        except Exception as exc:
                            # Progress reporting is best-effort. Keep draining
                            # stderr so a brief control-plane outage cannot
                            # deadlock the media encode.
                            errors.append(f"progress heartbeat failed: {exc}")
                continue
        errors.append(line)
        del errors[:-50]


def run_stream_job(
    job: dict,
    token: str,
    ffmpeg: str = "ffmpeg",
    progress_callback=None,
) -> None:
    """Bridge one full-duplex TCP media stream through an FFmpeg process."""
    command = ffmpeg_stream_command(
        encoder=job["encoder"],
        quality=int(job["quality"]),
        preset=job["preset"],
        audio_mode=job["audio_mode"],
        ffmpeg=ffmpeg,
    )
    command = with_progress(command)
    with socket.create_connection(
        (job["stream_host"], int(job["stream_port"])), timeout=15
    ) as connection:
        connection.settimeout(None)
        hello = {
            "token": token,
            "worker_id": job["worker_id"],
            "job_id": job["id"],
        }
        connection.sendall(json.dumps(hello).encode() + b"\n")
        incoming = connection.makefile("rb", buffering=0)
        response = incoming.readline(4096)
        if response != b"OK\n":
            raise RuntimeError(response.decode(errors="replace").strip() or "stream rejected")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        feeder = threading.Thread(target=_feed_ffmpeg, args=(incoming, process), daemon=True)
        errors: list[str] = []
        progress_reader = threading.Thread(
            target=_read_ffmpeg_progress,
            args=(process.stderr, progress_callback, errors),
            daemon=True,
        )
        feeder.start()
        progress_reader.start()
        try:
            while chunk := process.stdout.read(1024 * 1024):
                connection.sendall(chunk)
            process.stdout.close()
            connection.shutdown(socket.SHUT_WR)
            feeder.join()
            returncode = process.wait()
            progress_reader.join()
            if returncode != 0:
                raise RuntimeError("\n".join(errors)[-2000:] or f"ffmpeg exited with {returncode}")
        finally:
            if process.poll() is None:
                process.terminate()


def lease_once(
    client: httpx.Client,
    server: str,
    token: str,
    worker_id: str,
    ffmpeg: str,
    requested_encoder: str = "auto",
    encoders: list[str] | None = None,
) -> bool:
    if encoders is None:
        encoders = configured_encoders(detect_hevc_encoders(ffmpeg), requested_encoder)
    if not encoders:
        raise RuntimeError(
            f"설정한 인코더를 이 호스트에서 사용할 수 없습니다: {requested_encoder}"
        )
    response = client.post(
        urljoin(server.rstrip("/") + "/", "api/worker/lease"),
        headers=headers(token, worker_id),
        json={
            "worker_id": worker_id,
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "encoders": encoders,
            "version": VERSION,
        },
    )
    if response.status_code == 204:
        return False
    response.raise_for_status()
    job = response.json()
    job["worker_id"] = worker_id
    try:
        heartbeat_url = urljoin(
            server.rstrip("/") + "/", job["heartbeat_url"].lstrip("/")
        )
        last_reported = 0.0

        def report_progress(processed_seconds: float, encoding_speed: float) -> None:
            nonlocal last_reported
            now = time.monotonic()
            if now - last_reported < 1 and processed_seconds > 0:
                return
            last_reported = now
            heartbeat = client.post(
                heartbeat_url,
                headers=headers(token, worker_id),
                json={
                    "processed_seconds": processed_seconds,
                    "encoding_speed": encoding_speed,
                },
            )
            heartbeat.raise_for_status()

        run_stream_job(job, token, ffmpeg, report_progress)
        completed = client.post(
            urljoin(server.rstrip("/") + "/", job["complete_url"].lstrip("/")),
            headers=headers(token, worker_id),
            timeout=120,
        )
        completed.raise_for_status()
    except Exception as exc:
        with __import__("contextlib").suppress(Exception):
            client.post(
                urljoin(server.rstrip("/") + "/", job["fail_url"].lstrip("/")),
                headers=headers(token, worker_id),
                json={"error": str(exc)[-4000:]},
            )
        raise
    return True


def doctor(
    server: str | None, token: str | None, ffmpeg: str, requested_encoder: str = "auto"
) -> int:
    detected = detect_hevc_encoders(ffmpeg)
    try:
        encoders = configured_encoders(detected, requested_encoder)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({
        "ffmpeg": ffmpeg,
        "configured_encoder": requested_encoder,
        "detected_hevc_encoders": detected,
        "advertised_hevc_encoders": encoders,
    }, ensure_ascii=False))
    if not encoders:
        print(f"설정한 HEVC 인코더를 사용할 수 없습니다: {requested_encoder}", file=sys.stderr)
        return 1
    if server and token:
        try:
            response = httpx.get(urljoin(server.rstrip("/") + "/", "health/live"), timeout=5)
            response.raise_for_status()
            print(f"controller: {response.status_code}")
        except Exception as exc:
            print(f"controller 연결 실패: {exc}", file=sys.stderr)
            return 1
    return 0


def main() -> int:
    runtime = WorkerSettings()
    parser = argparse.ArgumentParser(description="CHZZK Archive remote encoding worker")
    parser.add_argument("--server", default=runtime.worker_server)
    parser.add_argument("--token", default=runtime.worker_token)
    parser.add_argument("--worker-id", default=default_worker_id(runtime.worker_id))
    parser.add_argument("--ffmpeg", default=runtime.worker_ffmpeg)
    parser.add_argument("--poll-interval", type=float, default=runtime.worker_poll_interval)
    parser.add_argument(
        "--encoder",
        default=runtime.encoding_video_encoder,
        help="auto, hevc_nvenc, hevc_qsv, hevc_amf, hevc_vaapi, or libx265",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    args = parser.parse_args()
    if args.doctor:
        return doctor(args.server, args.token, args.ffmpeg, args.encoder)
    if not args.server or not args.token:
        parser.error("--server and --token (or matching environment variables) are required")
    try:
        encoders = configured_encoders(detect_hevc_encoders(args.ffmpeg), args.encoder)
    except ValueError as exc:
        parser.error(str(exc))
    if not encoders:
        parser.error(f"설정한 인코더를 이 호스트에서 사용할 수 없습니다: {args.encoder}")
    print(
        json.dumps({"worker_id": args.worker_id, "hevc_encoders": encoders}, ensure_ascii=False),
        flush=True,
    )
    with httpx.Client(timeout=30) as client:
        while True:
            try:
                worked = lease_once(
                    client,
                    args.server,
                    args.token,
                    args.worker_id,
                    args.ffmpeg,
                    args.encoder,
                    encoders,
                )
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                print(f"worker error: {exc}", file=sys.stderr, flush=True)
                worked = False
            if args.once:
                return 0 if worked else 2
            time.sleep(0 if worked else max(1.0, args.poll_interval))


if __name__ == "__main__":
    raise SystemExit(main())
