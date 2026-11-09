"""Customer reviews of completed inquiries, and admin moderation."""

import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import BadRequest, Conflict, NotFound
from app.models import Inquiry, InquiryStatus, Review, User, UserRole
from app.schemas.common import PageParams
from app.schemas.review import PublicReview, ReviewSummary
from app.services import job_queue
from app.services.audit_service import audit
from app.services.notification_service import notify


def _query() -> Select[tuple[Review]]:
    return (
        select(Review)
        .options(
            selectinload(Review.customer),
            selectinload(Review.staff),
            selectinload(Review.inquiry),
        )
        .execution_options(populate_existing=True)
    )


async def get_review(db: AsyncSession, review_id: uuid.UUID) -> Review:
    review = await db.scalar(_query().where(Review.id == review_id))
    if review is None:
        raise NotFound("Review not found")
    return review


async def create_review(
    db: AsyncSession, customer: User, *, inquiry_id: uuid.UUID, rating: int, comment: str | None
) -> Review:
    inquiry = await db.get(Inquiry, inquiry_id)
    if inquiry is None or inquiry.customer_id != customer.id:
        raise NotFound("Inquiry not found")
    if inquiry.status != InquiryStatus.COMPLETED:
        raise BadRequest("You can review an inquiry once it's completed", code="not_completed")
    if await db.scalar(select(Review.id).where(Review.inquiry_id == inquiry_id)):
        raise Conflict("You've already reviewed this inquiry", code="already_reviewed")
    review = Review(
        customer_id=customer.id,
        inquiry_id=inquiry.id,
        staff_id=inquiry.assigned_staff_id,
        rating=rating,
        comment=(comment or "").strip() or None,
    )
    db.add(review)
    if inquiry.assigned_staff_id:
        notify(
            db,
            user_id=inquiry.assigned_staff_id,
            type="review_received",
            title=f"New {rating}★ review for {inquiry.ref_no}",
            body=review.comment,
            data={"inquiry_id": str(inquiry.id)},
        )
    await db.commit()
    return await get_review(db, review.id)


async def list_reviews(
    db: AsyncSession,
    user: User,
    params: PageParams,
    *,
    inquiry_id: uuid.UUID | None = None,
    rating: int | None = None,
    visible: bool | None = None,
    flagged: bool | None = None,
    staff_id: uuid.UUID | None = None,
) -> tuple[list[Review], int]:
    query = _query()
    if user.role == UserRole.CUSTOMER:
        query = query.where(Review.customer_id == user.id)
    elif user.role == UserRole.STAFF:
        query = query.where(Review.staff_id == user.id, Review.is_visible.is_(True))
    if inquiry_id is not None:
        query = query.where(Review.inquiry_id == inquiry_id)
    if rating is not None:
        query = query.where(Review.rating == rating)
    if visible is not None:
        query = query.where(Review.is_visible.is_(visible))
    if flagged is not None:
        query = query.where(Review.flagged.is_(flagged))
    if staff_id is not None:
        query = query.where(Review.staff_id == staff_id)
    total = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = await db.scalars(
        query.order_by(Review.created_at.desc()).offset(params.offset).limit(params.page_size)
    )
    return list(rows), total or 0


async def moderate(db: AsyncSession, admin: User, review: Review, changes: dict) -> Review:
    reply_added = False
    if "is_visible" in changes and changes["is_visible"] is not None:
        review.is_visible = changes["is_visible"]
    if "flagged" in changes and changes["flagged"] is not None:
        review.flagged = changes["flagged"]
    if "admin_reply" in changes:
        reply = (changes["admin_reply"] or "").strip() or None
        reply_added = reply is not None and reply != review.admin_reply
        review.admin_reply = reply
    audit(
        db,
        actor_id=admin.id,
        action="review.moderate",
        entity="review",
        entity_id=review.id,
        meta={k: v for k, v in changes.items() if k != "admin_reply"} | {"replied": reply_added},
    )
    if reply_added:
        notify(
            db,
            user_id=review.customer_id,
            type="review_reply",
            title="We replied to your review",
            body=review.admin_reply,
            data={"inquiry_id": str(review.inquiry_id)},
        )
    await db.commit()
    review = await get_review(db, review.id)
    if reply_added:
        await job_queue.send_email(
            to=review.customer.email,
            subject="We replied to your review",
            template="inquiry_update.html",
            context={
                "name": review.customer.full_name,
                "heading": "Thanks for your feedback",
                "message": review.admin_reply,
                "ref_no": review.inquiry.ref_no,
                "subject": review.inquiry.subject,
                "status": review.inquiry.status.value,
            },
        )
    return review


def _author(name: str) -> str:
    parts = name.split()
    if not parts:
        return "A customer"
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1][0]}."


async def public_summary(db: AsyncSession, limit: int = 10) -> ReviewSummary:
    avg, count = (
        await db.execute(
            select(func.avg(Review.rating), func.count(Review.id)).where(
                Review.is_visible.is_(True), Review.flagged.is_(False)
            )
        )
    ).one()
    rows = await db.scalars(
        _query()
        .where(Review.is_visible.is_(True), Review.flagged.is_(False))
        .order_by(Review.created_at.desc())
        .limit(limit)
    )
    return ReviewSummary(
        average=round(float(avg), 1) if avg is not None else None,
        count=count or 0,
        reviews=[
            PublicReview(
                id=r.id,
                rating=r.rating,
                comment=r.comment,
                author=_author(r.customer.full_name),
                admin_reply=r.admin_reply,
                created_at=r.created_at,
            )
            for r in rows
        ],
    )
