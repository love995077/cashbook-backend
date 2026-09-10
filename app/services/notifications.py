"""Outbound SMS / WhatsApp notifications.

PLACEHOLDER — `send_message` raises until a provider is wired up. Everything
around it (when to notify, what to say, and making sure a failure here can
never break a ledger write) is implemented.

------------------------------------------------------------------------------
IMPLEMENTING A PROVIDER
------------------------------------------------------------------------------
Option A — Twilio, which covers both SMS and WhatsApp through one API:

    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    client.messages.create(
        body=text,
        # WhatsApp needs the "whatsapp:" prefix on BOTH ends.
        from_=f"whatsapp:{settings.twilio_whatsapp_from}",
        to=f"whatsapp:{to_e164}",
    )

Option B — Meta's WhatsApp Cloud API (cheaper at volume, more setup):

    async with httpx.AsyncClient() as http:
        await http.post(
            f"https://graph.facebook.com/v21.0/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to_e164.lstrip("+"),
                "type": "template",
                "template": {
                    "name": "payment_settled",
                    "language": {"code": "hi"},
                    "components": [...],
                },
            },
        )

------------------------------------------------------------------------------
THINGS THAT WILL BITE
------------------------------------------------------------------------------
* WhatsApp will not let you send free-form text to someone who has not messaged
  you in the last 24 hours. Outside that window you must send a pre-approved
  TEMPLATE, submitted to Meta and reviewed (often a day or two). Since these
  notifications are triggered by the contractor, not by a worker replying, they
  will almost always fall outside the window — so plan on templates, and treat
  the free-form branch as the exception.
* Phone numbers must be E.164 (+919990001111). The database stores whatever the
  contractor typed, which for Indian numbers is usually 10 bare digits. That is
  what `to_e164` below is for; it assumes +91 and needs revisiting the day this
  app crosses a border.
* Never let a notification failure roll back a transaction. A worker's ledger
  being correct matters more than them getting a message about it, so every
  send is fire-and-forget through BackgroundTasks and swallows its own errors.
* Rate limits and cost are per message. A contractor settling twenty workers at
  once produces twenty sends; queue them if that becomes a problem.
* Consent. Workers should be able to opt out — a `notifications_enabled` column
  on `users` is the obvious next step, checked in `should_notify`.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from app.config import get_settings
from app.models.enums import TransactionType
from app.models.transaction import Transaction
from app.models.user import User

logger = logging.getLogger(__name__)
settings = get_settings()


class NotificationsUnavailable(RuntimeError):
    """No messaging provider is configured, or the provider failed."""


def to_e164(phone: str, default_country_code: str = "91") -> str:
    """Best-effort E.164. '8880002222' -> '+918880002222'.

    Numbers already carrying a country code are left alone.
    """
    digits = "".join(char for char in phone if char.isdigit() or char == "+")
    if digits.startswith("+"):
        return digits
    digits = digits.lstrip("0")
    if len(digits) > 10 and digits.startswith(default_country_code):
        return f"+{digits}"
    return f"+{default_country_code}{digits}"


def should_notify(transaction: Transaction) -> bool:
    """Whether this entry is worth a message.

    Two cases are worth interrupting someone's day for:
      * a payment settled — money actually changed hands
      * a large advance — the threshold is configurable because "large" depends
        on the trade; the default is five days at a typical day rate

    Wages are deliberately excluded: they are logged daily, and a message per
    wage entry would train everyone to ignore the messages.
    """
    if not settings.notifications_enabled:
        return False
    if transaction.type is TransactionType.PAYMENT:
        return True
    if transaction.type is TransactionType.ADVANCE:
        return transaction.amount >= Decimal(str(settings.large_advance_threshold))
    return False


def compose_message(
    transaction: Transaction, worker: User, contractor: User | None, balance: Decimal
) -> str:
    """The text to send.

    Kept short and factual: it is read on a cheap phone, possibly in a noisy
    place, and it is a record of money. The balance is included because that is
    the number the worker actually cares about.
    """
    amount = f"Rs. {transaction.amount:,.2f}"
    who = contractor.name if contractor else "Your contractor"
    date = transaction.date.strftime("%d %b %Y")

    if transaction.type is TransactionType.PAYMENT:
        headline = f"{who} settled a payment of {amount} on {date}."
    else:
        headline = f"{who} recorded an advance of {amount} on {date}."

    if balance >= 0:
        position = f"Your balance is now Rs. {balance:,.2f} payable to you."
    else:
        position = f"You have taken Rs. {abs(balance):,.2f} more than you have earned."

    return f"{headline} {position} - Cashbook"


async def send_message(to: str, text: str) -> None:
    """Deliver one message.

    Args:
        to: Destination in E.164 form.
        text: Message body.

    Raises:
        NotificationsUnavailable: Always, until a provider is implemented.
    """
    raise NotificationsUnavailable(
        "No messaging provider is configured. Implement send_message() in "
        "app/services/notifications.py and set NOTIFY_PROVIDER in .env."
    )


async def notify_transaction(
    transaction: Transaction, worker: User, contractor: User | None, balance: Decimal
) -> None:
    """Fire-and-forget notification for one transaction.

    Swallows every error on purpose. This runs after the response has been
    sent, and a messaging outage must never surface as a failed ledger write.
    """
    # A worker booked without a phone number has nowhere to be messaged. Not an
    # error, and not worth an exception: there is simply no recipient.
    if not worker.phone:
        logger.debug(
            "Notification skipped for transaction %s: worker %s has no phone number",
            transaction.id,
            worker.id,
        )
        return

    try:
        await send_message(to_e164(worker.phone), compose_message(transaction, worker, contractor, balance))
    except NotificationsUnavailable as exc:
        # Expected until a provider is configured — debug, not an error, so it
        # does not fill the log on every transaction.
        logger.debug("Notification skipped for transaction %s: %s", transaction.id, exc)
    except Exception:
        logger.exception("Notification failed for transaction %s", transaction.id)
