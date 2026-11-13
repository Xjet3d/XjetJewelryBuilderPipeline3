"""The promotion banner (P1, 12 Nov 2026): a slim XJet-red bar above the site's header that announces the charm promotion
(Admin → Pricing & Materials → Charm, under the promotion).

Its settings (product_settings "promo_banner", logged like every setting): on/off, the offer text, a text without a saving
(optional), a link on the site (optional), an optional start and end, "the text names its scope", and the falling snow on
both sides of the bar (owner, 13 Nov 2026: subtle, on by default; CSS only, still for visitors who ask for less motion). It never says more
than the customer can get — it is shown only while all of this holds:
  * the banner is on and inside its own dates, and charms are on offer to customers;
  * the charm promotion is active (p3/charmprices.py: on, inside its dates), so a disabled or ended promotion takes the
    banner down with it — by the clock, with no save;
  * the promotion covers every charm customers can order now (each shown size in each metal on sale) with one and the same
    saving (charmprices.Coverage) — unless an Admin ticked that the text names exactly which charms qualify;
  * the offer text, with its saving, only while the regular prices are verified reference prices and the visitor reads
    US dollars without VAT (the list prices the saving is in); otherwise the text without a saving, if there is one —
    else no banner.
"""

import re
from datetime import datetime, timezone

from p3 import charmprices as CharmPrices
from p3 import products as Products
from p3.context import HttpError

Key = "promo_banner"
Default = {"enabled": False, "text": "", "plain_text": "", "link": "", "starts_at": None, "ends_at": None, "names_scope": False,
           "snow": True}
SnowFlakes = 7                      # per side: few, small and slow — festive, never busy
TextMax, LinkMax = 90, 200
LinkPattern = re.compile(r"^/[A-Za-z0-9/_\-.?=&#%]*$")


def Settings(Ctx) -> dict:
    return {**Default, **(Ctx.Products.Get(Key) or {})}


def Validate(Raw) -> dict:
    """The banner to save: short texts (the offer, and one without a saving), a link on this site ("/design"), optional
    dates (the end after the start). On needs the offer text."""
    if not isinstance(Raw, dict):
        raise HttpError(400, "invalid_banner", "The banner is given as an object.")

    def Text(Name, V):
        V = " ".join(str(V or "").split())
        if len(V) > TextMax:
            raise HttpError(400, "invalid_banner", f"The {Name} is a short line (at most {TextMax} characters).")
        return V
    Link = str(Raw.get("link") or "").strip()
    if Link and (len(Link) > LinkMax or not LinkPattern.match(Link) or Link.startswith("//")):
        raise HttpError(400, "invalid_banner", "The link is a page of this site, starting with / (for example /design).")
    try:
        Starts = CharmPrices._When("The banner's start", Raw.get("starts_at"))
        Ends = CharmPrices._When("The banner's end", Raw.get("ends_at"))
    except CharmPrices.CharmPriceError as E:
        raise HttpError(400, "invalid_banner", str(E)) from None
    if Starts and Ends and CharmPrices._Dt(Ends) <= CharmPrices._Dt(Starts):
        raise HttpError(400, "invalid_banner", "The banner must end after it starts.")
    Out = {"enabled": Raw.get("enabled") is True, "text": Text("offer text", Raw.get("text")),
           "plain_text": Text("text without a saving", Raw.get("plain_text")), "link": Link,
           "starts_at": Starts, "ends_at": Ends, "names_scope": Raw.get("names_scope") is True,
           "snow": Raw.get("snow") is not False}
    if Out["enabled"] and not Out["text"]:
        raise HttpError(400, "invalid_banner", "Write the banner's text before switching it on.")
    return Out


def Decide(Ctx, Market=None, At: datetime | None = None, Visible: tuple | None = None) -> dict:
    """{"shown": {text, link, until} or None, "why": the reason when not shown (the Admin reads it)}. Market: how the visitor
    reads prices (B6: ₪ or $ including VAT); None: US dollars, the list prices. Visible: the products this visitor sees."""
    Now_ = At or datetime.now(timezone.utc)
    B = Settings(Ctx)
    if not B["enabled"]:
        return {"shown": None, "why": "The banner is off."}
    if B["starts_at"] and Now_ < CharmPrices._Dt(B["starts_at"]):
        return {"shown": None, "why": "The banner's start is still ahead."}
    if B["ends_at"] and Now_ >= CharmPrices._Dt(B["ends_at"]):
        return {"shown": None, "why": "The banner's end has passed."}
    if Products.Charm not in (Visible if Visible is not None else Ctx.Products.AvailableProducts()):
        return {"shown": None, "why": "Charms are not on offer to customers."}
    P = Ctx.CharmPrices.Promotion()
    State = CharmPrices.PromotionState(P, Now_)
    if State != "active":
        return {"shown": None, "why": {"off": "The charm promotion is off.", "scheduled": "The charm promotion has not started.",
                                        "ended": "The charm promotion has ended."}[State]}
    C = CharmPrices.Coverage(Ctx, Now_)
    if not C["eligible"]:
        return {"shown": None, "why": "No charm on offer has a promotional price."}
    if not B["names_scope"] and not C["all"]:
        return {"shown": None, "why": "Not every charm on offer is in the promotion (" + ", ".join(X["label"] for X in C["not_eligible"])
                                      + "): tick that the text names exactly which charms qualify, or give them a promotional price."}
    if not B["names_scope"] and not C["uniform"]:
        return {"shown": None, "why": "The savings differ between charms; tick that the text names exactly which charms qualify."}
    Claims = bool(P.get("reference_verified")) and not Market
    Text = B["text"] if Claims else B["plain_text"]
    if not Text:
        return {"shown": None, "why": ("The regular prices are not verified as reference prices" if not P.get("reference_verified")
                                       else "This visitor reads prices including VAT")
                                      + ", and there is no text without a saving."}
    Ends = [E for E in (B["ends_at"], P.get("ends_at")) if E]
    return {"shown": {"text": Text, "link": B["link"] or None, "until": min(Ends) if Ends else None, "snow": B["snow"]}, "why": None}


def Shown(Ctx, Market=None, At: datetime | None = None, Visible: tuple | None = None) -> dict | None:
    return Decide(Ctx, Market, At, Visible)["shown"]


def Html(Banner: dict | None, Base: str) -> str:
    """The banner's markup, written into the page by the server (no shift when the app starts); the app takes it down at
    its end ("until") while the page stays open."""
    if not Banner:
        return ""
    import html
    T = html.escape(Banner["text"], quote=False)
    Inner = (f'<a href="{html.escape(Base + Banner["link"], quote=True)}" class="promo-banner-link" @click="bannerClick($event)">{T}'
             '<span class="promo-banner-arrow" aria-hidden="true">→</span></a>') if Banner.get("link") else f'<span>{T}</span>'
    # the snow: a few flakes in each side zone, faded out before the text (index.html .promo-snow); decoration only
    Snow = "".join(f'<span class="promo-snow promo-snow-{S}" aria-hidden="true">{"<i></i>" * SnowFlakes}</span>' for S in ("l", "r")) \
        if Banner.get("snow") else ""
    return (f'<div id="promo-banner" class="promo-banner" x-show="bannerShown" role="region" aria-label="Offer">'
            f'{Snow}{Inner}</div>')
