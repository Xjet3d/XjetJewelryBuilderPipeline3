"""Checkout and orders — the real order path for fixed-price materials.

  Bag → Checkout → Customer details → Shipping address → Promo code → Payment → Order confirmation

Rules (the Legacy JewelryB2C flow was the functional reference; its browser-only "order", random
XJ numbers, mandatory XJET10 coupon and client-side prices were deliberately NOT carried over):
  * the server is authoritative: every unit price is the CURRENT fixed price of the material at the
    moment of ordering (the bag snapshot only flags "repriced"), promo discounts are evaluated here,
    totals are computed here, and the order stores a snapshot of all of it;
  * an order is a reservation until XJet has reviewed the design for production feasibility — the
    confirmation says so; payment is never faked (p3/payments.py);
  * one order per click (client_request_id); an order repeating one of the customer's own from the last day (the same
    pieces to the same address) is placed only when they confirm it; an empty or non-purchasable bag cannot be ordered,
    Gold (luxury) is not on this path (it asks for a quote instead: RequestQuote);
  * terms acceptance is recorded with its version and time; the customer's known profile pre-fills
    the form but never overwrites what they typed (the browser's job);
  * no production cost or 3D price ever appears in anything the customer receives.
"""

import html
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from p3 import addressing as Addressing
from p3 import assets
from p3 import charmprices as CharmPrices
from p3 import materialstatus as MaterialStatus
from p3 import payments as Payments
from p3 import products as Products
from p3.pricing.service import PublicPromotion
from p3 import ilspricing as IlsPricingMod
from p3 import ringids as RingIds
from p3 import sessions as Sessions
from p3 import versions as Versions
from p3.accounts import Principal
from p3.accounts.local import EmailPattern
from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now
from p3.testrecords import IsTestCustomer, ReservedEmail

# Orders placed before 2 Nov 2026 carry this fixed label; a newer order carries the archived version it accepted (M6,
# Orders.AcceptedTerms). Kept as it is on those orders: nothing is back-filled (they are test orders, WP7)
LegacyTermsLabel = "2026-10-02"
# Every delivery method an order may carry: an order placed with one keeps its label, price and total for good
ShippingMethods = {
    "standard": {"id": "standard", "label": "Standard delivery", "price": 0.0, "eta": "up to 14 business days",
                 "detail": "Free · tracked, signature on delivery"},
    "express": {"id": "express", "label": "Express delivery", "price": 45.0, "eta": "about 5 business days",
                "detail": "$45 · priority handling"},   # CON-07: the insurance claim is unverified (Counsel Book A-3), so it is not made (3 Nov 2026)
}
# The methods a new order or an approved quote can have: Standard only (owner's P1, 12 Nov 2026 — Express retired). Checkout
# shows no choice while one method is left; a request for another is answered with Standard (the quote) or refused (an order)
ShippingActive = ("standard",)
ShippingOptions = {K: V for K, V in ShippingMethods.items() if K in ShippingActive}
DefaultShipping = "standard"
RetiredShippingText = ("Express delivery is no longer available. Your order uses free standard delivery — "
                       "please check the total and place the order again.")
# "confirmed": the design review passed (B7, owner's approval 11 Nov 2026) — set by the Admin, or on a site with Fulfillment
# when every part of the order is approved for production; the order then waits for its payment
StatusOrder = ["new", "confirmed", "payment_confirmed", "three_d_ready", "production", "qc", "shipped", "completed"]
StatusLabels = {"new": "New", "confirmed": "Confirmed", "payment_confirmed": "Payment confirmed", "three_d_ready": "3D ready",
                "production": "In production", "qc": "Quality check", "shipped": "Shipped", "completed": "Completed", "cancelled": "Cancelled"}
# What the customer reads for each status in My Account (B7, owner's decisions 11 Nov 2026): one status per order, never an
# internal word ("New", "3D ready"); 3D ready, In production and Quality check all read "Production"
CustomerStatusLabels = {"new": "Design review", "confirmed": "Awaiting payment", "payment_confirmed": "Payment received",
                        "three_d_ready": "Production", "production": "Production", "qc": "Production",
                        "shipped": "Shipped", "completed": "Completed", "cancelled": "Cancelled"}
_InProduction = ("three_d_ready", "production", "qc")
_OrderRefPattern = re.compile(r"ORD-(\d{1,9})")
ProductionStatuses = ("production", "qc", "shipped", "completed")     # need every line's 3D result complete (SetStatus)
PhoneDigits = re.compile(r"\d")
PhonePattern = re.compile(r"^\+?[0-9 ()./-]{7,24}$")
MaxLines = 20
# An order that repeats one of the customer's own from the last day (the same pieces to the same address) is placed only
# when they confirm it (22 Oct: one checkout run twice, 44 s apart, made two identical orders)
RepeatHours = 24


def ShippingFor(Market=None) -> dict:
    """The delivery options as this customer sees them: in ₪ including VAT for an Israeli delivery (B6), in $ including VAT
    for a visitor from Israel who chose $ before giving an address, else as listed."""
    if not Market:
        return ShippingOptions
    Out = {}
    for K, O in ShippingOptions.items():
        Price = Market.Unit(O["price"]) if O["price"] else 0.0
        Shown = f"₪{Price:g}" if Market.currency == "ILS" else f"${Price:,.2f}"
        Detail = O["detail"].replace(f"${O['price']:g}", Shown) if O["price"] else O["detail"]
        Out[K] = {**O, "price": Price, "detail": Detail, "usd_price": O["price"], "currency": Market.currency}
    return Out


def OrderRef(No) -> str | None:
    return f"ORD-{No}" if No is not None else None


def QuoteRef(No) -> str | None:
    return f"Q-{No}" if No is not None else None


def OrderPageUrl(Site: str, Ref: str | None) -> str:
    """The order's own page in My Account (B7, owner's approval 11 Nov 2026): the address of an email's View Your Order
    button. It carries the Order ID only — never an internal id, a token or customer data — and opens only for the
    order's owner once signed in. Emails sent before carry {site}/#account, which keeps opening My Orders."""
    if not Site:
        return ""
    return f"{Site.rstrip('/')}/account/orders/{Ref}" if Ref else f"{Site.rstrip('/')}/account/orders"


def _Like(Text: str) -> str:
    """A LIKE pattern for a search box: the text anywhere (case-insensitive for ASCII), % and _ taken literally."""
    return "%" + re.sub(r"([\\%_])", r"\\\1", Text.strip()) + "%"


# Customer name ("Dana Levi") and email inside the customer snapshot, for searching
_Name = "(COALESCE(json_extract({0}.customer_json, '$.first_name'), '') || ' ' || COALESCE(json_extract({0}.customer_json, '$.last_name'), ''))"
_Email = "COALESCE(json_extract({0}.customer_json, '$.email'), '')"


QuoteOpen = ("new", "quoted", "answered")          # a quote can still be sent, revised and decided
QuoteStatuses = ("new", "quoted", "answered", "approved", "rejected", "closed")
# What a customer reads for each of those statuses (My Account); an open request whose quote ran out reads "Quote expired"
CustomerQuoteLabels = {"new": "Requested", "quoted": "Quote sent", "answered": "Answered", "approved": "Approved",
                       "rejected": "Declined", "closed": "Closed"}
QuoteManualStatuses = ("new", "quoted", "answered", "rejected", "closed")   # "approved" only by the customer (it creates the order)
# What a customer reads when an order or quote request includes a product hidden from customers (an Admin's preview, on a
# test account): its address must be a test address too, so the order or request is a Test record (p3/testrecords.py)
PreviewTestAddressError = ("Admin preview: an order or quote request for a product hidden from customers must use a test address "
                           "(a reserved test domain, such as example.com), so it stays a Test record.")


def QuoteMaterialProblem(Ctx: Context, Product: str, MaterialId: str) -> str | None:
    """Why this material can't be quoted for this product, or None: only a Quote Only metal is quoted
    (p3/materialstatus.py, WP3). An available one is ordered from Customize at its price, never quoted — no request,
    and no offer above or below the price table; a Preview Only or hidden one is neither ordered nor quoted."""
    Mat = Ctx.Catalog.Get(MaterialId or "")
    if Mat is None:
        return "This material is not in the catalog."
    if Product == Products.Charm and not CharmPrices.IsOffered(Ctx.Catalog, Mat.Id):
        return "This material is not offered for charms."
    Status, Label = MaterialStatus.Of(Ctx, Product, Mat.Id), CharmPrices.MaterialLabel(Ctx, Product, Mat.Id)
    if Status == MaterialStatus.Available:
        return f"{Label} has a fixed price: it is ordered from Customize, not quoted."
    if Status == MaterialStatus.Preview:
        return f"{Label} is preview only: it is not available to order or quote."
    if Status == MaterialStatus.Hidden:
        return f"{Label} is no longer offered."
    return None


def MaterialProblems(Ctx: Context, Product: str, Status: str) -> str:
    """Why a bag line in a metal that is not Available is not ordered (WP3): it stays in the bag."""
    Noun = Products.Plurals[Product]
    if Status == MaterialStatus.Quote:                       # gold, as always: a quote request instead
        return f"Gold {Noun.lower()} are quoted individually — remove this line and use Request a quote."
    if Status == MaterialStatus.Preview:
        return MaterialStatus.PreviewText(Ctx, Product)
    return MaterialStatus.HiddenText() + " Remove this line from your bag."


def QuoteOfferProblem(Ctx: Context, R: dict) -> str | None:
    """Why a quote request can't be sent an offer, have its offer approved or become an order now, or None: its product
    must be on offer to customers (Admin → Settings → Products) and its material quotable (QuoteMaterialProblem). The one
    check behind the Admin's Send, the customer's quote page, Approve and the order made from a quote."""
    Product = R.get("product_type") or Products.Ring
    if not Ctx.Products.Available(Product):
        return f"{Products.Plurals[Product]} are not on offer to customers (Admin → Settings → Products)."
    return QuoteMaterialProblem(Ctx, Product, R.get("material_id"))


def QuoteEvent(DbOrConn, RequestId: str, Kind: str, Data: dict | None = None, By: str = "system") -> None:
    """One line of a quote request's history (quote_events), on the database or inside an open transaction."""
    Sql = "INSERT INTO quote_events (request_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)"
    Args = (RequestId, Kind, Dumps(Data or {}), By, Now())
    (DbOrConn.Execute if hasattr(DbOrConn, "Execute") else DbOrConn.execute)(Sql, Args)


class OrderService:
    def __init__(self, Ctx: Context, Customize, Promos, Payment=None, Validator=None, Mailer=None, Production=None):
        self.Ctx = Ctx
        self.Customize = Customize
        self.Promos = Promos
        self.Payment = Payment or Payments.NoPaymentProvider()
        self.Validator = Validator or Addressing.NoValidator()
        self.Mailer = Mailer
        self.Production = Production          # Production3D: the STL for an ordered line at its size/material
        self.Versions = None                  # VersionService (p3/versions.py): a revised STL uploaded for a line
        self.StatusGuard = None               # the Fulfillment bridge: an enrolled order's production statuses are not set here
        self.StatusOptions = None             # … the status moves offered for such an order (only what the server accepts, G1)
        self.StatusApplied = None             # … and what follows a change (an undone cancellation catches up with Fulfillment, G1)
        self.LineGuard = None                 # the Fulfillment bridge: a line in Production changes its 3D version only through the revision workflow
        self.FulfillmentView = None           # the Fulfillment bridge: what the customer sees of an enrolled order (stage, parcels, timeline)
        self.Activations = None               # StlActivations (p3/stl_activation.py): the active file of a result and its bore-corrected revision

    # ── checkout: what the customer sees before ordering ─────────────────
    def AcceptedTerms(self) -> tuple[str | None, str | None]:
        """The Terms version a customer accepts by placing an order now: the archived version ("v3") its "I accept the
        Terms of Service" links to, and that text's SHA-256 — the same identifiers a consent record keeps
        (p3/accounts/consent.py). (None, None) only where the legal texts are not connected (a bare test context): an
        order is never refused for it."""
        try:
            T = self.Ctx.Accounts.Consent.Current()["terms"]
        except Exception:  # noqa: BLE001
            return None, None
        return f"v{T['version']}", T["sha256"]

    def CheckoutInfo(self, Who: Principal, Visible: tuple | None = None, Market=None) -> dict:
        Profile = self.Ctx.Accounts.Profile(Who)
        Name = (Profile.get("name") or "").strip()
        First, _, Last = Name.partition(" ")
        return {
            "customer": {"first_name": First, "last_name": Last.strip(), "email": Profile.get("email") or "", "phone": ""},
            "countries": Addressing.CountryList(),            # with each one's dialling code (the phone field)
            "region_required": sorted(Addressing.RegionRequired), "no_postal_code": sorted(Addressing.NoPostalCode),
            "shipping_options": list(ShippingFor(Market).values()),
            "terms_version": self.AcceptedTerms()[0],
            "payment": {"available": self.Payment.Available, "provider": self.Payment.Name if self.Payment.Available else None,
                        "message": getattr(self.Payment, "Message", "")},
            "address_validation": {"provider": self.Validator.Name if self.Validator.Name != "none" else None},
            "quote": self.Quote(Who, Visible=Visible, Market=Market),
        }

    def _BagLines(self, Who: Principal, Visible: tuple | None = None) -> list[dict]:
        """The bag at today's prices: a line is orderable only with a product on offer (Visible: to this browser; None:
        every product), a metal Available for it (p3/materialstatus.py, WP3), an available price and a standard size. The
        snapshot price is kept for the "repriced" flag only."""
        Out = []
        Statuses = {P: MaterialStatus.Map(self.Ctx, P) for P in Products.All}
        for L in self.Ctx.Db.All("SELECT b.*, c.asset_path, d.title FROM bag_lines b JOIN candidates c ON c.id = b.candidate_id "
                                 "JOIN designs d ON d.id = b.design_id WHERE b.owner_account_id = ? ORDER BY b.created_at", (Who.AccountId,)):
            Mat = self.Ctx.Catalog.Get(L["material_id"])
            Product = L.get("product_type") or Products.Ring
            Q = CharmPrices.QuoteFor(self.Ctx, Product, L["material_id"], L.get("charm_size")) if Mat else None
            Problem = None
            Status = Statuses[Product].get(L["material_id"], MaterialStatus.Hidden) if Mat else MaterialStatus.Hidden
            if Visible is not None and Product not in Visible:      # its product is switched OFF: kept, never ordered
                Problem = Products.UnavailableText
            elif Status != MaterialStatus.Available:                 # kept in the bag, never ordered (WP3)
                Problem = MaterialProblems(self.Ctx, Product, Status)
            elif Product == Products.Charm:
                # a charm's price depends on its size: the size is checked first
                if L["charm_size"] is None:
                    Problem = "Please choose one of the charm sizes."
                elif not self.Ctx.Products.IsValidCharmSize(L["charm_size"]):    # switched off in Admin → Charm sizes
                    Problem = "This charm size is no longer offered — remove this line and add the charm again from Customize."
                elif not Q.IsAvailable:
                    Problem = "The price for this charm in this size and material is not available right now."
            elif not Q.IsAvailable:
                Problem = "The price for this material is not available right now."
            elif L["ring_size"] is None or not self.Ctx.Catalog.IsValidSize(L["ring_size"]):
                Problem = "Please choose a ring size."
            Unit = Q.unit_price if Q and Q.IsAvailable else None
            Out.append({"bag_line_id": L["id"], "design_id": L["design_id"], "candidate_id": L["candidate_id"],
                        "customization_id": L["customization_id"], "title": L["title"], "image_path": L["asset_path"],
                        "image_url": self.Ctx.AssetUrl(L["asset_path"]), "material_id": L["material_id"],
                        "material_label": CharmPrices.MaterialLabel(self.Ctx, Product, L["material_id"]), "ring_size": L["ring_size"],
                        "product_type": Product, "charm_size": L.get("charm_size"),
                        "size_label": Products.SizeLabel(Product, L["ring_size"], L.get("charm_size")),   # R-02: "14 mm" in Checkout
                        "quantity": L["quantity"], "unit_price": Unit, "currency": Q.currency if Q else L["currency"],
                        "pricing_version": Q.pricing_version if Q else None,
                        "line_total": round(Unit * L["quantity"], 2) if Unit is not None else None,
                        "repriced": Unit is not None and round(Unit, 2) != round(L["unit_price"], 2),
                        "snapshot_unit_price": L["unit_price"], "problem": Problem,
                        # a promotional price (p3/charmprices.py): the whole record, kept with the order line; the
                        # customer's answer carries its public part only (Quote)
                        "_promotion": Q.promotion if Q and Q.IsAvailable and Q.promotion else None})
        return Out

    def _VerifiedEmail(self, Who: Principal) -> str | None:
        """The caller's verified email, for a promo code meant for one customer; None when unknown."""
        Get = getattr(self.Ctx.Accounts, "VerifiedEmail", None)
        try:
            return Get(Who.AccountId) if Get and Who is not None else None
        except Exception:  # noqa: BLE001 — an unknown account simply has no verified email
            return None

    def Quote(self, Who: Principal, PromoCode: str | None = None, ShippingMethod: str = "standard",
              Visible: tuple | None = None, Market=None, Internal: bool = False) -> dict:
        """The bag's totals. Market (B6): ₪ including VAT — each line's unit price converted from its USD list price and
        rounded by the rule, the delivery converted the same way, a promo checked on the USD lines (its rules and minimum
        are in USD) and its discount taken from the ₪ lines. None: USD, as before. A line at a promotional price carries
        its promotion (its regular price and saving converted too); a promo code never discounts such a line (no double
        discount, P1 12 Nov 2026). Internal: the whole promotion record stays on the lines (an order keeps it)."""
        Lines = self._BagLines(Who, Visible)
        Refs = RingIds.CandidateRefs(self.Ctx.Db, list({L["design_id"] for L in Lines}))
        for L in Lines:
            L["ring_id"] = Refs.get(L["candidate_id"])
        Ok = [L for L in Lines if not L["problem"]]
        Offered = ShippingFor(Market)
        Ship = Offered.get(ShippingMethod or DefaultShipping) or Offered[DefaultShipping]   # Express retired: Standard
        Promo, PromoError_ = None, None
        if PromoCode and str(PromoCode).strip():
            if not Ok:
                PromoError_ = {"code": "promo_no_items", "message": "Add a piece to your bag before using a promo code."}
            else:
                try:
                    Promo = self.Promos.Evaluate(PromoCode, Ok, AccountEmail=self._VerifiedEmail(Who), Market=Market)
                except HttpError as E:
                    PromoError_ = {"code": E.Code, "message": E.Message}
        if Market:
            for L in Lines:
                if L["unit_price"] is not None and L["currency"] == "USD":
                    L["usd_unit_price"], L["unit_price"], L["currency"] = L["unit_price"], Market.Unit(L["unit_price"]), Market.currency
                    L["line_total"] = round(L["unit_price"] * L["quantity"], 2)
                    if L["_promotion"]:
                        L["_promotion"] = IlsPricingMod.LocalizePromotion(L["_promotion"], L["unit_price"], Market)
            if Promo:
                Share = Promo["eligible_subtotal"] / max(sum(L["usd_unit_price"] * L["quantity"] for L in Ok), 0.01)
                Eligible = round(sum(L["line_total"] for L in Ok) * Share, 2)        # the eligible lines' share, in ₪
                Discount = round(Eligible * Promo["value"] / 100.0, 2) if Promo["kind"] == "percent" else min(Market.Plain(Promo["value"]), Eligible)
                Promo = {**Promo, "usd_discount": Promo["discount"], "discount": Discount,
                         "label": Promo["label"] if Promo["kind"] == "percent"
                         else f"{'₪' if Market.currency == 'ILS' else '$'}{Discount:,.2f} off"}
        for L in Lines:                       # each line's promotion as the customer reads it; the whole record only inside
            L["promotion"] = PublicPromotion(L["_promotion"])
            if not Internal:
                del L["_promotion"]
        Subtotal = round(sum(L["line_total"] for L in Ok), 2)
        Currency = next((L["currency"] for L in Ok), Market.currency if Market else "USD")
        Discount = Promo["discount"] if Promo else 0.0
        Shipping = Ship["price"] if Ship else 0.0
        Total = round(max(0.0, Subtotal - Discount) + Shipping, 2)
        return {"lines": Lines, "count": sum(L["quantity"] for L in Ok), "currency": Currency,
                **({"incl_vat": True, "vat_amount": Market.VatOf(Total), "vat_pct": float(Market.vat_pct)} if Market else {}),
                "subtotal": Subtotal, "discount": round(Discount, 2), "promo": Promo, "promo_error": PromoError_,
                "shipping_method": Ship["id"] if Ship else None, "shipping": Shipping,
                "total": Total,
                # a line that can't be ordered is left out (shown with its problem) and never blocks the rest (WP2)
                "ok": bool(Ok) and Ship is not None, "excluded": len(Lines) - len(Ok),
                "repriced": any(L["repriced"] for L in Ok)}

    # ── validation ───────────────────────────────────────────────────────
    @staticmethod
    def ValidateCustomer(Raw: dict | None) -> tuple[dict, list[dict]]:
        Raw = Raw or {}
        C = {K: str(Raw.get(K) or "").strip() for K in ("first_name", "last_name", "email", "phone")}
        P = []
        if not C["first_name"]:
            P.append({"field": "first_name", "message": "Please enter your first name."})
        if not C["last_name"]:
            P.append({"field": "last_name", "message": "Please enter your last name."})
        if not EmailPattern.match(C["email"]):
            P.append({"field": "email", "message": "Please enter a valid email address."})
        if not PhonePattern.match(C["phone"]) or len(PhoneDigits.findall(C["phone"])) < 7:
            P.append({"field": "phone", "message": "Please enter a phone number we can reach you on."})
        return C, P

    def ValidateAddress(self, Raw: dict | None) -> dict:
        """Field checks, then the (pluggable) validation service. A 'corrected' result carries a
        suggestion the customer confirms in the browser; nothing is replaced silently."""
        A = Addressing.Normalize(Raw)
        P = Addressing.Problems(A)
        Result = {"status": "unverified", "suggestion": None, "message": ""} if P else self.Validator.Check(A)
        return {"address": A, "problems": P, "lines": Addressing.Lines(A) if not P else [],
                "validation": {**Result, "provider": self.Validator.Name if self.Validator.Name != "none" else None}}

    # ── place an order ───────────────────────────────────────────────────
    @staticmethod
    def _BagChanged(Body: dict, Ordered: list[dict], Total: float) -> bool:
        """The bag changed after Checkout showed it (WP2): the lines the page showed as ordered (expected_lines) or its
        total (expected_total) differ from what would be ordered now — the customer reviews it again. Not sent: not
        checked (older pages)."""
        Lines = Body.get("expected_lines")
        if isinstance(Lines, list) and sorted(str(X) for X in Lines) != sorted(L["bag_line_id"] for L in Ordered):
            return True
        Expected = Body.get("expected_total")
        if Expected is None:
            return False
        try:
            return round(float(Expected), 2) != round(float(Total), 2)
        except (TypeError, ValueError):
            return True

    def Create(self, Who: Principal, Body: dict, SendMail=None, Visible: tuple | None = None, Origin: str = "",
               Market=None) -> dict:
        """Market (B6): how the order is priced, decided by its delivery country (₪ including VAT for Israel). The order
        keeps the ₪ figures, the VAT inside its total and how they were reached (fx_json). None: USD, as before."""
        Db = self.Ctx.Db
        Rid = (Body.get("client_request_id") or "").strip() or None
        if Rid:
            Existing = Db.One("SELECT id FROM orders WHERE owner_account_id = ? AND client_request_id = ?", (Who.AccountId, Rid))
            if Existing:
                return self.Get(Who, Existing["id"])
        Customer, Problems = self.ValidateCustomer(Body.get("customer"))
        Addr = self.ValidateAddress(Body.get("address"))
        Problems += Addr["problems"]
        Method = str(Body.get("shipping_method") or DefaultShipping)
        if Method not in ShippingOptions:
            Problems.append({"field": "shipping_method", "code": "shipping_method_unavailable",
                             "message": RetiredShippingText if Method in ShippingMethods else "Please choose a delivery option."})
        if Body.get("terms_accepted") is not True:
            Problems.append({"field": "terms_accepted", "message": "Please accept the Terms of Service to place the order."})
        if Problems:
            raise _Invalid(Problems)
        Q = self.Quote(Who, Body.get("promo_code"), Method, Visible, Market, Internal=True)
        if not Q["lines"]:
            raise HttpError(409, "bag_empty", "Your bag is empty.")
        if Q["promo_error"]:
            raise HttpError(400, Q["promo_error"]["code"], Q["promo_error"]["message"])
        if not Q["ok"]:
            Bad = next(L for L in Q["lines"] if L["problem"])
            raise HttpError(409, "bag_not_orderable", Bad["problem"])
        # The order holds the lines that can be ordered now; the others stay in the bag (WP2)
        Ordered = [L for L in Q["lines"] if not L["problem"]]
        if self._BagChanged(Body, Ordered, Q["total"]):
            raise HttpError(409, "bag_changed", "Your bag changed while you were checking out — please review it and place the order again.")
        # A line of a product hidden from customers is orderable only in an Admin's preview on a test account: the order
        # is a Test order too (its address at a reserved test domain), never a real one
        Hidden = [L for L in Ordered if L["product_type"] not in self.Ctx.Products.AvailableProducts()]
        if Hidden and not ReservedEmail(Customer["email"]):
            raise HttpError(409, "preview_test_address", PreviewTestAddressError)
        if len(Ordered) > MaxLines:
            raise HttpError(409, "bag_too_large", f"An order can hold up to {MaxLines} lines.")
        # The validation service's verdict travels with the order; a confirmed suggestion replaces the address.
        Validation = Addr["validation"]
        Address = Addr["address"]
        if Body.get("use_suggested_address") and Validation.get("suggestion"):
            Address, Validation = Addressing.Normalize(Validation["suggestion"]), {**Validation, "status": "corrected"}
        Status = Validation.get("status") or "unverified"
        if Status not in ("unverified", "verified", "corrected", "failed"):
            Status = "unverified"
        Earlier = None if Body.get("confirm_duplicate") is True else self._Repeats(Who, Ordered, Address)
        if Earlier:
            raise HttpError(409, "duplicate_order",
                            f"You already placed this order — the same pieces to the same address — as {Earlier['ref']}, "
                            f"{Earlier['when']}. Place it again only if you want a second, identical order.",
                            Details={"existing_order": {"id": Earlier["id"], "ref": Earlier["ref"]}})
        OrderId, T = NewId("ord"), Now()
        TermsV, TermsSha = self.AcceptedTerms()             # what "I accept the Terms of Service" links to now (M6)
        # The customer's note from Checkout ("Anything we should know?", R-45): kept on the order, as a quote's is
        Note = str(Body.get("customer_note") or "").strip()[:1000]
        Promo = Q["promo"]
        PromoSnapshot = {**{K: Promo[K] for K in ("code", "kind", "value", "discount", "label")}, "original_amount": Q["subtotal"],
                         "final_amount": round(Q["subtotal"] - Promo["discount"], 2)} if Promo else None
        # Each line is pinned to the 3D version its option is made from right now (the design's current one — LIFO or the
        # Admin's choice); a later version never changes it. A design without a model yet is pinned when it is prepared.
        Pins = {L["bag_line_id"]: (Versions.MeshFor(Db, L["design_id"], L["candidate_id"]) or {}).get("id") for L in Ordered}
        with Db.Transaction() as Conn:
            Conn.execute("INSERT INTO orders (id, owner_account_id, status, payment_status, customer_json, shipping_json, "
                         "address_validation, shipping_method, currency, subtotal, discount, shipping, total, promo_code, promo_json, "
                         "terms_version, terms_sha256, terms_accepted_at, client_request_id, notes, created_at, updated_at, "
                         "vat_amount, fx_json) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (OrderId, Who.AccountId, "new", "pending", Dumps(Customer),
                          Dumps({**Address, "validation": {K: V for K, V in Validation.items() if K != "suggestion"},
                                 "suggestion": Validation.get("suggestion")}),
                          Status, Method, Q["currency"], Q["subtotal"], Q["discount"], Q["shipping"], Q["total"],
                          Promo["code"] if Promo else None, Dumps(PromoSnapshot) if PromoSnapshot else None,
                          TermsV, TermsSha, T, Rid, f"Customer's note: {Note}" if Note else None, T, T,
                          Q.get("vat_amount"), Dumps({**Market.Record(), "net_total": round(Q["total"] - Q["vat_amount"], 2),
                                                      "vat_amount": Q["vat_amount"]}) if Market else None))
            for N, L in enumerate(Ordered, start=1):
                Conn.execute("INSERT INTO order_lines (id, order_id, position, design_id, candidate_id, bag_line_id, customization_id, "
                             "title, ring_id, product_type, material_id, material_label, ring_size, charm_size, quantity, unit_price, "
                             "line_total, currency, pricing_version, image_path, purchase_json, mesh_id, usd_unit_price) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (NewId("oln"), OrderId, N, L["design_id"], L["candidate_id"], L["bag_line_id"], L["customization_id"],
                              L["title"], L["ring_id"], L["product_type"], L["material_id"], L["material_label"],
                              L["ring_size"] if L["product_type"] == Products.Ring else None,
                              L["charm_size"] if L["product_type"] == Products.Charm else None, L["quantity"],
                              L["unit_price"], L["line_total"], L["currency"], L["pricing_version"], L["image_path"],
                              Dumps(PurchaseSnapshot(L)), Pins.get(L["bag_line_id"]), L.get("usd_unit_price")))
                Conn.execute("DELETE FROM bag_lines WHERE id = ? AND owner_account_id = ?", (L["bag_line_id"], Who.AccountId))
            if Promo:
                self.Promos.Redeem(Conn, Promo["promo_id"])
            Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                         (OrderId, "placed", Dumps({"total": Q["total"], "lines": len(Ordered), "promo": Promo["code"] if Promo else None}),
                          "customer", T))
        # Payment through the adapter: today "pending" with a plain message — never a faked success.
        Order = Db.One("SELECT * FROM orders WHERE id = ?", (OrderId,))
        Pay = self.Payment.Begin(Order)
        PayStatus = Pay["status"] if Pay["status"] in Payments.Statuses else "pending"
        Db.Execute("UPDATE orders SET payment_status = ?, payment_provider = ?, payment_ref = ?, payment_json = ?, updated_at = ? WHERE id = ?",
                   (PayStatus, self.Payment.Name if self.Payment.Available else None, Pay.get("ref"),
                    Dumps({"message": Pay.get("message"), "client": Pay.get("client")}), Now(), OrderId))
        if PayStatus == "paid":
            self._SetStatus(OrderId, "payment_confirmed", self.Payment.Name, "Paid through the provider")
        Ref = OrderRef(Db.One("SELECT order_no FROM orders WHERE id = ?", (OrderId,))["order_no"])
        for L in Ordered:
            Size = {"product_type": Products.Charm, "charm_size": L["charm_size"]} if L["product_type"] == Products.Charm \
                else {"ring_size": L["ring_size"]}
            Sessions.Record(self.Ctx, Who.AccountId, "order_placed", L["design_id"], order_id=OrderId, order_ref=Ref,
                            candidate_id=L["candidate_id"], ring_id=L["ring_id"], material_id=L["material_id"],
                            **Size, quantity=L["quantity"], unit_price=L["unit_price"],
                            line_total=L["line_total"], currency=L["currency"], promo=Promo["code"] if Promo else None)
        Out = self.Get(Who, OrderId)
        self._Email(Out, SendMail, Origin)
        return Out

    @staticmethod
    def _LineKey(L: dict) -> tuple:
        """What makes two order lines the same piece: design, option, customization, metal, size and quantity."""
        Num = lambda V: None if V is None else round(float(V), 3)
        Charm = L.get("product_type") == Products.Charm
        return (L.get("design_id") or "", L.get("candidate_id") or "", L.get("customization_id") or "", L.get("material_id") or "",
                None if Charm else Num(L.get("ring_size")), Num(L.get("charm_size")) if Charm else None, int(L.get("quantity") or 0))

    @staticmethod
    def _AddressKey(A: dict) -> tuple:
        Same = lambda V: " ".join(str(V or "").split()).casefold()
        return tuple(Same(A.get(K)) for K in ("recipient", "line1", "city", "postal_code", "country"))

    def _Repeats(self, Who: Principal, Ordered: list[dict], Address: dict) -> dict | None:
        """The customer's own order of the last RepeatHours that this one would repeat — the same pieces, metals, sizes and
        quantities to the same address, and not cancelled — as {"id", "ref", "when"}; None when there is none."""
        Since = (datetime.now(timezone.utc) - timedelta(hours=RepeatHours)).isoformat(timespec="milliseconds")
        Lines, Where = sorted(self._LineKey(L) for L in Ordered), self._AddressKey(Address)
        Db = self.Ctx.Db
        for O in Db.All("SELECT id, order_no, shipping_json, created_at FROM orders WHERE owner_account_id = ? AND status != 'cancelled' "
                        "AND created_at >= ? ORDER BY created_at DESC", (Who.AccountId, Since)):
            if self._AddressKey(json.loads(O["shipping_json"] or "{}")) != Where:
                continue
            Theirs = Db.All("SELECT design_id, candidate_id, customization_id, material_id, product_type, ring_size, charm_size, quantity "
                            "FROM order_lines WHERE order_id = ?", (O["id"],))
            if sorted(self._LineKey(L) for L in Theirs) == Lines:
                Minutes = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(O["created_at"])).total_seconds() // 60))
                When = ("a moment ago" if Minutes < 1 else f"{Minutes} minute{'s' if Minutes > 1 else ''} ago" if Minutes < 60
                        else f"about {Minutes // 60} hour{'s' if Minutes >= 120 else ''} ago")
                return {"id": O["id"], "ref": OrderRef(O["order_no"]), "when": When}
        return None

    def _Email(self, Order: dict, SendMail, Origin: str = "") -> None:
        """Origin: the public site the customer ordered on (P3_PUBLIC_BASE_URL first): the email's pictures and its View Your
        Order button (My Account) point there."""
        if self.Mailer is None or not Order["customer"]["email"]:
            return
        from p3.mail import OrderConfirmationEmail, StaffOrderEmail, Trace as MailTrace
        Origin = (self.Ctx.Settings.PublicBaseUrl or Origin or "").rstrip("/")
        Site = f"{Origin}{self.Ctx.Settings.BasePath or ''}" if Origin else ""
        Subject, Body = OrderConfirmationEmail(Order, Origin, OrderPageUrl(Site, Order["ref"]), Site=Site)
        To = Order["customer"]["email"]
        Staff = tuple(getattr(self.Ctx.Settings, "StaffNotifyEmails", ()) or ())
        Test = ReservedEmail(To)

        def Event(Kind: str, Data: dict) -> None:
            self.Ctx.Db.Execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                                (Order["id"], Kind, Dumps(Data), "system", Now()))

        def Send():
            if Test:                                             # a synthetic order: the customer email is never sent (p3/testrecords.py)
                Event("email_skipped", {"to": To, "reason": "test order: the address is at a reserved test domain"})
            else:
                try:
                    Mid = self.Mailer.Send(To, Subject, Body)
                    Event("email", {"to": To, "subject": Subject, "delivery": self.Mailer.Mode, **MailTrace(self.Mailer, Mid)})
                except Exception as E:  # noqa: BLE001 — an email problem never undoes an order
                    Event("email_failed", {"to": To, "error": str(E)[:200]})
            if Staff:                                            # the people who act on a new order (P3_STAFF_NOTIFY_EMAILS)
                StaffSubject, StaffBody = StaffOrderEmail(Order)
                StaffSubject = ("[TEST] " if Test else "") + StaffSubject
                for Address in Staff:
                    try:
                        self.Mailer.Send(Address, StaffSubject, StaffBody)
                        Event("staff_notified", {"to": Address})
                    except Exception as E:  # noqa: BLE001
                        Event("staff_notify_failed", {"to": Address, "error": str(E)[:200]})
        (SendMail or (lambda Fn: Fn()))(Send)

    def EmailState(self, OrderId: str, To: str | None) -> dict:
        """What the customer may be told about their confirmation email: sent (left the server through SMTP),
        pending (about to be sent), failed, not_sent (no mail delivery is configured — the outbox keeps a copy) or
        not_configured. Never "sent" unless it was."""
        if self.Mailer is None or not To:
            return {"status": "not_configured", "to": To}
        if ReservedEmail(To):                                    # a test order: no customer email, ever (p3/testrecords.py)
            return {"status": "skipped", "to": To, "test": True}
        if self.Mailer.Mode != "smtp":
            return {"status": "not_sent", "to": To}
        Kinds = [E["kind"] for E in self.Ctx.Db.All("SELECT kind FROM order_events WHERE order_id = ? AND kind IN ('email', 'email_failed') "
                                                     "ORDER BY id", (OrderId,))]
        return {"status": "sent" if "email" in Kinds else "failed" if "email_failed" in Kinds else "pending", "to": To}

    # ── reading ──────────────────────────────────────────────────────────
    def _Lines(self, OrderId: str) -> list[dict]:
        return self.Ctx.Db.All("SELECT * FROM order_lines WHERE order_id = ? ORDER BY position", (OrderId,))

    def ToJson(self, O: dict, Lines: list[dict], ForCustomer: bool = True) -> dict:
        Ship = ShippingMethods.get(O["shipping_method"], {})          # an order keeps the method it was placed with
        Shipping = json.loads(O["shipping_json"])
        Pay = json.loads(O["payment_json"] or "{}")
        Out = {
            "id": O["id"], "ref": OrderRef(O["order_no"]), "created_at": O["created_at"], "updated_at": O["updated_at"],
            "status": O["status"], "status_label": StatusLabels.get(O["status"], O["status"]),
            "payment_status": O["payment_status"], "payment_label": Payments.Labels.get(O["payment_status"], O["payment_status"]),
            "payment_message": Pay.get("message") or "", "payment_provider": O["payment_provider"],
            "customer": json.loads(O["customer_json"]),
            # A synthetic order (p3/testrecords.py) or one an Admin marked Test (2026-10-16): marked Test, out of the figures,
            # its customer never emailed by Fulfillment's milestones; Fulfillment ships it like any Test order. The
            # customer's own view stays the address's verdict: nothing of the marking reaches the customer
            "test": IsTestCustomer(json.loads(O["customer_json"])) or (not ForCustomer and bool(O.get("test_marked_at"))),
            "address": {K: Shipping.get(K, "") for K in Addressing.Fields}, "address_lines": Addressing.Lines(Shipping),
            "address_validation": O["address_validation"], "address_validation_detail": Shipping.get("validation"),
            "shipping_method": O["shipping_method"], "shipping_label": Ship.get("label"), "shipping_eta": Ship.get("eta"),
            "currency": O["currency"], "subtotal": O["subtotal"], "discount": O["discount"], "shipping": O["shipping"], "total": O["total"],
            # an order in ₪ (B6): the VAT inside its total; a USD order has none and no such fields
            **({"incl_vat": True, "vat_amount": O["vat_amount"]} if O.get("vat_amount") is not None else {}),
            **({"fx": json.loads(O["fx_json"])} if O.get("fx_json") and not ForCustomer else {}),
            "promo_code": O["promo_code"], "promo": json.loads(O["promo_json"]) if O["promo_json"] else None,
            "terms_version": O["terms_version"], "terms_sha256": O.get("terms_sha256"), "terms_accepted_at": O["terms_accepted_at"],
            "confirmation_email": self.EmailState(O["id"], json.loads(O["customer_json"]).get("email")),
            "count": sum(L["quantity"] for L in Lines),
            # Which products the order holds — both for a mixed order
            "product_types": sorted({L.get("product_type") or Products.Ring for L in Lines}, key=Products.All.index),
            "lines": [{"id": L["id"], "design_id": L["design_id"], "candidate_id": L["candidate_id"], "title": L["title"],
                       "ring_id": L["ring_id"], "material_id": L["material_id"], "material_label": L["material_label"],
                       "product_type": L.get("product_type") or Products.Ring, "charm_size": L.get("charm_size"),
                       "size_label": Products.SizeLabel(L.get("product_type") or Products.Ring, L["ring_size"], L.get("charm_size")),
                       "ring_size": L["ring_size"], "quantity": L["quantity"], "unit_price": L["unit_price"],
                       "line_total": L["line_total"], "currency": L["currency"], "image_url": self.Ctx.AssetUrl(L["image_path"]),
                       # the promotional price it was ordered at, as recorded then (P1): never today's promotion
                       **({"promotion": P} if (P := LinePromotion(L, Full=not ForCustomer)) else {})}
                      for L in Lines],
        }
        if not ForCustomer:
            Out["owner_account_id"] = O["owner_account_id"]
            Out["notes"] = O["notes"]
            Out["payment_ref"] = O["payment_ref"]
            Out["client_request_id"] = O["client_request_id"]
            # who marked it Test and why (Admin only; the customer sees nothing of it — 2026-10-16)
            Out["test_marked"] = {"at": O["test_marked_at"], "by": O.get("test_marked_by"), "reason": O.get("test_reason")} if O.get("test_marked_at") else None
            for J, L in zip(Out["lines"], Lines):          # the 3D result and version the line is pinned to (Admin only)
                J["pinned_three_d_id"], J["pinned_mesh_id"] = L.get("three_d_id"), L.get("mesh_id")
        return Out

    def Get(self, Who: Principal, OrderId: str) -> dict:
        """One of the customer's own orders, by its internal id or its Order ID (ORD-10016: the address of the order's
        page and of the email's View Your Order button, B7). Another account's order and an unknown one give the same
        answer, so an Order ID reveals nothing."""
        M = _OrderRefPattern.fullmatch((OrderId or "").strip().upper())
        O = self.Ctx.Db.One("SELECT * FROM orders WHERE (id = ? OR order_no = ?) AND owner_account_id = ?",
                            (OrderId, int(M.group(1)) if M else -1, Who.AccountId))
        if O is None:
            raise HttpError(404, "order_not_found", "Order not found.")
        return self._ForCustomer(O)

    def List(self, Who: Principal) -> list[dict]:
        return [self._ForCustomer(O) for O in
                self.Ctx.Db.All("SELECT * FROM orders WHERE owner_account_id = ? ORDER BY created_at DESC LIMIT 50", (Who.AccountId,))]

    def _ForCustomer(self, O: dict) -> dict:
        """An order as its customer sees it in My Account: the order, its statuses with their dates (never the Admin's
        notes) and the quote it came from."""
        J = self.ToJson(O, self._Lines(O["id"]))
        History, PaidAt = [], None
        for E in self.Ctx.Db.All("SELECT kind, data_json, created_at FROM order_events WHERE order_id = ? AND kind IN ('placed', 'status', 'payment') "
                                 "ORDER BY id", (O["id"],)):
            D = json.loads(E["data_json"] or "{}")
            if E["kind"] == "payment":                       # when the payment was recorded (the Admin's entry or the provider's)
                PaidAt = E["created_at"] if D.get("to") == "paid" else None
                continue
            To = "new" if E["kind"] == "placed" else D.get("to")
            History.append({"status": To, "label": "Order placed" if E["kind"] == "placed" else StatusLabels.get(To, To), "at": E["created_at"]})
        J["status_history"] = History
        Quote = self.Ctx.Db.One("SELECT request_no FROM quote_requests WHERE order_id = ?", (O["id"],))
        J["quote_ref"] = QuoteRef(Quote["request_no"]) if Quote else None
        # With Fulfillment (docs/FULFILLMENT.md, Batch 8): the stage, the parcels and the milestones of an enrolled order; nothing for the others
        View = self.FulfillmentView(O["id"]) if self.FulfillmentView is not None else None
        if View:
            J["fulfillment"] = View
            J["fulfillment_stage"] = View.get("stage")
        PaidAt = PaidAt if O["payment_status"] == "paid" else None
        J["paid_at"] = PaidAt
        J["customer_note"] = CustomerNote(O.get("notes"))
        J.update(CustomerView(J, History, PaidAt))
        return J

    def ListQuotes(self, Who: Principal) -> list[dict]:
        """The customer's own quote requests, newest first, with what they were told: the request, each quote sent
        (amount, validity), their decision and the order it became, the status changes. Internal notes, staff emails,
        the quote link and the weight behind the price stay in the Admin."""
        Out = []
        for R in self.Ctx.Db.All("SELECT * FROM quote_requests WHERE owner_account_id = ? ORDER BY created_at DESC LIMIT 50", (Who.AccountId,)):
            J = self.QuoteRequestJson(R)
            History = []
            for E in self.Ctx.Db.All("SELECT kind, data_json, created_at FROM quote_events WHERE request_id = ? "
                                     "AND kind IN ('created', 'offer_sent', 'approved', 'rejected', 'status') ORDER BY id", (R["id"],)):
                D, At = json.loads(E["data_json"] or "{}"), E["created_at"]
                if E["kind"] == "created":
                    History.append({"kind": "requested", "label": "Quote requested", "at": At})
                elif E["kind"] == "offer_sent":
                    History.append({"kind": "quote_sent", "label": "Revised quote sent" if D.get("revised") else "Quote sent", "at": At,
                                    "version": D.get("version"), "total": D.get("total"), "valid_until": D.get("valid_until")})
                elif E["kind"] == "approved":
                    History.append({"kind": "approved", "label": "You approved the quote", "at": At, "order_ref": D.get("order_ref")})
                elif E["kind"] == "rejected":
                    History.append({"kind": "declined", "label": "You declined the quote", "at": At})
                elif D.get("to") in CustomerQuoteLabels:
                    History.append({"kind": "status", "label": CustomerQuoteLabels[D["to"]], "at": At})
            if not any(H["kind"] == "requested" for H in History):          # a request from before its history was kept
                History.insert(0, {"kind": "requested", "label": "Quote requested", "at": R["created_at"]})
            Out.append({**{K: J[K] for K in ("id", "ref", "design_id", "title", "ring_id", "material_label", "product_type", "size_label",
                                             "quantity", "image_url", "created_at", "order_id", "order_ref", "offer_expired")},
                        "status": R["status"],
                        "status_label": "Quote expired" if J["offer_expired"] else CustomerQuoteLabels.get(R["status"], R["status"]),
                        "offer": J["offer"] and {K: J["offer"].get(K) for K in ("version", "unit_price", "quantity", "total", "valid_until",
                                                                                 "sent_at", "size_label")},
                        "history": History})
        return Out

    # ── gold: request a quote instead of ordering ────────────────────────
    def RequestQuote(self, Who: Principal, Body: dict, SendMail=None, Visible: tuple | None = None) -> dict:
        Db = self.Ctx.Db
        Customer, Problems = self.ValidateCustomer(Body.get("customer"))
        if Problems:
            raise _Invalid(Problems)
        DesignId, CandidateId = str(Body.get("design_id") or ""), str(Body.get("candidate_id") or "")
        D = self.Customize.Images.RequireDesign(Who, DesignId)
        Product = D.get("product_type") or Products.Ring
        if Visible is not None:                                       # a quote is an order step: not while switched OFF
            Products.RequireOffered(Product, Visible)
        if Product not in self.Ctx.Products.AvailableProducts() and not ReservedEmail(Customer["email"]):
            raise HttpError(409, "preview_test_address", PreviewTestAddressError)    # an Admin's preview: a Test request only
        C = self.Customize.Images.RequireReadyCandidate(DesignId, CandidateId)
        Mat = self.Ctx.Catalog.Get(str(Body.get("material_id") or ""))
        if Mat is None:
            raise HttpError(400, "unknown_material", "Unknown material.")
        Charm = Product == Products.Charm
        if Charm:
            if not CharmPrices.IsOffered(self.Ctx.Catalog, Mat.Id):
                raise HttpError(400, "material_not_offered", "This material is not offered for charms.")
            Size = Body.get("charm_size")
            if Size is not None and not self.Ctx.Products.IsValidCharmSize(Size):
                raise HttpError(400, "invalid_charm_size", "Please choose one of the charm sizes.")
        else:
            Size = Body.get("ring_size")
            if Size is not None and not self.Ctx.Catalog.IsValidSize(Size):
                raise HttpError(400, "invalid_ring_size", "Please choose a standard ring size.")
        Problem = QuoteMaterialProblem(self.Ctx, Product, Mat.Id)
        if Problem:                                                   # a fixed-price material is ordered, never quoted
            raise HttpError(409, "not_quotable", Problem)
        Qty = Body.get("quantity", 1)
        if not isinstance(Qty, int) or isinstance(Qty, bool) or not 1 <= Qty <= 10:
            raise HttpError(400, "invalid_quantity", "Quantity must be between 1 and 10.")
        Id, T = NewId("qrq"), Now()
        Ring = RingIds.CandidateRef(Db, CandidateId)
        if Charm:
            Db.Execute("INSERT INTO quote_requests (id, owner_account_id, design_id, candidate_id, title, ring_id, material_id, "
                       "material_label, ring_size, product_type, charm_size, quantity, customer_json, message, status, created_at, "
                       "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (Id, Who.AccountId, DesignId, CandidateId, D["title"], Ring, Mat.Id, CharmPrices.Label(self.Ctx.Catalog, Mat.Id),
                        None, Products.Charm, float(Size) if Size is not None else None, Qty, Dumps(Customer),
                        str(Body.get("message") or "")[:1000], "new", T, T))
        else:
            Db.Execute("INSERT INTO quote_requests (id, owner_account_id, design_id, candidate_id, title, ring_id, material_id, material_label, "
                       "ring_size, quantity, customer_json, message, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (Id, Who.AccountId, DesignId, CandidateId, D["title"], Ring, Mat.Id, Mat.Label,
                        float(Size) if Size is not None else None, Qty, Dumps(Customer), str(Body.get("message") or "")[:1000], "new", T, T))
        Sessions.Record(self.Ctx, Who.AccountId, "quote_requested", DesignId, request_id=Id, candidate_id=CandidateId,
                        ring_id=Ring, material_id=Mat.Id,
                        **({"product_type": Products.Charm, "charm_size": Size} if Charm else {"ring_size": Size}), quantity=Qty)
        QuoteEvent(Db, Id, "created", {"message": str(Body.get("message") or "")[:1000], "quantity": Qty, "material_id": Mat.Id,
                                        "size": float(Size) if Size is not None else None}, "customer")
        Out = self.QuoteRequestJson(Db.One("SELECT * FROM quote_requests WHERE id = ?", (Id,)), C)
        # What the customer may be told: "sent" only when mail really leaves the server (SMTP); the outbox keeps a copy
        Test = ReservedEmail(Customer.get("email"))
        Out["email_status"] = "not_configured" if self.Mailer is None else ("skipped" if Test else "pending" if self.Mailer.Mode == "smtp" else "not_sent")
        if self.Mailer is not None:
            from p3.mail import QuoteRequestEmail, StaffQuoteEmail, Trace as MailTrace
            Subject, Html = QuoteRequestEmail(Out)
            Staff = tuple(getattr(self.Ctx.Settings, "StaffNotifyEmails", ()) or ())
            Mode = self.Mailer.Mode

            def Send():
                if Test:                                         # a synthetic request: the customer email is never sent (p3/testrecords.py)
                    QuoteEvent(Db, Id, "email_skipped", {"type": "request_confirmation", "to": Customer["email"], "reason": "test request: reserved test domain"})
                else:
                    try:
                        Mid = self.Mailer.Send(Customer["email"], Subject, Html)
                        QuoteEvent(Db, Id, "email", {"type": "request_confirmation", "to": Customer["email"], "delivery": Mode,
                                                     **MailTrace(self.Mailer, Mid)})
                    except Exception as E:  # noqa: BLE001
                        QuoteEvent(Db, Id, "email_failed", {"type": "request_confirmation", "to": Customer["email"], "error": str(E)[:200]})
                if Staff:
                    StaffSubject, StaffHtml = StaffQuoteEmail(Out)
                    StaffSubject = ("[TEST] " if Test else "") + StaffSubject
                    for Address in Staff:
                        try:
                            self.Mailer.Send(Address, StaffSubject, StaffHtml)
                            QuoteEvent(Db, Id, "staff_notified", {"to": Address, "about": "request"})
                        except Exception as E:  # noqa: BLE001
                            QuoteEvent(Db, Id, "staff_notify_failed", {"to": Address, "about": "request", "error": str(E)[:200]})
            (SendMail or (lambda Fn: Fn()))(Send)
        return Out

    def QuoteRequestJson(self, R: dict, Cand: dict | None = None) -> dict:
        Cand = Cand or self.Ctx.Db.One("SELECT asset_path FROM candidates WHERE id = ?", (R["candidate_id"],))
        Product = R.get("product_type") or Products.Ring
        Offer = json.loads(R["offer_json"]) if R.get("offer_json") else None
        Order = self.Ctx.Db.One("SELECT order_no FROM orders WHERE id = ?", (R["order_id"],)) if R.get("order_id") else None
        return {"id": R["id"], "ref": QuoteRef(R["request_no"]), "design_id": R["design_id"], "candidate_id": R["candidate_id"],
                "title": R["title"], "ring_id": R["ring_id"], "material_id": R["material_id"], "material_label": R["material_label"],
                "product_type": Product, "charm_size": R.get("charm_size"),
                "size_label": Products.SizeLabel(Product, R["ring_size"], R.get("charm_size")),
                "ring_size": R["ring_size"], "quantity": R["quantity"], "customer": json.loads(R["customer_json"]),
                "message": R["message"], "status": R["status"], "created_at": R["created_at"], "updated_at": R["updated_at"],
                "image_url": self.Ctx.AssetUrl(Cand["asset_path"]) if Cand else None, "owner_account_id": R["owner_account_id"],
                # the current quote (p3/quotes.py) and the customer's decision
                "offer": {K: Offer.get(K) for K in ("version", "unit_price", "quantity", "total", "valid_until", "sent_at", "size_label")}
                if Offer else None,
                "decided_at": R.get("decided_at"), "decision_note": R.get("decision_note"), "order_id": R.get("order_id"),
                "order_ref": OrderRef(Order["order_no"]) if Order else None,
                # an open request whose quote can no longer be approved (valid_until is a UTC date)
                "offer_expired": bool(Offer and Offer.get("valid_until") and R["status"] in QuoteOpen
                                      and Offer["valid_until"] < datetime.now(timezone.utc).date().isoformat())}

    # ── admin ────────────────────────────────────────────────────────────
    def _ThreeDForLine(self, L: dict) -> dict:
        """The 3D result that belongs to THIS line: the design's model scaled to the ordered size in the
        ordered material. A result for another size or material is never offered as the line's file —
        it is reported as `latest` so the admin sees what exists and can prepare the right one.

        A line is pinned to the 3D version current when it was ordered (order_lines.mesh_id; the normal order flow), then to
        the result it is produced from (three_d_id): a later version, or a newer result, never changes it — only an explicit
        "Use another version" (UseVersion). A line ordered before its design had a model follows the design (the version
        its STL would be prepared from, p3/versions.py MeshFor) until it is prepared or the order is released."""
        from p3.production3d import ProductionState
        Db = self.Ctx.Db
        Rows = Db.All("SELECT s.id, s.status, s.production_size, s.material_id, s.candidate_id, s.mesh_id, s.accepted_at, r.integrity "
                      "FROM session_3d s LEFT JOIN raw_geometry r ON r.mesh_id = s.mesh_id WHERE s.design_id = ? ORDER BY s.created_at DESC",
                      (L["design_id"],))
        Done = ("measured", "needs_review")
        Size = LineSize(L)                               # the US size of a ring, the mm size of a charm (None: no match)

        def SameSize(R):
            return Size is not None and R["production_size"] is not None and float(R["production_size"]) == float(Size)

        def Fits(R):                                     # of the ordered option, at the ordered size and material (2026-10-17)
            return R is not None and R["candidate_id"] == L.get("candidate_id") and SameSize(R) and R["material_id"] == L["material_id"]
        Pin = next((R for R in Rows if R["id"] == L.get("pinned_three_d_id")), None)
        if Pin and Pin["status"] in ("failed", "cancelled"):
            Pin = None                                   # a pinned result that failed: prepared again (and pinned again)
        Version = (Db.One("SELECT * FROM meshes WHERE id = ?", (Pin["mesh_id"] if Pin else L.get("pinned_mesh_id"),))
                   if Pin or L.get("pinned_mesh_id") else None) or Versions.MeshFor(Db, L["design_id"], L.get("candidate_id"))
        if Pin:
            Match, Pending = (Pin, None) if Pin["status"] in Done else (None, Pin)
        else:
            Mine = [R for R in Rows if Version and R["mesh_id"] == Version["id"]]
            Match = next((R for R in Mine if R["status"] in Done and SameSize(R) and R["material_id"] == L["material_id"]), None)
            Pending = next((R for R in Mine if R["status"] not in Done + ("failed", "cancelled") and SameSize(R)
                            and R["material_id"] == L["material_id"]), None)
        # The newest result of the ORDERED option only: another option's result is never shown beside the line (the owner's
        # report of 2026-10-17: Aurora Curve, ordered as option C, showed option B's result as its "latest")
        Latest = next((R for R in Rows if R["candidate_id"] == L.get("candidate_id")), None)
        Model = Versions.ActiveMesh(Db, L["design_id"])
        Chosen = Match or Pending
        Active = Model["id"] if Model else None
        Choices = Db.All("SELECT m.* FROM meshes m WHERE m.candidate_id = ? AND m.status = 'ready' AND (m.id = ? OR (m.archived_at IS NULL "
                         "AND NOT EXISTS (SELECT 1 FROM raw_geometry r WHERE r.mesh_id = m.id AND r.status = 'failed'))) "
                         "ORDER BY COALESCE(m.version_no, 0) DESC", (L.get("candidate_id"), Version["id"] if Version else ""))
        return {"three_d_id": Chosen["id"] if Chosen else None,
                "three_d_status": Chosen["status"] if Chosen else None,
                "three_d_state": ProductionState(Chosen["status"], Chosen["integrity"], bool(Chosen["accepted_at"])) if Chosen else None,
                "three_d_match": Fits(Match),                          # a result of another option, size or material never matches
                "three_d_pinned": bool(Pin),                           # its result (prepared, or released with the order)
                # What does not belong to this line among its picture, 3D version and STL (empty when they agree): blocks the hand-over
                "three_d_problems": self.LineAssetProblems(L),
                "three_d_version_pinned": bool(Pin or L.get("pinned_mesh_id")),   # its version (from the moment it was ordered)
                # the 3D version the line is (or would be) produced from, and the versions of the ordered option to choose from
                "three_d_version": Versions.Brief(Version, Active),
                "three_d_versions": [Versions.Brief(M, Active) for M in Choices],
                "has_model": Model is not None,
                # the modelled option: the one Hi3D model per design may be of another option than the ordered one
                "model_ring_id": RingIds.CandidateRef(Db, Model["candidate_id"]) if Model else None,
                "three_d_latest": {"id": Latest["id"], "size": Latest["production_size"], "material_id": Latest["material_id"],
                                   "state": ProductionState(Latest["status"], Latest["integrity"], bool(Latest["accepted_at"]))} if Latest and not Chosen else None}

    def MasterPresent(self, MeshId: str | None) -> bool | None:
        """Whether a 3D version's master STL is on disk with its recorded size (None: the version has no master record
        yet — still downloading or measuring)."""
        Raw = self.Ctx.Db.One("SELECT stl_path, bytes FROM raw_geometry WHERE mesh_id = ?", (MeshId,)) if MeshId else None
        if Raw is None:
            return None
        try:
            Path_ = assets.Resolve(self.Ctx.Settings.DevDir, Raw["stl_path"])
        except Exception:  # noqa: BLE001 — a path that escapes the root is no file
            return False
        return Path_.is_file() and (Raw["bytes"] is None or Path_.stat().st_size == Raw["bytes"])

    def LineAssetProblems(self, L: dict) -> list[str]:
        """What does not belong to THIS line among what it is shown and produced with (the owner's report of 2026-10-17):
        its option must be an option of the ordered design, its picture of that option, its 3D version of that option, and
        its recorded STL of the design, option and version at the ordered size and material. Empty when everything agrees;
        any problem blocks the line's hand-over to Production and is named exactly. Nothing is changed here."""
        Db, Out = self.Ctx.Db, []
        Cand = L.get("candidate_id")
        if not Cand:
            return Out

        def Ref(C):
            return (RingIds.CandidateRef(Db, C) if C else None) or "another option"
        Of = Db.One("SELECT b.design_id FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE c.id = ?", (Cand,))
        if Of is None or Of["design_id"] != L["design_id"]:
            Out.append("its option is not an option of the ordered design")
        Img = L.get("image_path") or L.get("image_url") or ""
        if "/candidates/" in Img and f"/candidates/{Cand}." not in Img:
            Out.append(f"its picture is not of the ordered option {L.get('ring_id') or ''}".rstrip())
        if L.get("pinned_mesh_id"):
            M = Db.One("SELECT candidate_id FROM meshes WHERE id = ?", (L["pinned_mesh_id"],))
            if M is None:
                Out.append("its 3D version no longer exists")
            elif M["candidate_id"] != Cand:
                Out.append(f"its 3D version is of {Ref(M['candidate_id'])}, not of the ordered {L.get('ring_id')}")
        if L.get("pinned_three_d_id"):
            R = Db.One("SELECT s.design_id, s.candidate_id, s.mesh_id, s.production_size, s.material_id, m.candidate_id AS mesh_candidate "
                       "FROM session_3d s LEFT JOIN meshes m ON m.id = s.mesh_id WHERE s.id = ?", (L["pinned_three_d_id"],))
            Product = L.get("product_type") or Products.Ring
            Size = LineSize(L)
            if R is None:
                Out.append("its STL record no longer exists")
                return Out
            if R["design_id"] != L["design_id"]:
                Out.append("its STL belongs to another design")
            elif R["candidate_id"] != Cand or (R["mesh_candidate"] and R["mesh_candidate"] != Cand):
                Out.append(f"its STL is of {Ref(R['mesh_candidate'] or R['candidate_id'])}, not of the ordered {L.get('ring_id')}")
            if L.get("pinned_mesh_id") and R["mesh_id"] != L["pinned_mesh_id"]:
                Out.append("its STL was not made from its 3D version")
            if Size is None or R["production_size"] is None or float(R["production_size"]) != float(Size):
                Got = Products.SizeLabel(Product, None if Product == Products.Charm else R["production_size"],
                                         R["production_size"] if Product == Products.Charm else None)
                Out.append(f"its STL is for {Got or 'no size'}, the line is {L.get('size_label') or 'without a size'}")
            if R["material_id"] != L["material_id"]:
                Mat = CharmPrices.Label(self.Ctx.Catalog, R["material_id"]) if Product == Products.Charm else \
                    ((self.Ctx.Catalog.Get(R["material_id"]).Label if self.Ctx.Catalog.Get(R["material_id"]) else R["material_id"]))
                Out.append(f"its STL is recorded for {Mat}, the line is {L.get('material_label')}")
        return Out

    def StlCandidates(self, L: dict) -> dict:
        """The finished 3D results that may become an unlinked line's print file (the owner's decisions of 2026-10-14,
        Fulfillment I-35): made from the line's own 3D version (the one fixed when it was ordered, else its option's
        current version) at exactly its size and material, production-complete (measured and unflagged, or flagged and
        accepted), the master STL on disk, and any file of its own present. Newest first. `same_file`: every candidate
        exports the very same file (the same master scaled by the same method, no bore made round, no stored file of its
        own) — linking the newest is then no choice at all; otherwise the Admin chooses, never silently. `pending`,
        `flagged`, `failed`: the line's other results at that size and material."""
        from p3.production3d import ProductionState
        Db = self.Ctx.Db
        Version = (Db.One("SELECT * FROM meshes WHERE id = ?", (L.get("pinned_mesh_id"),)) if L.get("pinned_mesh_id") else None) \
            or Versions.MeshFor(Db, L["design_id"], L.get("candidate_id"))
        Out = {"version": Versions.Brief(Version), "candidates": [], "same_file": False, "pending": [], "flagged": [], "failed": [],
               "shared": [], "master_present": self.MasterPresent(Version["id"]) if Version else None}
        Size = LineSize(L)
        if Version is None or Size is None:
            return Out
        Rows = Db.All("SELECT s.id, s.status, s.accepted_at, s.created_at, s.updated_at, r.integrity FROM session_3d s LEFT JOIN raw_geometry r ON r.mesh_id = s.mesh_id "
                      "WHERE s.design_id = ? AND s.mesh_id = ? AND s.material_id = ? AND s.production_size = ? ORDER BY s.created_at DESC",
                      (L["design_id"], Version["id"], L["material_id"], float(Size)))
        Keys = set()
        from p3.stl_activation import Inactive
        for R in Rows:
            State = ProductionState(R["status"], R["integrity"], bool(R["accepted_at"]))
            Brief = {"id": R["id"], "state": State, "created_at": R["created_at"], "measured_at": R["updated_at"], "accepted": bool(R["accepted_at"]),
                     "version_label": Versions.Label(Version), "size_label": L.get("size_label"), "material_label": L.get("material_label")}
            if State in ("failed", "cancelled"):
                Out["failed"].append(Brief)
                continue
            if State == "processing":
                Out["pending"].append(Brief)
                continue
            if State == "review_required":
                Out["flagged"].append(Brief)
                continue
            G = Db.One("SELECT method_version, checks_json, stl_path FROM geometry_results WHERE session_3d_id = ? AND stage = 'production'", (R["id"],)) or {}
            Corrected = bool(json.loads(G.get("checks_json") or "{}").get("bore_corrected"))
            Own = G.get("stl_path")
            if Own:
                try:
                    if not assets.Resolve(self.Ctx.Settings.DevDir, Own).is_file():
                        continue                                  # its own file is gone: not a print file
                except Exception:  # noqa: BLE001
                    continue
            if not Out["master_present"]:
                continue
            # Of a result and its bore-corrected revision, only the active one is offered as the line's file (FixBore update,
            # 2026-10-15); the other stays a valid file the Admin may still link explicitly
            if self.Activations is not None and Inactive(self.Activations.Pair(R["id"]), R["id"]):
                Out.setdefault("inactive", []).append({**Brief, "bore_corrected": Corrected, "own_file": bool(Own)})
                continue
            Keys.add((G.get("method_version"), Corrected, Own or ""))
            Out["candidates"].append({**Brief, "bore_corrected": Corrected, "own_file": bool(Own)})
        Out["same_file"] = bool(Out["candidates"]) and len(Keys) == 1
        if not Out["candidates"] and Out["master_present"]:
            Out["shared"] = self._SharedStls(L, Version, float(Size))
        return Out

    def _SharedStls(self, L: dict, Version: dict, Size: float) -> list[dict]:
        """The STL of the line's own option and 3D version at its size, finished in ANOTHER material (the owner's report of
        2026-10-17, ORD-10002 Fil Rope: the 14 mm STL existed in Silver, Stainless Steel was asked to prepare it again). A
        scaled STL is made from the master and the size only — the material changes the weight and the price, never the
        file — so such a result is the very same file. Only plain results count: measured, unflagged and never accepted
        as flagged (each material's flagged STL is reviewed on its own), no file of their own (a bore-corrected file is
        reused explicitly, ReuseBoreFix), and the active side of a FixBore pair. Newest first."""
        from p3.production3d import ProductionState
        from p3.stl_activation import Inactive
        Db, Out = self.Ctx.Db, []
        Rows = Db.All("SELECT s.id, s.status, s.accepted_at, s.created_at, s.updated_at, s.material_id, r.integrity FROM session_3d s "
                      "LEFT JOIN raw_geometry r ON r.mesh_id = s.mesh_id WHERE s.design_id = ? AND s.mesh_id = ? AND s.candidate_id = ? "
                      "AND s.material_id != ? AND s.production_size = ? ORDER BY s.created_at DESC",
                      (L["design_id"], Version["id"], L.get("candidate_id"), L["material_id"], Size))
        Product = L.get("product_type") or Products.Ring
        for R in Rows:
            if R["status"] != "measured" or R["accepted_at"] or ProductionState(R["status"], R["integrity"], False) != "complete":
                continue
            G = Db.One("SELECT checks_json, stl_path FROM geometry_results WHERE session_3d_id = ? AND stage = 'production'", (R["id"],)) or {}
            if G.get("stl_path") or json.loads(G.get("checks_json") or "{}").get("bore_corrected"):
                continue
            if self.Activations is not None and Inactive(self.Activations.Pair(R["id"]), R["id"]):
                continue
            Out.append({"id": R["id"], "state": "complete", "created_at": R["created_at"], "measured_at": R["updated_at"],
                        "version_label": Versions.Label(Version), "size_label": L.get("size_label"), "material_label": L.get("material_label"),
                        "from_material_id": R["material_id"],
                        "from_material_label": CharmPrices.Label(self.Ctx.Catalog, R["material_id"]) if Product == Products.Charm
                        else (self.Ctx.Catalog.Get(R["material_id"]).Label if self.Ctx.Catalog.Get(R["material_id"]) else R["material_id"])})
        return Out

    def PrepareShared(self, OrderId: str, LineId: str, FromId: str, By: str) -> dict:
        """Record the line's own STL from the same file finished in another material (_SharedStls), at once — no geometry job,
        no AI cost — and pin the line to it. Refused unless FromId is still such a file for this line; the new result must
        be finished and its geometry identical to FromId's (only the weight and the price differ), else nothing is pinned."""
        O = self.AdminGet(OrderId)
        L = next((X for X in O["lines"] if X["id"] == LineId), None)
        if L is None:
            raise HttpError(404, "order_line_not_found", "Order line not found.")
        if L.get("three_d_pinned"):
            return {"line": L, "three_d_id": L["three_d_id"], "prepared": False}
        Version = L.get("three_d_version")
        C = self.StlCandidates(L)
        From = next((X for X in C.get("shared", []) if X["id"] == FromId), None)
        if From is None or not Version or self.Production is None:
            raise HttpError(409, "shared_stl_changed", f"{L['ring_id']}: the {L.get('size_label')} STL named in the confirmation is no longer the same "
                                                        "file for this line — reload the order and check it.")
        Customer = (Sessions.Summaries(self.Ctx, SessionIds=[L["session_id"]]) or [None])[0]
        T = self.Production.Request(L["design_id"], ProductionSize=LineSize(L), MaterialId=L["material_id"],
                                    CandidateId=L["candidate_id"], RequestedBy=By, Customer=Customer, MeshId=Version["mesh_id"])
        Db = self.Ctx.Db
        Geo = {X["session_3d_id"]: X for X in Db.All("SELECT session_3d_id, volume_mm3, size_x_mm, size_y_mm, size_z_mm FROM geometry_results "
                                                     "WHERE stage = 'production' AND session_3d_id IN (?, ?)", (T["id"], FromId))}
        A, B = Geo.get(T["id"]), Geo.get(FromId)
        Same = A is not None and B is not None and all(
            A[K] is not None and B[K] is not None and abs(float(A[K]) - float(B[K])) <= 1e-6 * max(1.0, abs(float(B[K])))
            for K in ("volume_mm3", "size_x_mm", "size_y_mm", "size_z_mm"))
        if T.get("production_state") != "complete" or not Same:
            raise HttpError(409, "shared_stl_not_identical", f"{L['ring_id']}: the {L.get('material_label')} STL could not be recorded as the same file as the "
                                                             f"{From['from_material_label']} one ({T.get('production_state') or T.get('status')}) — "
                                                             "prepare it on the line and check it; nothing was linked.")
        self._Pin(O["id"], L, T["id"], By, "3d_prepared", ring_size=L["ring_size"], charm_size=L.get("charm_size"),
                  product_type=L.get("product_type"), material_id=L["material_id"], reused_model=True, shared_from=FromId,
                  shared_from_material_id=From["from_material_id"])
        Fresh = next(X for X in self.AdminGet(OrderId)["lines"] if X["id"] == LineId)
        return {"line": Fresh, "three_d_id": T["id"], "prepared": True, "shared_from": FromId}

    def LinkLine3D(self, OrderId: str, LineId: str, ThreeDId: str, By: str) -> dict:
        """Link one finished result to a line as its print file — the Admin's explicit choice when several different
        results exist (I-35). Never replaces a line's existing link: a different file for a linked line is a revised
        version (Use another version / Upload revised STL), and a line in Production changes only through a revision."""
        O = self.AdminGet(OrderId)
        L = next((X for X in O["lines"] if X["id"] == LineId), None)
        if L is None:
            raise HttpError(404, "order_line_not_found", "Order line not found.")
        if self.LineGuard:
            self.LineGuard(O, L)
        if L.get("three_d_pinned"):
            raise HttpError(409, "already_linked", f"{L['ring_id']} is already linked to its STL ({(L.get('three_d_version') or {}).get('label') or 'its version'}); "
                                                   "a different file is a revised version — Use another version or Upload revised STL.")
        C = self.StlCandidates(L)
        if ThreeDId not in [X["id"] for X in C["candidates"] + C.get("inactive", [])]:       # the inactive file of a pair: an explicit choice
            raise HttpError(409, "not_a_candidate", f"That result is not a finished STL of {L['ring_id']}'s 3D version at {L.get('size_label')} · {L.get('material_label')}.")
        self._Pin(O["id"], L, ThreeDId, By, linked="chosen", size_label=L.get("size_label"), material_id=L["material_id"])
        Fresh = next(X for X in self.AdminGet(OrderId)["lines"] if X["id"] == LineId)
        return {"line": Fresh, "three_d_id": ThreeDId}

    def _Pin(self, OrderId: str, Line: dict, ThreeDId: str, By: str, Kind: str = "3d_pinned", **Data) -> None:
        """Pin a line to a 3D result and its version (an order event says which, who and when)."""
        Db = self.Ctx.Db
        T = Db.One("SELECT s.mesh_id, m.version_no FROM session_3d s LEFT JOIN meshes m ON m.id = s.mesh_id WHERE s.id = ?", (ThreeDId,))
        Db.Execute("UPDATE order_lines SET three_d_id = ?, mesh_id = ? WHERE id = ? AND order_id = ?",
                   (ThreeDId, T["mesh_id"], Line["id"], OrderId))
        Db.Execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                   (OrderId, Kind, Dumps({"line_id": Line["id"], "session_3d_id": ThreeDId, "mesh_id": T["mesh_id"],
                                          "version_no": T["version_no"], **Data}), By, Now()))

    def PrepareLine3D(self, OrderId: str, LineId: str, By: str) -> dict:
        """The STL for an ordered ring must be the ordered size and material. Reuse the design's model
        (arithmetic, no Hi3D call) when the matching result does not exist yet; never start a paid
        Hi3D request from here — a design without a model is generated on its session page. The line is pinned to the
        result (and its 3D version) from here on."""
        O = self.AdminGet(OrderId)
        L = next((X for X in O["lines"] if X["id"] == LineId), None)
        if L is None:
            raise HttpError(404, "order_line_not_found", "Order line not found.")
        # A line pinned to an STL that is not its own (2026-10-17): its own STL is made from its option's own version and
        # pinned in its place, the replaced one and why named in the history — never for a line in Production, and never
        # when the line's own record (its picture or its option) is wrong: that line is not changed here, nor ever sent
        Repair = bool(L["three_d_id"] and L["three_d_pinned"] and L.get("three_d_problems"))
        if Repair:
            Own = [P for P in L["three_d_problems"] if P.startswith(("its picture", "its option"))]
            if Own:
                raise HttpError(409, "line_record_inconsistent", f"{L['ring_id']}: {'; '.join(Own)} — the order line itself is not consistent: "
                                                                 "it is not changed here and is never sent as it is.")
            if self.LineGuard:
                self.LineGuard(O, L)
        elif L["three_d_id"]:
            if not L["three_d_pinned"]:
                # A finished result exists for the line's size and material: link it — the newest when they are all the
                # very same file, never a choice among different ones (I-35: the Admin chooses with Link STL)
                C = self.StlCandidates(L)
                if C["candidates"] and not C["same_file"]:
                    raise HttpError(409, "stl_ambiguous", f"{L['ring_id']} has {len(C['candidates'])} different finished STLs at {L.get('size_label')} · "
                                                          f"{L.get('material_label')}: choose which one to link (Link STL).", {"choices": C["candidates"]})
                Sid = C["candidates"][0]["id"] if C["candidates"] else L["three_d_id"]
                self._Pin(O["id"], L, Sid, By)
                L = next(X for X in self.AdminGet(OrderId)["lines"] if X["id"] == LineId)
            return {"line": L, "three_d_id": L["three_d_id"], "prepared": False}
        if not L["has_model"]:
            raise HttpError(409, "no_model", f"{L['title']} ({L['ring_id']}) has no 3D model yet. Generate it on the session page first "
                                             "(a paid Hi3D call), then prepare the STL here.")
        # The STL is prepared from the option the customer ordered — never from another option's model
        Version = L.get("three_d_version")              # the pinned version, else the one the ordered option is made from
        if Repair:                                      # the pinned version may be the wrong one: the option's own current version
            Version = Versions.Brief(Versions.MeshFor(self.Ctx.Db, L["design_id"], L["candidate_id"]))
        if not Version:
            raise HttpError(409, "model_of_other_option",
                            f"The 3D model of this design is of option {L['model_ring_id'] or '—'}; this line was ordered as "
                            f"{L['ring_id']}. Generate a model for the ordered option on the session page (a paid Hi3D call), "
                            "then prepare the STL here.")
        if self.Production is None:
            raise HttpError(503, "production_unavailable", "3D production is not available.")
        Customer = (Sessions.Summaries(self.Ctx, SessionIds=[L["session_id"]]) or [None])[0]
        T = self.Production.Request(L["design_id"], ProductionSize=LineSize(L), MaterialId=L["material_id"],
                                    CandidateId=L["candidate_id"], RequestedBy=By, Customer=Customer, MeshId=Version["mesh_id"])
        self._Pin(O["id"], L, T["id"], By, "3d_prepared", ring_size=L["ring_size"], charm_size=L.get("charm_size"),
                  product_type=L.get("product_type"), material_id=L["material_id"], reused_model=bool(T.get("raw_available")),
                  **({"replaced": L["three_d_id"], "replaced_because": L["three_d_problems"]} if Repair else {}))
        Fresh = next(X for X in self.AdminGet(OrderId)["lines"] if X["id"] == LineId)
        return {"line": Fresh, "three_d_id": T["id"], "prepared": True}

    def UseVersion(self, OrderId: str, LineId: str, MeshId: str, By: str, Note: str = "", ForProduction: bool = False) -> dict:
        """Produce a line from another 3D version of its option (explicit; an order event keeps from / to / who / why). The
        line's result for that version is reused or made (arithmetic on its one measurement — no Hi3D call). An order
        already in production or later needs a note saying why its geometry changes. A line sent to Production changes
        only through the revision workflow (ForProduction: the bridge's SubmitRevision), never in place."""
        O = self.AdminGet(OrderId)
        L = next((X for X in O["lines"] if X["id"] == LineId), None)
        if L is None:
            raise HttpError(404, "order_line_not_found", "Order line not found.")
        if self.LineGuard and not ForProduction:
            self.LineGuard(O, L)
        Db = self.Ctx.Db
        M = Db.One("SELECT * FROM meshes WHERE id = ? AND status = 'ready'", (MeshId,))
        if M is None or not Db.One("SELECT 1 AS x FROM candidates c JOIN batches b ON b.id = c.batch_id WHERE c.id = ? AND b.design_id = ?",
                                   (M["candidate_id"], L["design_id"])):
            raise HttpError(404, "version_not_found", "That 3D version is not a version of this design.")
        if M["candidate_id"] != L["candidate_id"]:
            raise HttpError(409, "version_of_other_option", f"That version is of another option; this line was ordered as {L['ring_id']}.")
        if M["archived_at"]:
            raise HttpError(409, "version_archived", "That version is archived. Restore it on the session page first.")
        Note = " ".join(str(Note or "").split())[:300]
        if O["status"] in ProductionStatuses and not Note:
            raise HttpError(400, "note_required", f"Order {O['ref']} is {StatusLabels[O['status']].lower()}: say why its 3D version changes.")
        if self.Production is None:
            raise HttpError(503, "production_unavailable", "3D production is not available.")
        Before = L.get("three_d_version") or {}
        if L["three_d_pinned"] and Before.get("mesh_id") == MeshId:
            return {"line": L, "three_d_id": L["three_d_id"], "changed": False}
        T = self.Production.ResultFor(L["design_id"], MeshId, LineSize(L), L["material_id"])
        if T is None:
            Customer = (Sessions.Summaries(self.Ctx, SessionIds=[L["session_id"]]) or [None])[0]
            T = self.Production.Request(L["design_id"], ProductionSize=LineSize(L), MaterialId=L["material_id"],
                                        CandidateId=L["candidate_id"], RequestedBy=By, Customer=Customer, MeshId=MeshId)
        self._Pin(O["id"], L, T["id"], By, "3d_version_changed", from_mesh_id=Before.get("mesh_id"),
                  from_version_no=Before.get("version_no"), from_session_3d_id=L.get("pinned_three_d_id"), label=Versions.Label(M),
                  order_status=O["status"], note=Note)
        Fresh = next(X for X in self.AdminGet(OrderId)["lines"] if X["id"] == LineId)
        return {"line": Fresh, "three_d_id": T["id"], "changed": True}

    def UploadVersion(self, OrderId: str, LineId: str, UploadId: str, By: str, Scaling: str = "exact", SourceTool: str = "",
                      Note: str = "", Activate: bool = True, ForProduction: bool = False) -> dict:
        """A revised STL for an ordered line: a new 3D version of the line's design and option (revising the version the
        line is produced from), then the line is produced from it (UseVersion). Activate = it also becomes the design's
        active version; otherwise it is this order's own revision and the design keeps its active version. A line in
        Production takes it only through the revision workflow (ForProduction)."""
        if self.Versions is None:
            raise HttpError(503, "versions_unavailable", "3D versions are not available.")
        O = self.AdminGet(OrderId)
        L = next((X for X in O["lines"] if X["id"] == LineId), None)
        if L is None:
            raise HttpError(404, "order_line_not_found", "Order line not found.")
        if self.LineGuard and not ForProduction:
            self.LineGuard(O, L)
        Note = " ".join(str(Note or "").split())[:300]
        if O["status"] in ProductionStatuses and not Note:
            raise HttpError(400, "note_required", f"Order {O['ref']} is {StatusLabels[O['status']].lower()}: say why its 3D version changes.")
        Parent = (L.get("three_d_version") or {}).get("mesh_id")
        V = self.Versions.AddVersion(L["design_id"], UploadId, By, CandidateId=L["candidate_id"], Scaling=Scaling, SourceTool=SourceTool,
                                     Note=Note, Activate=Activate, ParentMeshId=Parent,
                                     Context_={"order_ref": O["ref"], "line_id": LineId})
        self.Ctx.Db.Execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                            (O["id"], "3d_version_uploaded", Dumps({"line_id": LineId, "mesh_id": V["id"], "version_no": V["version_no"],
                                                                    "filename": V["original_filename"], "scaling": Scaling,
                                                                    "source_tool": V["source_tool"], "active": bool(Activate)}), By, Now()))
        return {**self.UseVersion(OrderId, LineId, V["id"], By, Note or f"Revised STL {V['original_filename']}", ForProduction=ForProduction), "version": V}

    def _PinDerived(self, O: dict, By: str) -> None:
        """An order released toward production pins every line that still follows its design (prepared on the session
        page rather than here): from now on its geometry never changes by itself."""
        for L in O["lines"]:
            if L.get("three_d_id") and not L.get("three_d_pinned"):
                self._Pin(O["id"], L, L["three_d_id"], By, status_change=True)

    @staticmethod
    def _AdminWhere(Status: str | None, Payment: str | None, Query: str | None, Product: str | None,
                    OpenOnly: bool = False, Ids: list[str] | None = None) -> tuple[str, list]:
        """The Admin's order filters in SQL: status, payment, product, still open, a set of ids (the Fulfillment
        filter), and the search box — Order ID, customer name or email, promo code, a line's design name, Ring /
        Charm ID or design id."""
        Where, Params = [], []
        if Ids is not None:
            Where.append("o.id IN (" + ",".join("?" * len(Ids)) + ")" if Ids else "0")
            Params += list(Ids)
        if Product:
            Where.append("EXISTS (SELECT 1 FROM order_lines l WHERE l.order_id = o.id AND l.product_type = ?)")
            Params.append(Products.Normalize(Product))
        if Status:
            Where.append("o.status = ?")
            Params.append(Status)
        if Payment:
            Where.append("o.payment_status = ?")
            Params.append(Payment)
        if OpenOnly:
            Where.append("o.status NOT IN ('cancelled', 'completed')")
        if (Query or "").strip():
            Like = " LIKE ? ESCAPE '\\'"
            Where.append("(('ORD-' || o.order_no)" + Like + " OR " + _Name.format("o") + Like + " OR " + _Email.format("o") + Like
                         + " OR COALESCE(o.promo_code, '')" + Like
                         + " OR EXISTS (SELECT 1 FROM order_lines l WHERE l.order_id = o.id AND (l.title" + Like
                         + " OR COALESCE(l.ring_id, '')" + Like + " OR l.design_id" + Like + ")))")
            Params += [_Like(Query)] * 7
        return (" WHERE " + " AND ".join(Where)) if Where else "", Params

    def AdminList(self, Status: str | None = None, Payment: str | None = None, Query: str | None = None, Limit: int | None = 50,
                  Product: str | None = None, Offset: int = 0, OpenOnly: bool = False, Ids: list[str] | None = None) -> list[dict]:
        """One page of orders, newest first (Limit None: every match)."""
        Where, Params = self._AdminWhere(Status, Payment, Query, Product, OpenOnly, Ids)
        Page = " LIMIT ? OFFSET ?" if Limit is not None else ""
        Rows = self.Ctx.Db.All("SELECT o.* FROM orders o" + Where + " ORDER BY o.created_at DESC" + Page,
                               (*Params, *((Limit, max(0, Offset)) if Limit is not None else ())))
        Mock = Sessions.MockDesignIds(self.Ctx)
        Out = []
        for O in Rows:
            Lines = self._Lines(O["id"])
            J = self.ToJson(O, Lines, ForCustomer=False)
            J["mock"] = bool(Lines) and all(L["design_id"] in Mock for L in Lines)
            for L in J["lines"]:
                L.update(self._ThreeDForLine(L))
            J["three_d_state"] = _WorstState([L["three_d_state"] for L in J["lines"]])
            Out.append(J)
        return Out

    def AdminCount(self, Status: str | None = None, Payment: str | None = None, Query: str | None = None,
                   Product: str | None = None, Ids: list[str] | None = None) -> int:
        Where, Params = self._AdminWhere(Status, Payment, Query, Product, Ids=Ids)
        return self.Ctx.Db.One("SELECT COUNT(*) AS n FROM orders o" + Where, tuple(Params))["n"]

    def AdminGet(self, OrderId: str) -> dict:
        O = self.Ctx.Db.One("SELECT * FROM orders WHERE id = ? OR order_no = ?", (OrderId, _OrderNo(OrderId)))
        if O is None:
            raise HttpError(404, "order_not_found", "Order not found.")
        Lines = self._Lines(O["id"])
        J = self.ToJson(O, Lines, ForCustomer=False)
        J["test_marking"] = self.TestMarking(dict(O))                        # may an Admin mark it Test (or unmark it) now (2026-10-16)
        J["test_orders_to_production"] = bool(self.Ctx.Settings.FulfillmentTestOrders)   # does this site hand Test orders over (WP1)
        for L in J["lines"]:
            L.update(self._ThreeDForLine(L))
            # what the activation of a bore-corrected STL did with this line, if it concerned it (FixBore update, 2026-10-15)
            L["stl_activation"] = self.Activations.LineNote(L["id"]) if self.Activations is not None else None
            # the line's STL is an accepted FixBore revision (the bore-corrected file, stored)
            Of = self.Production.BoreFixOf(L["pinned_three_d_id"]) if self.Production is not None and L.get("pinned_three_d_id") else None
            L["stl_revision"] = {"bore_fix_id": Of["id"], "revision_of": Of["session_3d_id"], "file_sha256": Of["file_sha256"]} if Of else None
            Use = self.Ctx.Db.One("SELECT id FROM gallery_uses WHERE design_id = ? AND owner_account_id = ?", (L["design_id"], O["owner_account_id"]))
            L["session_id"] = Use["id"] if Use else L["design_id"]       # the customer's journey in Sessions
        J["three_d_state"] = _WorstState([L["three_d_state"] for L in J["lines"]])
        J["events"] = [{**E, "data": json.loads(E.pop("data_json") or "{}")} for E in
                       self.Ctx.Db.All("SELECT * FROM order_events WHERE order_id = ? ORDER BY id", (O["id"],))]
        J["allowed_statuses"] = (self.StatusOptions(O) if self.StatusOptions is not None
                                 else ["completed"] if O["status"] == "completed" else StatusOrder + ["cancelled"])
        Dups = self.DuplicateLines(O)
        for L in J["lines"]:
            L["duplicates"] = Dups.get(L["id"], [])
        J["notes_list"] = self._NotesList(O, J["events"])
        Quote = self.Ctx.Db.One("SELECT id, request_no FROM quote_requests WHERE order_id = ?", (O["id"],))
        J["quote"] = {"id": Quote["id"], "ref": QuoteRef(Quote["request_no"])} if Quote else None   # the quote it came from
        try:
            J["user"] = self.Ctx.Accounts.AdminGet(O["owner_account_id"])
        except Exception:  # noqa: BLE001
            J["user"] = None
        return J

    def _SetStatus(self, OrderId: str, Status: str, By: str, Note: str = "") -> None:
        Db = self.Ctx.Db
        Old = Db.One("SELECT status FROM orders WHERE id = ?", (OrderId,))["status"]
        if Old == Status:
            return
        T = Now()
        Db.Execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (Status, T, OrderId))
        Db.Execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                   (OrderId, "status", Dumps({"from": Old, "to": Status, "note": Note[:300]}), By, T))

    def DuplicateLines(self, O: dict) -> dict:
        """G9 (2026-10-15): for each line of the order, the same customer's other orders — not cancelled — that hold the
        same item at the same size and material (on tron, ORD-10005 re-ordered ORD-10003's ring and ORD-10004's charm).
        The customer is the account or the e-mail address. A warning for the Admin, never a block by itself and never a
        cancellation: a second piece can be intended. The same saved design (customization) is said as such."""
        Email = (json.loads(O.get("customer_json") or "{}").get("email") or "").strip().lower()
        Others = self.Ctx.Db.All("SELECT id, order_no, status, created_at FROM orders WHERE id <> ? AND status <> 'cancelled' AND "
                                 "((owner_account_id IS NOT NULL AND owner_account_id = ?) OR (? <> '' AND lower(trim(json_extract(customer_json, '$.email'))) = ?)) "
                                 "ORDER BY order_no", (O["id"], O.get("owner_account_id"), Email, Email))
        if not Others:
            return {}
        Mine = self.Ctx.Db.All("SELECT id, ring_id, product_type, material_id, material_label, ring_size, charm_size, customization_id "
                               "FROM order_lines WHERE order_id = ?", (O["id"],))
        Out: dict = {}
        for X in Others:
            for L2 in self.Ctx.Db.All("SELECT id, ring_id, product_type, material_id, ring_size, charm_size, customization_id FROM order_lines "
                                      "WHERE order_id = ? ORDER BY position", (X["id"],)):
                for L in Mine:
                    if (L["ring_id"], L["material_id"], L["ring_size"], L["charm_size"]) != (L2["ring_id"], L2["material_id"], L2["ring_size"], L2["charm_size"]):
                        continue
                    Out.setdefault(L["id"], []).append({
                        "order_id": X["id"], "order_ref": OrderRef(X["order_no"]), "status": X["status"], "status_label": StatusLabels.get(X["status"], X["status"]),
                        "placed_at": X["created_at"], "line_id": L2["id"], "item_ref": L2["ring_id"],
                        "size_label": Products.SizeLabel(L["product_type"] or Products.Ring, L["ring_size"], L["charm_size"]), "material_label": L["material_label"],
                        "same_design": bool(L["customization_id"]) and L["customization_id"] == L2["customization_id"]})
        return Out

    def TestMarking(self, O: dict) -> dict:
        """Whether an Admin may mark this order Test (or unmark it) now, and why not (the owner's decision of 2026-10-16).
        internal: the customer is at an internal domain (P3_TEST_ORDER_DOMAINS); since R-47 (2026-10-26) an Admin may mark
        a QA order placed from a customer account too, with the reason documented — the Admin is warned first."""
        Email = (json.loads(O["customer_json"] or "{}").get("email") or "").strip().lower()
        Domain = Email.rsplit("@", 1)[1] if "@" in Email else ""
        if ReservedEmail(Email):
            return {"possible": False, "why": "the customer is at a reserved test domain: the order is Test by its address"}
        if self._SentToProduction(O["id"]):
            return {"possible": False, "why": "it was sent to Production: Fulfillment classified it at the hand-over"}
        if O["payment_status"] == "paid":
            return {"possible": False, "why": "it is paid: a paid order is a real sale"}
        return {"possible": True, "why": None, "internal": Domain in tuple(self.Ctx.Settings.TestOrderDomains or ())}

    def _SentToProduction(self, OrderId: str) -> bool:
        Db = self.Ctx.Db
        for Table, Col in (("fulfillment_line_production", "order_id"), ("fulfillment_enrollments", "entity_id")):
            if Db.One("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?", (Table,)) and Db.One(f"SELECT 1 AS x FROM {Table} WHERE {Col} = ?", (OrderId,)):
                return True
        return False

    def MarkTest(self, OrderId: str, Test: bool, By: str, Reason: str) -> dict:
        """An Admin marks an order Test — or unmarks it — so a trial can run through Fulfillment to Shipped and Delivered
        without the real-order rule (the owner's decision of 2026-10-16). Explicit, with a reason, recorded in the order's
        history with the Admin who did it; never from an address alone; only before the order is sent to Production
        (Fulfillment classifies it once, at the hand-over); only an unpaid order. Since R-47 (2026-10-26) that includes a
        QA order placed from a customer account, not only one of an internal customer (P3_TEST_ORDER_DOMAINS). A Test
        order is kept out of the figures and Fulfillment's milestones never e-mail its customer."""
        Reason = " ".join(str(Reason or "").split())[:300]
        if len(Reason) < 5:
            raise HttpError(400, "reason_required", "Say why the order is " + ("marked Test" if Test else "no longer Test") + " (kept in its history).")
        O = self.Ctx.Db.One("SELECT * FROM orders WHERE id = ? OR order_no = ?", (OrderId, _OrderNo(OrderId)))
        if O is None:
            raise HttpError(404, "order_not_found", "Order not found.")
        if bool(O["test_marked_at"]) == bool(Test):
            return self.AdminGet(O["id"])                                    # already so: recorded once
        Can = self.TestMarking(O)
        if not Can["possible"]:
            raise HttpError(409, "test_marking_not_allowed", f"{OrderRef(O['order_no'])} cannot be " + ("marked Test" if Test else "unmarked") + f": {Can['why']}.")
        T = Now()
        with self.Ctx.Db.Transaction() as Conn:
            Cur = Conn.execute("SELECT test_marked_at, payment_status FROM orders WHERE id = ?", (O["id"],)).fetchone()
            if bool(Cur["test_marked_at"]) == bool(Test) or Cur["payment_status"] == "paid":
                raise HttpError(409, "test_marking_not_allowed", "The order changed meanwhile: reload it.")
            Conn.execute("UPDATE orders SET test_marked_at = ?, test_marked_by = ?, test_reason = ?, updated_at = ? WHERE id = ?",
                         (T if Test else None, By if Test else None, Reason if Test else None, T, O["id"]))
            Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                         (O["id"], "test_marked" if Test else "test_unmarked", Dumps({"reason": Reason}), By, T))
        return self.AdminGet(O["id"])

    def SetStatus(self, OrderId: str, Status: str, By: str, Note: str = "", Force: bool = False) -> dict:
        """Production and every later status need each line's 3D result for the ordered size and material to be
        complete (measured and not flagged, or a flagged one accepted by the Admin). Force with a note records an
        exception (a piece made outside the pipeline) instead of letting a flagged or missing result slip through."""
        O = self.AdminGet(OrderId)
        if Status not in StatusOrder + ["cancelled"]:
            raise HttpError(400, "invalid_status", "Unknown order status.")
        if O["status"] == "completed" and Status != "completed":
            raise HttpError(409, "order_completed", "A completed order cannot change status.")
        if self.StatusGuard is not None:
            self.StatusGuard(O, Status, Note)     # with Fulfillment: production / QC / shipping are recorded there (docs/FULFILLMENT.md)
        # Undoing a cancellation is an exception the history keeps with its reason (G1, 2026-10-15: ORD-10003 was set back
        # to New with none)
        if O["status"] == "cancelled" and Status != "cancelled" and not (Note or "").strip():
            raise HttpError(400, "reason_required", "Say why the cancellation is undone (kept in the order's history).")
        if Status in ProductionStatuses:
            Unresolved = [L for L in O["lines"] if not (L.get("three_d_match") and L.get("three_d_state") == "complete")]
            if Unresolved and not Force:
                Names = ", ".join(f"{L['title']} ({L['ring_id'] or '—'})" for L in Unresolved)
                raise HttpError(409, "three_d_unresolved",
                                f"Not ready for production: {Names} — the 3D result for the ordered size and material is missing, "
                                "still processing or flagged for review. Prepare or accept it first (or set the status with a "
                                "note that explains the exception).")
            if Unresolved and not (Note or "").strip():
                raise HttpError(400, "note_required", "Say why the status is set although the 3D results are not complete.")
            if Unresolved:
                Note = f"[3D results not complete — exception] {Note}"
        if Status == "three_d_ready" or Status in ProductionStatuses:
            self._PinDerived(O, By)
        self._SetStatus(O["id"], Status, By, Note)
        if self.StatusApplied is not None:
            self.StatusApplied(O, Status)
        return self.AdminGet(O["id"])

    def SetPayment(self, OrderId: str, Status: str, By: str, Note: str = "", Ref: str | None = None) -> dict:
        """Record a payment outcome by hand (bank transfer, phone payment …). It is logged as a manual
        entry with who did it and why; a connected provider will write the same fields itself."""
        O = self.AdminGet(OrderId)
        if Status not in Payments.Statuses:
            raise HttpError(400, "invalid_payment_status", "Unknown payment status.")
        if not (Note or "").strip():
            raise HttpError(400, "note_required", "Please say how the payment was received (or why it failed).")
        T = Now()
        self.Ctx.Db.Execute("UPDATE orders SET payment_status = ?, payment_provider = COALESCE(payment_provider, 'manual'), "
                            "payment_ref = COALESCE(?, payment_ref), updated_at = ? WHERE id = ?", (Status, (Ref or "").strip() or None, T, O["id"]))
        self.Ctx.Db.Execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                            (O["id"], "payment", Dumps({"from": O["payment_status"], "to": Status, "note": Note[:300], "ref": Ref, "manual": True}), By, T))
        if Status == "paid" and O["status"] in ("new", "confirmed"):
            self._SetStatus(O["id"], "payment_confirmed", By, "Payment recorded")
        return self.AdminGet(O["id"])

    def AddNote(self, OrderId: str, Note: str, By: str) -> dict:
        """A note joins the order's history and never replaces an earlier one: the note the order was created with
        ("From quote Q-5001 …") stays in orders.notes, every Admin note is its own event with its author and time."""
        O = self.AdminGet(OrderId)
        Note = (Note or "").strip()[:1000]
        if not Note:
            raise HttpError(400, "note_required", "Please write a note.")
        T = Now()
        self.Ctx.Db.Execute("UPDATE orders SET updated_at = ? WHERE id = ?", (T, O["id"]))
        self.Ctx.Db.Execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                            (O["id"], "note", Dumps({"note": Note}), By, T))
        return self.AdminGet(O["id"])

    @staticmethod
    def _NotesList(O: dict, Events: list[dict]) -> list[dict]:
        """Every note of an order, oldest first: the one it was created with ("From quote Q-5001 … Customer's note: …"),
        each Admin note, and a cancellation's reason. Before notes were append-only, a later note replaced orders.notes;
        that copy is not shown twice."""
        Out = [{"kind": "note", "note": E["data"]["note"], "at": E["created_at"], "by": E["by"]}
               for E in Events if E["kind"] == "note" and E["data"].get("note")]
        Out += [{"kind": "cancelled", "note": E["data"]["note"].strip(), "at": E["created_at"], "by": E["by"]}
                for E in Events if E["kind"] == "status" and E["data"].get("to") == "cancelled" and (E["data"].get("note") or "").strip()]
        First = (O.get("notes") or "").strip()
        if First and First not in {N["note"] for N in Out if N["kind"] == "note"}:
            Out.append({"kind": "created", "note": First, "at": O["created_at"], "by": "system"})
        return sorted(Out, key=lambda N: (N["at"] or "", N["kind"] != "created"))

    @staticmethod
    def _QuoteWhere(Status: str | None, Query: str | None) -> tuple[str, list]:
        """The quote inbox's filters in SQL: status ("open": new, quoted or answered) and the search box — Quote ID,
        customer name or email, design name, Ring / Charm ID, the Order ID it became."""
        Where, Params = [], []
        if Status == "open":
            Where.append("q.status IN (" + ",".join("?" * len(QuoteOpen)) + ")")
            Params += list(QuoteOpen)
        elif Status:
            Where.append("q.status = ?")
            Params.append(Status)
        if (Query or "").strip():
            Like = " LIKE ? ESCAPE '\\'"
            Where.append("(('Q-' || q.request_no)" + Like + " OR " + _Name.format("q") + Like + " OR " + _Email.format("q") + Like
                         + " OR q.title" + Like + " OR COALESCE(q.ring_id, '')" + Like
                         + " OR EXISTS (SELECT 1 FROM orders o WHERE o.id = q.order_id AND ('ORD-' || o.order_no)" + Like + "))")
            Params += [_Like(Query)] * 6
        return (" WHERE " + " AND ".join(Where)) if Where else "", Params

    def AdminQuoteRequests(self, Status: str | None = None, Query: str | None = None, Limit: int | None = 50,
                           Offset: int = 0) -> list[dict]:
        """One page of quote requests, newest first (Limit None: every match)."""
        Where, Params = self._QuoteWhere(Status, Query)
        Page = " LIMIT ? OFFSET ?" if Limit is not None else ""
        Rows = self.Ctx.Db.All("SELECT q.* FROM quote_requests q" + Where + " ORDER BY q.created_at DESC" + Page,
                               (*Params, *((Limit, max(0, Offset)) if Limit is not None else ())))
        return [self.QuoteRequestJson(R) for R in Rows]

    def AdminQuoteCount(self, Status: str | None = None, Query: str | None = None) -> int:
        Where, Params = self._QuoteWhere(Status, Query)
        return self.Ctx.Db.One("SELECT COUNT(*) AS n FROM quote_requests q" + Where, tuple(Params))["n"]

    def QuoteCounts(self) -> dict[str, int]:
        """Quote requests per status, every status present (the inbox filter and its badge: "new" awaits a reply)."""
        Counts = {R["status"]: R["n"] for R in self.Ctx.Db.All("SELECT status, COUNT(*) AS n FROM quote_requests GROUP BY status")}
        return {S: Counts.get(S, 0) for S in QuoteStatuses}

    def SetQuoteStatus(self, RequestId: str, Status: str, By: str = "admin") -> dict:
        """The Admin's own status for a request (recorded in its history). An approved quote became an order: it is
        changed through the order, not here."""
        if Status not in QuoteManualStatuses:
            raise HttpError(400, "invalid_status", "Unknown request status.")
        R = self.Ctx.Db.One("SELECT * FROM quote_requests WHERE id = ?", (RequestId,))
        if R is None:
            raise HttpError(404, "request_not_found", "Quote request not found.")
        if R["status"] == "approved":
            raise HttpError(409, "quote_approved", "This quote was approved and became an order: change the order instead.")
        if R["status"] != Status:
            self.Ctx.Db.Execute("UPDATE quote_requests SET status = ?, updated_at = ? WHERE id = ?", (Status, Now(), RequestId))
            QuoteEvent(self.Ctx.Db, RequestId, "status", {"from": R["status"], "to": Status}, By)
        return self.QuoteRequestJson(self.Ctx.Db.One("SELECT * FROM quote_requests WHERE id = ?", (RequestId,)))

    def CreateFromQuote(self, Q: dict, Offer: dict, AddressRaw: dict, Method: str, Note: str = "", SendMail=None,
                        Origin: str = "") -> dict:
        """An approved quote becomes an order: the quoted piece at the quoted price (one line), the customer of the
        request, the address and the terms given on approval. One order per quote (a repeated approval returns it);
        payment as for every order — never faked."""
        Db = self.Ctx.Db
        Rid, Ref = f"quote:{Q['id']}", QuoteRef(Q["request_no"])
        Existing = Db.One("SELECT id FROM orders WHERE owner_account_id = ? AND client_request_id = ?", (Q["owner_account_id"], Rid))
        if Existing:
            return self._OrderJson(Existing["id"])
        if QuoteOfferProblem(self.Ctx, Q):            # never an order for a product off for customers or a material not quoted
            raise HttpError(409, "quote_not_orderable", "This quote can no longer become an order. Please contact us.")
        Addr = self.ValidateAddress(AddressRaw)
        Problems = list(Addr["problems"])
        if Method not in ShippingOptions:
            Problems.append({"field": "shipping_method", "code": "shipping_method_unavailable",
                             "message": RetiredShippingText if Method in ShippingMethods else "Please choose a delivery option."})
        if Problems:
            raise _Invalid(Problems)
        Product = Q.get("product_type") or Products.Ring
        Charm = Product == Products.Charm
        Size, Unit, Qty = float(Offer["size"]), round(float(Offer["unit_price"]), 2), int(Offer["quantity"])
        Line = {"design_id": Q["design_id"], "candidate_id": Q["candidate_id"], "title": Q["title"], "ring_id": Q["ring_id"],
                "product_type": Product, "material_id": Q["material_id"], "material_label": Q["material_label"],
                "ring_size": None if Charm else Size, "charm_size": Size if Charm else None, "quantity": Qty, "unit_price": Unit,
                "line_total": round(Unit * Qty, 2), "currency": Offer.get("currency") or "USD",
                "pricing_version": f"quote:{Ref}:v{Offer['version']}"}
        Ship = ShippingOptions[Method]
        Validation = Addr["validation"]
        Status = Validation.get("status") or "unverified"
        if Status not in ("unverified", "verified", "corrected", "failed"):
            Status = "unverified"
        Img = Db.One("SELECT asset_path FROM candidates WHERE id = ?", (Q["candidate_id"],))
        Snapshot = {**PurchaseSnapshot(Line), "quote": {"ref": Ref, "version": Offer["version"], "weight_g": Offer.get("weight_g"),
                                                        "price_per_g": Offer.get("price_per_g")}}
        OrderId, T = NewId("ord"), Now()
        TermsV, TermsSha = self.AcceptedTerms()             # the approval page's "I accept the Terms of Service" (M6)
        Total = round(Line["line_total"] + Ship["price"], 2)
        Notes = f"From quote {Ref} (version {Offer['version']})." + (f" Customer's note: {Note}" if Note else "")
        Pin = (Versions.MeshFor(Db, Q["design_id"], Q["candidate_id"]) or {}).get("id")     # the 3D version current now
        try:
            with Db.Transaction() as Conn:
                Conn.execute("INSERT INTO orders (id, owner_account_id, status, payment_status, customer_json, shipping_json, "
                             "address_validation, shipping_method, currency, subtotal, discount, shipping, total, promo_code, promo_json, "
                             "terms_version, terms_sha256, terms_accepted_at, client_request_id, notes, created_at, updated_at) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (OrderId, Q["owner_account_id"], "new", "pending", Q["customer_json"],
                              Dumps({**Addr["address"], "validation": {K: V for K, V in Validation.items() if K != "suggestion"},
                                     "suggestion": Validation.get("suggestion")}),
                              Status, Method, Line["currency"], Line["line_total"], 0.0, Ship["price"], Total, None, None,
                              TermsV, TermsSha, T, Rid, Notes, T, T))
                Conn.execute("INSERT INTO order_lines (id, order_id, position, design_id, candidate_id, bag_line_id, customization_id, "
                             "title, ring_id, product_type, material_id, material_label, ring_size, charm_size, quantity, unit_price, "
                             "line_total, currency, pricing_version, image_path, purchase_json, mesh_id) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (NewId("oln"), OrderId, 1, Line["design_id"], Line["candidate_id"], None, None, Line["title"],
                              Line["ring_id"], Product, Line["material_id"], Line["material_label"], Line["ring_size"],
                              Line["charm_size"], Qty, Unit, Line["line_total"], Line["currency"], Line["pricing_version"],
                              Img["asset_path"] if Img else None, Dumps(Snapshot), Pin))
                Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                             (OrderId, "placed", Dumps({"total": Total, "lines": 1, "from_quote": Ref, "version": Offer["version"]}),
                              "customer", T))
        except sqlite3.IntegrityError:                  # the same approval twice at once: the first one placed the order
            Existing = Db.One("SELECT id FROM orders WHERE owner_account_id = ? AND client_request_id = ?", (Q["owner_account_id"], Rid))
            if Existing:
                return self._OrderJson(Existing["id"])
            raise
        Order = Db.One("SELECT * FROM orders WHERE id = ?", (OrderId,))
        Pay = self.Payment.Begin(Order)
        PayStatus = Pay["status"] if Pay["status"] in Payments.Statuses else "pending"
        Db.Execute("UPDATE orders SET payment_status = ?, payment_provider = ?, payment_ref = ?, payment_json = ?, updated_at = ? WHERE id = ?",
                   (PayStatus, self.Payment.Name if self.Payment.Available else None, Pay.get("ref"),
                    Dumps({"message": Pay.get("message"), "client": Pay.get("client")}), Now(), OrderId))
        if PayStatus == "paid":
            self._SetStatus(OrderId, "payment_confirmed", self.Payment.Name, "Paid through the provider")
        OrderNo = Db.One("SELECT order_no FROM orders WHERE id = ?", (OrderId,))["order_no"]
        Sizes = {"product_type": Products.Charm, "charm_size": Size} if Charm else {"ring_size": Size}
        Sessions.Record(self.Ctx, Q["owner_account_id"], "order_placed", Q["design_id"], order_id=OrderId, order_ref=OrderRef(OrderNo),
                        candidate_id=Q["candidate_id"], ring_id=Q["ring_id"], material_id=Q["material_id"], **Sizes, quantity=Qty,
                        unit_price=Unit, line_total=Line["line_total"], currency=Line["currency"], from_quote=Ref)
        Out = self._OrderJson(OrderId)
        self._Email(Out, SendMail, Origin)
        return Out

    def _OrderJson(self, OrderId: str) -> dict:
        O = self.Ctx.Db.One("SELECT * FROM orders WHERE id = ?", (OrderId,))
        return self.ToJson(O, self._Lines(OrderId))

    def Summary(self, ExcludeMock: bool = True, Since: str | None = None) -> dict:
        """Dashboard numbers: counts by status and payment, revenue of live orders, open quote requests.
        Since (ISO) limits the orders to a time range; None = all time."""
        Mock = Sessions.MockDesignIds(self.Ctx) if ExcludeMock else set()
        Orders, Tests = [], 0
        for O in self.Ctx.Db.All("SELECT * FROM orders" + (" WHERE created_at >= ?" if Since else "") + " ORDER BY created_at DESC",
                                 (Since,) if Since else ()):
            Lines = self._Lines(O["id"])
            if ExcludeMock and Lines and all(L["design_id"] in Mock for L in Lines):
                continue
            if IsTestCustomer(json.loads(O["customer_json"] or "{}")) or O["test_marked_at"]:   # a synthetic or marked Test order: counted apart
                Tests += 1
                continue
            Orders.append(O)
        Live = [O for O in Orders if O["status"] != "cancelled"]
        return {"orders": len(Orders), "test_orders": Tests, "open": sum(1 for O in Live if O["status"] not in ("completed",)),
                "payment_pending": sum(1 for O in Live if O["payment_status"] == "pending"),
                "revenue": round(sum(O["total"] for O in Live if O["payment_status"] == "paid"), 2),
                "reserved_value": round(sum(O["total"] for O in Live if O["payment_status"] != "paid"), 2),
                "by_status": {S: sum(1 for O in Orders if O["status"] == S) for S in StatusOrder + ["cancelled"]},
                "quote_requests_open": self.Ctx.Db.One("SELECT COUNT(*) AS n FROM quote_requests WHERE status = 'new'")["n"],
                "by_product": {P: sum(1 for O in Orders if P in {L.get("product_type") or Products.Ring for L in self._Lines(O["id"])})
                               for P in Products.All},
                "latest": [self.ToJson(O, self._Lines(O["id"]), ForCustomer=False) for O in Orders[:5]]}


def LineSize(L: dict):
    """The size a line was ordered in: a ring's US size, a charm's size in mm (None when it has none)."""
    return L.get("charm_size") if (L.get("product_type") or Products.Ring) == Products.Charm else L.get("ring_size")


def PurchaseSnapshot(L: dict) -> dict:
    """What was bought, as it was understood at the moment of ordering — kept with the order line so it stays clear
    if sizes, definitions or names change later."""
    Product = L.get("product_type") or Products.Ring
    if Product == Products.Charm:
        Size = {"value": L.get("charm_size"), "unit": Products.CharmSizeUnit, "label": Products.SizeLabel(Product, None, L.get("charm_size")),
                "definition": Products.CharmSizeDefinition["text"]}
    else:
        Size = {"value": L.get("ring_size"), "system": "US", "label": Products.SizeLabel(Product, L.get("ring_size"))}
    Promo = L.get("_promotion")
    return {"product": Product, "product_label": Products.Labels[Product], "size": Size,
            "material": {"id": L["material_id"], "label": L["material_label"]},
            "price": {"unit_price": L["unit_price"], "currency": L["currency"], "pricing_version": L["pricing_version"]},
            # a promotional price (P1, 12 Nov 2026): its label, the regular price and the saving at that moment (in the
            # order's currency; the USD figures too for an order in ₪), and whether they were verified reference prices
            **({"promotion": {K: Promo.get(K) for K in ("label", "regular_unit_price", "savings", "ends_at", "claims",
                                                       "usd_regular_unit_price", "usd_savings") if Promo.get(K) is not None
                              or K == "claims"}} if Promo else {})}


def LinePromotion(L: dict, Full: bool = False) -> dict | None:
    """The promotion an order line was placed at (its purchase snapshot), as the customer may read it: the label, and the
    regular price and saving only when they were verified reference prices then. Full: all of it (the Admin)."""
    try:
        Snap = json.loads(L.get("purchase_json") or "{}")
    except (TypeError, ValueError):
        return None
    return (Snap.get("promotion") or None) if Full else PublicPromotion(Snap.get("promotion"))


_NotePattern = re.compile(r"(?:From quote Q-\d+ \(version \d+\)\. )?Customer's note: (.+)", re.S)


def CustomerNote(Notes: str | None) -> str | None:
    """The note the customer wrote at checkout or on a quote approval (R-45), as the order keeps it ("Customer's note: …",
    after "From quote Q-5001 (version 1). " for a quote's order) — the customer's own words only, never anything else the
    field holds (before notes were append-only, an Admin note could replace it: such a field gives None)."""
    M = _NotePattern.fullmatch((Notes or "").strip())
    return M.group(1).strip() or None if M else None


def CustomerView(J: dict, History: list[dict], PaidAt: str | None) -> dict:
    """What My Account shows for an order (B7, owner's decisions 11 Nov 2026), from the order status, the payment status
    and the order's history only:
      customer_status — one status in the customer's words (CustomerStatusLabels), a next-step line for the order's card
                        and a longer "what happens now";
      progress        — Order received → Design review → Payment → Production → Shipping → Completed: a step is done only
                        when the history records it (its date) or a later recorded status implies it was passed (no
                        date then); never assumed, never a promised date; the current step says what it waits for;
      payment_view    — the payment as the customer reads it;
      cancel_free     — the order is not confirmed yet: it can be cancelled free of charge (the published wording).
    Every time is in business days."""
    S, Pay = J["status"], J["payment_status"]
    First: dict = {}
    for H in History:
        First.setdefault(H["status"], H["at"])

    def At(*Statuses):
        Times = [First[X] for X in Statuses if First.get(X)]
        return min(Times) if Times else None

    def Idx(X):
        return StatusOrder.index(X) if X in StatusOrder else -1
    # The furthest the order got: its status now (an Admin's step back wins), or for a cancelled order, before it was cancelled
    Reached = Idx(S) if S != "cancelled" else max([Idx(H["status"]) for H in History] + [0])
    Ship = (J.get("shipping_label") or "Delivery") + (", " + J["shipping_eta"] if J.get("shipping_eta") else "")
    Failed = Pay == "failed"
    Paid = Pay == "paid" or Reached >= Idx("payment_confirmed")
    Steps = [
        {"key": "received", "label": "Order received", "done": True, "at": J["created_at"], "text": ""},
        {"key": "review", "label": "Design review", "done": Reached >= Idx("confirmed"), "at": At("confirmed"),
         "text": "About 2 business days", "now": "In progress · about 2 business days"},
        {"key": "payment", "label": "Payment", "done": Paid, "at": PaidAt or At("payment_confirmed"),
         "text": "We contact you after the review to arrange it",
         "now": "Your payment didn’t go through — we’ll contact you" if Failed else "We’ll contact you to arrange payment"},
        {"key": "production", "label": "Production", "done": Reached >= Idx("shipped"), "at": At("shipped"),
         "text": "Starts once your payment is confirmed · up to 14 business days",
         "now": "In progress · up to 14 business days from your payment" if S in _InProduction else "Starts now · up to 14 business days",
         "since": At(*_InProduction)},
        {"key": "shipping", "label": "Shipping", "done": Reached >= Idx("completed"), "at": At("completed"),
         "text": Ship + ", tracked", "now": "On its way · " + Ship + ", tracked", "since": At("shipped")},
        {"key": "completed", "label": "Completed", "done": Reached >= Idx("completed"), "at": At("completed"), "text": ""},
    ]
    Progress, Current = [], None
    for P in Steps:
        if P["done"]:
            State = "done"
        elif S in ("cancelled", "completed"):
            continue                                     # a cancelled order shows only what happened before
        elif Current is None:
            State = Current = "current"
        else:
            State = "upcoming"
        Progress.append({"key": P["key"], "label": P["label"], "state": State, "at": P["at"] if State == "done" else None,
                         "since": P.get("since") if State == "current" else None,
                         "text": P.get("now", P["text"]) if State == "current" else P["text"] if State == "upcoming" else ""})
    if S == "cancelled":
        Progress.append({"key": "cancelled", "label": "Cancelled", "state": "done", "at": At("cancelled"), "since": None, "text": ""})
    Words = {
        "new": ("progress", "We’re reviewing your design (about 2 business days). Nothing to pay yet.",
                "Our team is checking that your design can be made (about 2 business days). Nothing to pay yet — after the review "
                "we contact you to arrange payment. You can still cancel free of charge."),
        "confirmed": ("action", "Your design is confirmed. We’ll contact you to arrange payment.",
                      "Your design is confirmed. We’ll contact you to arrange payment; production starts once your payment is "
                      "confirmed and takes up to 14 business days."),
        "payment_confirmed": ("progress", "Production starts now and takes up to 14 business days.",
                              f"Your payment is confirmed. Production starts now and takes up to 14 business days; then we ship "
                              f"your order ({Ship})."),
        "shipped": ("progress", f"On its way: {Ship}.", f"Your order is on its way: {Ship}, tracked."),
        "completed": ("done", "This order is complete.", "This order is complete. Questions about it? Email us with your Order ID."),
        "cancelled": ("ended", "This order was cancelled.", "This order was cancelled."),
    }
    for X in _InProduction:
        Words[X] = ("progress", "Being made — up to 14 business days from your payment.",
                    f"Your order is being made. Production takes up to 14 business days from your payment; then we ship it ({Ship}).")
    Tone, Next, Now_ = Words.get(S, ("progress", "", ""))
    if Failed and S in ("new", "confirmed"):
        Tone, Next = "action", "Your payment didn’t go through — we’ll contact you about it."
        Now_ = Next
    if Pay == "paid":
        PayView = ("paid", "Paid", "Your payment is confirmed.", PaidAt)
    elif Failed:
        PayView = ("action", "Payment didn’t go through", "We’ll contact you about it.", None)
    elif Pay == "refunded":
        PayView = ("ended", "Refunded", "Your payment was refunded.", None)
    elif Pay == "cancelled" or S == "cancelled":
        PayView = ("ended", "Not due", "No payment is due for a cancelled order.", None)
    elif Reached >= Idx("payment_confirmed"):
        PayView = ("paid", "Payment confirmed", "Your payment is confirmed.", At("payment_confirmed"))
    elif S == "confirmed":
        PayView = ("action", "Awaiting payment", "We’ve confirmed your design and will contact you to arrange payment. No payment is "
                                                 "taken online. Production starts once your payment is confirmed.", None)
    else:
        PayView = ("progress", "Not due yet", "No payment is taken online. After our review (about 2 business days) we contact you "
                                              "to arrange payment. Production starts once your payment is confirmed.", None)
    return {"customer_status": {"key": S, "label": CustomerStatusLabels.get(S, J.get("status_label") or S), "tone": Tone, "next": Next, "now": Now_},
            "progress": Progress,
            "payment_view": {"tone": PayView[0], "label": PayView[1], "text": PayView[2], "at": PayView[3]},
            "cancel_free": S == "new"}


def _OrderNo(Ref: str):
    M = re.fullmatch(r"(?:ORD-)?(\d+)", (Ref or "").strip().upper())
    return int(M.group(1)) if M else -1


def _WorstState(States: list) -> str | None:
    Rank = ["failed", "review_required", "processing", "complete", "cancelled"]
    Known = [S for S in States if S]
    if not Known:
        return None
    return sorted(Known, key=lambda S: Rank.index(S) if S in Rank else 99)[0]


def _Invalid(Problems: list[dict]) -> HttpError:
    E = HttpError(400, "invalid_order", Problems[0]["message"])
    E.Problems = Problems
    return E
