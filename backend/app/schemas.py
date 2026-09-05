"""Request bodies accepted by the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel


class LoginBody(BaseModel):
    username: str
    password: str


class SetupBody(LoginBody):
    pass


class RegisterBody(LoginBody):
    invite: str


class SubscribeBody(BaseModel):
    channel: str
    auto_record: bool = True


class SubscriptionUpdateBody(BaseModel):
    auto_record: bool


class StartLiveBody(BaseModel):
    """Opt-in to capture a broadcast that is already running."""

    channel: str


class ManualDownloadBody(BaseModel):
    url: str


class UnsubscribeBody(BaseModel):
    remove_recordings: bool = False


class CookieBody(BaseModel):
    cookies: dict[str, str]
