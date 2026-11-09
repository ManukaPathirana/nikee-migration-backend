import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel
from app.schemas.inquiry import UserBrief
from app.schemas.meeting import InquiryRef


class ReviewCreate(BaseModel):
    inquiry_id: uuid.UUID
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


class ReviewOut(ORMModel):
    id: uuid.UUID
    rating: int
    comment: str | None
    is_visible: bool
    flagged: bool
    admin_reply: str | None
    created_at: datetime
    inquiry: InquiryRef
    customer: UserBrief
    staff: UserBrief | None


class ReviewModerate(BaseModel):
    is_visible: bool | None = None
    flagged: bool | None = None
    # "" removes the reply
    admin_reply: str | None = Field(default=None, max_length=2000)


class PublicReview(BaseModel):
    id: uuid.UUID
    rating: int
    comment: str | None
    author: str  # "Jane P."
    admin_reply: str | None
    created_at: datetime


class ReviewSummary(BaseModel):
    average: float | None
    count: int
    reviews: list[PublicReview]
