"""Request and response shapes for the public API, straight from the Technical Contract.

Money is integer minor units (SGD) paired with a currency; datetimes are ISO 8601 UTC.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .serializers import iso_utc

# --- requests ----------------------------------------------------------------


class CreateOrderRequest(BaseModel):
    """POST /api/v1/orders.

    The buyer comes from the gateway, so the body carries the hold to buy from and the
    email the tickets go to (`customer_email` is accepted as an alias). Quantity and
    price are read from the hold, never from the client, so the client can never change
    what it pays or how many tickets are issued.
    """

    model_config = ConfigDict(extra="ignore")

    hold_id: str = Field(min_length=1, max_length=64)
    delivery_email: str = Field(min_length=3, max_length=320)

    @field_validator("hold_id")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


# --- responses ---------------------------------------------------------------


class PaymentView(BaseModel):
    status: str
    amount: int
    currency: str
    succeeded_at: str | None = None
    failed_at: str | None = None


class RefundView(BaseModel):
    refund_id: str
    amount: int
    currency: str
    reason: str
    status: str
    stripe_refund_id: str | None = None
    last_error: str | None = None


class OrderView(BaseModel):
    """The response body for GET and POST /api/v1/orders."""

    order_id: str
    status: str
    hold_id: str
    event_id: str
    category_id: str | None = None
    quantity: int
    unit_amount: int
    total_amount: int
    currency: str
    delivery_email: str | None = None
    payment: PaymentView | None = None
    refunds: list[RefundView] = []
    # Present only on a freshly created PENDING_PAYMENT order: the Stripe client secret
    # the frontend hands to the Payment Element. Never returned on later reads.
    client_secret: str | None = None
    created_at: str
    updated_at: str


class ListOrdersView(BaseModel):
    items: list[OrderView]
    total: int
    page: int
    page_size: int


def _ts(value) -> str:
    return iso_utc(value)
