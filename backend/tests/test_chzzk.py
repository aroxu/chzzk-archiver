import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app.services import chzzk
from app.services.chzzk import _mpd_estimated_size, _playback_from_json, _progressive_from_mpd, parse_content_url


def test_manual_url_types():
    assert parse_content_url("https://chzzk.naver.com/live/" + "a" * 32)[0] == "live"
    assert parse_content_url("https://chzzk.naver.com/video/123456")[0] == "vod"
    assert parse_content_url("https://chzzk.naver.com/clips/ABCDEF")[0] == "clip"


def test_rejects_non_chzzk_manual_url():
    with pytest.raises(HTTPException) as excinfo:
        parse_content_url("https://example.com/video/123")
    assert excinfo.value.status_code == 422


def test_extracts_hls_from_rewind_playback_json():
    playback = (
        '{"media":[{"protocol":"DASH","path":"https://example/dash"},'
        '{"protocol":"HLS","path":"https://example/master.m3u8"}]}'
    )
    assert _playback_from_json(playback) == "https://example/master.m3u8"


def test_selects_highest_progressive_mp4_up_to_1080p():
    mpd = b'''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet>
      <Representation mimeType="video/mp4" codecs="avc1,mp4a" height="720"><BaseURL>720.mp4</BaseURL></Representation>
      <Representation mimeType="video/mp4" codecs="avc1,mp4a" height="1080"><BaseURL>1080.mp4</BaseURL></Representation>
      <Representation mimeType="video/mp4" codecs="avc1,mp4a" height="2160"><BaseURL>2160.mp4</BaseURL></Representation>
    </AdaptationSet></Period></MPD>'''
    assert _progressive_from_mpd(mpd, "https://cdn.example/path/manifest.mpd") == "https://cdn.example/path/1080.mp4"


def test_estimates_dash_size_from_duration_and_selected_bitrates():
    mpd = b'''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT2M">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation height="720" bandwidth="2000000" />
          <Representation height="1080" bandwidth="4000000" />
          <Representation height="2160" bandwidth="9000000" />
        </AdaptationSet>
        <AdaptationSet mimeType="audio/mp4">
          <Representation bandwidth="128000" />
        </AdaptationSet>
      </Period>
    </MPD>'''
    assert _mpd_estimated_size(mpd) == 66_873_600


def test_channel_profile_parser_uses_canonical_metadata(monkeypatch):
    channel_id = "32dbf442f78a051a1ef4a5ac0bcf9773"
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "code": 200,
                "content": {
                    "channelId": channel_id,
                    "channelName": "example streamer",
                    "channelImageUrl": "https://img.example/profile.jpg",
                },
            },
        )
    )

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await chzzk.fetch_channel_profile(channel_id, client)

    monkeypatch.undo()
    assert asyncio.run(run()) == {"name": "example streamer", "image": "https://img.example/profile.jpg"}


def test_live_probe_uses_open_status_and_stable_id():
    async def run(payload):
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        async with httpx.AsyncClient(transport=transport) as client:
            return await chzzk.fetch_live("channel-id", client)

    assert asyncio.run(run({"code": 200, "content": None})) is None
    assert asyncio.run(run({"code": 200, "content": {"status": "CLOSE"}})) is None
    live = asyncio.run(
        run(
            {
                "code": 200,
                "content": {
                    "status": "OPEN",
                    "liveId": 1234,
                    "liveTitle": "test live",
                    "openDate": "2026-07-13 12:00:00",
                    "liveImageUrl": "https://img/{type}.jpg",
                    "channel": {"channelId": "channel-id", "channelName": "streamer"},
                },
            }
        )
    )
    assert live["id"] == "1234"
    assert live["thumbnail"] == "https://img/1080.jpg"
