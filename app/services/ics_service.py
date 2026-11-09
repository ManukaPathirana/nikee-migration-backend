"""Calendar invites (.ics) for confirmed or cancelled meetings."""

from datetime import UTC, datetime

from icalendar import Calendar, Event, vCalAddress, vText

from app.models import Meeting, MeetingMode
from app.services.email_service import Attachment

MODE_LABEL = {
    MeetingMode.ONLINE: "Online meeting",
    MeetingMode.IN_PERSON: "In-person meeting",
    MeetingMode.PHONE: "Phone call",
}


def where(meeting: Meeting) -> str:
    if meeting.mode == MeetingMode.ONLINE:
        return meeting.meeting_link or "Online"
    if meeting.mode == MeetingMode.IN_PERSON:
        return meeting.location or "Our office"
    return f"Phone call — we'll call {meeting.customer.phone or 'you'}"


def build_ics(meeting: Meeting, *, cancel: bool = False) -> bytes:
    """REQUEST for a confirmed meeting, CANCEL to remove it from calendars."""
    slot = meeting.confirmed_slot
    if slot is None:
        raise ValueError("Meeting has no confirmed time")

    cal = Calendar()
    cal.add("prodid", "-//Nikee Migrations//Meetings//EN")
    cal.add("version", "2.0")
    cal.add("method", "CANCEL" if cancel else "REQUEST")

    event = Event()
    event.add("uid", f"meeting-{meeting.id}@nikeemigration.com")
    event.add("sequence", meeting.ics_sequence)
    event.add("dtstamp", datetime.now(UTC))
    event.add("dtstart", slot.start_at.astimezone(UTC))
    event.add("dtend", slot.end_at.astimezone(UTC))
    event.add("summary", f"Nikee Migrations: {meeting.inquiry.subject} ({meeting.inquiry.ref_no})")
    description = [
        f"{MODE_LABEL[meeting.mode]} with {meeting.staff.full_name}",
        f"Inquiry: {meeting.inquiry.ref_no} — {meeting.inquiry.subject}",
    ]
    if meeting.meeting_link:
        description.append(f"Join: {meeting.meeting_link}")
    if meeting.staff_note:
        description.append(f"Note: {meeting.staff_note}")
    event.add("description", "\n".join(description))
    event.add("location", where(meeting))
    if meeting.meeting_link:
        event.add("url", meeting.meeting_link)
    event.add("status", "CANCELLED" if cancel else "CONFIRMED")

    organizer = vCalAddress(f"mailto:{meeting.staff.email}")
    organizer.params["cn"] = vText(meeting.staff.full_name)
    event["organizer"] = organizer
    for person in (meeting.customer, meeting.staff):
        attendee = vCalAddress(f"mailto:{person.email}")
        attendee.params["cn"] = vText(person.full_name)
        attendee.params["role"] = vText("REQ-PARTICIPANT")
        event.add("attendee", attendee, encode=0)

    cal.add_component(event)
    return cal.to_ical()


def ics_attachment(meeting: Meeting, *, cancel: bool = False) -> Attachment:
    return {
        "filename": "cancel.ics" if cancel else "invite.ics",
        "content": build_ics(meeting, cancel=cancel),
        "mime_type": "text/calendar",
    }
