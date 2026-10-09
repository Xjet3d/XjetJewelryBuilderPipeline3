"""A daily AI spend warning: one email when the day's estimated live AI spend passes the Admin's threshold.

Admin → Settings → System → AI spend warning keeps the threshold (USD per UTC day) and the address to tell, in the
database (never in the code). After every recorded live submission (p3/credits.py RecordUsage) the day's estimate is
compared with the threshold; the first submission that crosses it sends the warning, once per day, on a background
thread. It never stops, slows or refuses a request: the hard cap is a separate configuration (P3_DAILY_AI_SPEND_CAP_USD)
and is not changed here. The estimate uses the list prices in Admin → AI prices; fal.ai's invoice is the reference."""

from __future__ import annotations

import logging
import threading
from urllib.parse import urlparse

from p3 import credits as Credits
from p3.context import Context, HttpError
from p3.db import Now

Logger = logging.getLogger("p3.spendwarning")
Key = "ai_spend_warning"                 # {"threshold_usd": 50.0, "to": "someone@example.com"}
SentKey = "ai_spend_warning_sent"        # the latest warning: {"day", "at", "spend_usd", "threshold_usd", "to", "delivery"}
MaxThreshold = 100000.0
_Lock = threading.Lock()


def Config(Ctx: Context) -> dict:
    C = Ctx.Products.Get(Key) or {}
    return {"threshold_usd": C.get("threshold_usd"), "to": C.get("to") or ""}


def State(Ctx: Context) -> dict:
    """For the Admin: the threshold, the address, the day's estimate, the cap (if any) and the latest warning."""
    return {**Config(Ctx), "spend_today_usd": round(Credits.SpendToday(Ctx), 4), "day": Credits.Today(),
            "cap_usd": Ctx.Settings.DailyAiSpendCapUsd, "last": Ctx.Products.Get(SentKey) or None,
            "mail_mode": getattr(getattr(Ctx, "Mailer", None), "Mode", None), "live": Ctx.Provider.Name != "mock"}


def Configure(Ctx: Context, Threshold, To, By: str) -> dict:
    """Threshold: a USD amount per day, or empty to turn the warning off. To: the address that receives it."""
    from p3.accounts.local import EmailPattern
    if Threshold in (None, ""):
        Value = None
    else:
        try:
            Value = round(float(Threshold), 2)
        except (TypeError, ValueError):
            raise HttpError(400, "invalid_threshold", "The threshold is an amount in US dollars per day.") from None
        if not 1 <= Value <= MaxThreshold:
            raise HttpError(400, "invalid_threshold", f"The threshold must be between $1 and ${MaxThreshold:,.0f} per day.")
    To = str(To or "").strip()
    if To and not EmailPattern.match(To):
        raise HttpError(400, "invalid_email", "Please enter a valid email address.")
    if Value is not None and not To:
        raise HttpError(400, "email_required", "Enter the address that receives the warning.")
    Ctx.Products.Set(Key, {"threshold_usd": Value, "to": To}, By,
                     f"AI spend warning at ${Value:,.2f} a day" if Value is not None else "AI spend warning off")
    return State(Ctx)


def _Site(Ctx: Context) -> tuple[str, str]:
    """The site's name in the warning (its host and base path) and the Admin's address, where known."""
    S = Ctx.Settings
    if S.PublicBaseUrl:
        return urlparse(S.PublicBaseUrl).netloc + S.BasePath, f"{S.PublicBaseUrl}{S.BasePath}/admin#/dashboard"
    return f"P3 ({S.Env}){S.BasePath}", ""


def Check(Ctx: Context, Background: bool = True) -> dict | None:
    """After a live submission: warn once a day when the day's estimate reaches the threshold. Never raises."""
    try:
        C = Config(Ctx)
        if not C["threshold_usd"] or not C["to"] or Ctx.Provider.Name == "mock" or getattr(Ctx, "Mailer", None) is None:
            return None
        Spend = Credits.SpendToday(Ctx)
        if Spend < C["threshold_usd"]:
            return None
        Day = Credits.Today()
        with _Lock:                                    # one warning a day, whatever runs at the same time
            Last = Ctx.Products.Get(SentKey) or {}
            if Last.get("day") == Day:
                return None
            Sent = {"day": Day, "at": Now(), "spend_usd": round(Spend, 2), "threshold_usd": C["threshold_usd"],
                    "to": C["to"], "delivery": "sending"}
            Ctx.Products.Set(SentKey, Sent, "system", f"AI spend warning: ${Spend:,.2f} today (threshold ${C['threshold_usd']:,.2f})")
        if Background:
            threading.Thread(target=_Deliver, args=(Ctx, Sent), name="p3-spend-warning", daemon=True).start()
        else:
            _Deliver(Ctx, Sent)
        return Sent
    except Exception:  # noqa: BLE001 — a warning problem never touches the customer's request
        Logger.exception("AI spend warning check failed")
        return None


def _Deliver(Ctx: Context, Sent: dict) -> None:
    from p3.mail import SpendWarningEmail
    Site, AdminUrl = _Site(Ctx)
    Subject, Html = SpendWarningEmail(Sent["spend_usd"], Sent["threshold_usd"], Sent["day"], Site, AdminUrl,
                                      Ctx.Settings.DailyAiSpendCapUsd)
    try:
        Ctx.Mailer.Send(Sent["to"], Subject, Html)
        Delivery = "sent (" + str(getattr(Ctx.Mailer, "Mode", "mail")) + ")"
    except Exception as E:  # noqa: BLE001
        Logger.warning("AI spend warning email failed: %s", E)
        Delivery = f"failed: {type(E).__name__}"
    try:
        Ctx.Products.Set(SentKey, {**Sent, "delivery": Delivery}, "system", "AI spend warning " + Delivery)
    except Exception:  # noqa: BLE001
        Logger.exception("AI spend warning: recording the delivery failed")
