import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.deps import CurrentUser, CustomerUser, DbSession
from app.schemas.common import Page, PageParams, page_params
from app.schemas.review import ReviewCreate, ReviewOut, ReviewSummary
from app.services import review_service

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("/public", response_model=ReviewSummary)
async def public_reviews(db: DbSession) -> ReviewSummary:
    """Public: average rating and the latest visible reviews (first name + initial only)."""
    return await review_service.public_summary(db)


@router.get("", response_model=Page[ReviewOut])
async def my_reviews(
    user: CurrentUser,
    db: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    inquiry_id: uuid.UUID | None = None,
) -> Page[ReviewOut]:
    """Customers: reviews they wrote. Staff: visible reviews about them. Admin: all."""
    rows, total = await review_service.list_reviews(db, user, params, inquiry_id=inquiry_id)
    return Page(
        items=[ReviewOut.model_validate(r) for r in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@router.post("", response_model=ReviewOut, status_code=status.HTTP_201_CREATED)
async def create_review(data: ReviewCreate, user: CustomerUser, db: DbSession) -> ReviewOut:
    """Rate a completed inquiry (1-5 stars). One review per inquiry."""
    review = await review_service.create_review(
        db, user, inquiry_id=data.inquiry_id, rating=data.rating, comment=data.comment
    )
    return ReviewOut.model_validate(review)
