import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class NotificationOut(ORMModel):
    id: uuid.UUID
    type: str
    title: str
    body: str | None
    data: dict[str, Any] | None
    read_at: datetime | None
    created_at: datetime


class UnreadCountOut(BaseModel):
    count: int


class DeviceTokenIn(BaseModel):
    fcm_token: str = Field(min_length=10, max_length=512)
    platform: Literal["android", "ios", "web"]
