"""Peewee ORM models mirroring the archiver schema."""

from __future__ import annotations

from datetime import UTC, datetime

from peewee import (
    AutoField,
    BooleanField,
    CharField,
    DateTimeField as _DateTimeField,
    ForeignKeyField,
    FloatField,
    IntegerField,
    Model,
    TextField,
)

from .db import database


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | str | None) -> datetime | None:
    """Normalise any stored timestamp representation into an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class DateTimeField(_DateTimeField):
    """DateTimeField that always reads back as an aware UTC datetime.

    Peewee stores timezone-aware values as ISO strings with an offset, which its
    default formats cannot parse back. Normalising on write and on read keeps
    comparisons against ``datetime.now(UTC)`` valid everywhere.
    """

    def db_value(self, value):
        if isinstance(value, datetime) and value.tzinfo:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return super().db_value(value)

    def python_value(self, value):
        return as_utc(super().python_value(value))


class BaseModel(Model):
    class Meta:
        database = database
        legacy_table_names = False


class User(BaseModel):
    id = AutoField()
    username = CharField(max_length=80, unique=True, index=True)
    password = CharField(max_length=255, null=True)
    role = CharField(max_length=16, default="user")
    active = BooleanField(default=True)
    oidc_issuer = CharField(max_length=255, null=True)
    oidc_subject = CharField(max_length=255, null=True)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "users"


class Channel(BaseModel):
    id = AutoField()
    chzzk_id = CharField(max_length=64, unique=True, index=True)
    name = CharField(max_length=120, default="알 수 없는 채널")
    image_url = TextField(null=True)
    last_live = BooleanField(default=False)

    class Meta:
        table_name = "channels"


class Subscription(BaseModel):
    id = AutoField()
    user = ForeignKeyField(User, field=User.id, column_name="user_id", backref="subscriptions", on_delete="CASCADE", index=True)
    channel = ForeignKeyField(Channel, field=Channel.id, column_name="channel_id", backref="subscriptions", on_delete="CASCADE", index=True)
    active = BooleanField(default=True)
    auto_record = BooleanField(default=True)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "subscriptions"
        indexes = ((("user", "channel"), True),)


class Broadcast(BaseModel):
    id = AutoField()
    channel = ForeignKeyField(Channel, field=Channel.id, column_name="channel_id", backref="broadcasts", on_delete="CASCADE", index=True)
    broadcast_id = CharField(max_length=100)
    source_type = CharField(max_length=16, default="live")
    source_url = TextField(null=True)
    title = CharField(max_length=300, default="라이브 방송")
    category = CharField(max_length=120, null=True)
    thumbnail_url = TextField(null=True)
    started_at = DateTimeField(null=True)

    class Meta:
        table_name = "broadcasts"
        indexes = ((("channel", "broadcast_id"), True),)


class Recording(BaseModel):
    id = AutoField()
    broadcast = ForeignKeyField(Broadcast, field=Broadcast.id, column_name="broadcast_id", backref="recordings", on_delete="CASCADE", unique=True, index=True)
    state = CharField(max_length=20, default="queued", index=True)
    path = TextField(null=True)
    size = IntegerField(default=0)
    total_size = IntegerField(default=0)
    speed_bps = IntegerField(default=0)
    eta_seconds = IntegerField(null=True)
    duration_seconds = FloatField(default=0)
    error = TextField(null=True)
    created_at = DateTimeField(default=utcnow)
    started_at = DateTimeField(null=True)
    finished_at = DateTimeField(null=True)

    class Meta:
        table_name = "recordings"
        # The library lists newest-first; without this SQLite sorts every
        # entitled row through a temporary B-tree on each request.
        indexes = ((("created_at",), False),)


class WorkerNode(BaseModel):
    """A remote encoder that polls the controller for leased work."""

    id = CharField(max_length=120, primary_key=True)
    hostname = CharField(max_length=255)
    platform = CharField(max_length=80)
    encoders = TextField(default="[]")
    version = CharField(max_length=40, default="unknown")
    last_seen_at = DateTimeField(default=utcnow, index=True)

    class Meta:
        table_name = "worker_nodes"


class EncodingJob(BaseModel):
    """Durable lease-backed encoding work associated with one recording."""

    recording = ForeignKeyField(
        Recording,
        field=Recording.id,
        column_name="recording_id",
        backref="encoding_jobs",
        on_delete="CASCADE",
        unique=True,
        index=True,
    )
    state = CharField(max_length=20, default="queued", index=True)
    worker = ForeignKeyField(
        WorkerNode,
        field=WorkerNode.id,
        column_name="worker_id",
        backref="jobs",
        on_delete="SET NULL",
        null=True,
    )
    video_encoder = CharField(max_length=40, default="auto")
    # The encoder that actually ran, which may differ from the request when
    # "auto" resolves to a GPU or falls back to software.
    used_encoder = CharField(max_length=40, null=True)
    quality = IntegerField(default=23)
    preset = CharField(max_length=40, default="medium")
    audio_mode = CharField(max_length=20, default="copy")
    output_extension = CharField(max_length=8, default=".mp4")
    source_path = TextField(null=True)
    progress = FloatField(default=0)
    processed_seconds = FloatField(default=0)
    duration_seconds = FloatField(default=0)
    encoding_speed = FloatField(default=0)
    eta_seconds = IntegerField(null=True)
    attempts = IntegerField(default=0)
    lease_expires_at = DateTimeField(null=True, index=True)
    upload_path = TextField(null=True)
    error = TextField(null=True)
    created_at = DateTimeField(default=utcnow)
    started_at = DateTimeField(null=True)
    finished_at = DateTimeField(null=True)

    class Meta:
        table_name = "encoding_jobs"


class Entitlement(BaseModel):
    id = AutoField()
    user = ForeignKeyField(User, field=User.id, column_name="user_id", backref="entitlements", on_delete="CASCADE", index=True)
    recording = ForeignKeyField(Recording, field=Recording.id, column_name="recording_id", backref="entitlements", on_delete="CASCADE", index=True)
    source = CharField(max_length=20, default="subscription")

    class Meta:
        table_name = "recording_entitlements"
        indexes = ((("user", "recording"), True),)


class Credential(BaseModel):
    id = AutoField()
    user = ForeignKeyField(User, field=User.id, column_name="user_id", backref="credentials", on_delete="CASCADE", unique=True)
    encrypted = TextField()
    valid = BooleanField(default=True)
    updated_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "credentials"


class Invite(BaseModel):
    id = AutoField()
    token_hash = CharField(max_length=64, unique=True)
    expires_at = DateTimeField()
    used_at = DateTimeField(null=True)

    class Meta:
        table_name = "invites"


class ApiToken(BaseModel):
    id = AutoField()
    user = ForeignKeyField(User, field=User.id, column_name="user_id", backref="tokens", on_delete="CASCADE", index=True)
    token_hash = CharField(max_length=64, unique=True)
    kind = CharField(max_length=20, default="extension")
    expires_at = DateTimeField(null=True)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "api_tokens"


class AuditLog(BaseModel):
    id = AutoField()
    actor = ForeignKeyField(User, field=User.id, column_name="actor_id", backref="audit_logs", on_delete="SET NULL", null=True)
    action = CharField(max_length=80)
    detail = TextField(default="{}")
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "audit_logs"


ALL_MODELS = [
    User,
    Channel,
    Subscription,
    Broadcast,
    Recording,
    WorkerNode,
    EncodingJob,
    Entitlement,
    Credential,
    Invite,
    ApiToken,
    AuditLog,
]
