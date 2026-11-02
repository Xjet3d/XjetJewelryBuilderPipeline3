"""Payment integration seam. Provider-specific logic stays behind this adapter; the checkout code
only knows the order's payment_status (pending · paid · failed · refunded · cancelled).

Today no provider is connected (P3_PAYMENT_PROVIDER=none): an order is created with payment
"pending" and says so plainly — a successful payment is NEVER faked. A real provider (hosted or
tokenised form, so card details never reach XJet) plugs in here with Begin()/Confirm() and the
webhook route can call Confirm() without touching the checkout flow.
"""

import os
from typing import Protocol

Statuses = ("pending", "paid", "failed", "refunded", "cancelled")
Labels = {"pending": "Payment pending", "paid": "Paid", "failed": "Payment failed", "refunded": "Refunded",
          "cancelled": "Cancelled"}


class PaymentProvider(Protocol):
    Name: str
    Available: bool

    def Begin(self, Order: dict) -> dict:
        """Start payment for a freshly created order.
        {"status": pending|requires_action|paid|failed, "ref": str|None, "client": dict|None, "message": str}
        `client` is whatever the browser needs to continue (e.g. a hosted-page URL or a client secret)."""

    def Confirm(self, Order: dict, Payload: dict) -> dict:
        """Settle a payment from the provider's return / webhook. Same shape as Begin()."""


class NoPaymentProvider:
    Name = "none"
    Available = False
    Message = ("Online payment is not available yet. Your order has been received and reserved; "
               "we will contact you to arrange payment.")

    def Begin(self, Order: dict) -> dict:
        return {"status": "pending", "ref": None, "client": None, "message": self.Message}

    def Confirm(self, Order: dict, Payload: dict) -> dict:
        raise RuntimeError("No payment provider is connected.")


def BuildPaymentProvider() -> PaymentProvider:
    Kind = os.environ.get("P3_PAYMENT_PROVIDER", "none").strip().lower()
    if Kind not in ("", "none"):
        raise ValueError(f"Unknown payment provider {Kind!r} (only 'none' is implemented yet)")
    return NoPaymentProvider()
