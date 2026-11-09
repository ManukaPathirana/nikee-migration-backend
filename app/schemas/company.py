import uuid
from typing import Any

from pydantic import BaseModel, EmailStr, Field, HttpUrl

from app.schemas.common import ORMModel


class CompanyOut(ORMModel):
    id: uuid.UUID
    name: str
    about: str | None
    website_url: str
    phone: str | None
    email: str | None
    whatsapp: str | None
    address: str | None
    socials: dict[str, Any] | None


class CompanyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    about: str | None = Field(default=None, max_length=5000)
    website_url: HttpUrl | None = None
    phone: str | None = Field(default=None, max_length=40)
    email: EmailStr | None = None
    whatsapp: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=1000)
    socials: dict[str, str] | None = None
