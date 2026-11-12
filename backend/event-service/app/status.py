from enum import StrEnum


class EventStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


# Drafts stay private to their organiser.
PUBLIC_STATUSES = (EventStatus.PUBLISHED, EventStatus.CANCELLED, EventStatus.COMPLETED)
