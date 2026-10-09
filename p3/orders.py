"""Checkout and orders — the real order path for fixed-price materials.

  Bag → Checkout → Customer details → Shipping address → Promo code → Payment → Order confirmation

Rules (the Legacy JewelryB2C flow was the functional reference; its browser-only "order", random
XJ numbers, mandatory XJET10 coupon and client-side prices were deliberately NOT carried over):
  * the server is authoritative: every unit price is the CURRENT fixed price of the material at the
    moment of ordering (the bag snapshot only flags "repriced"), promo discounts are evaluated here,
    totals are computed here, and the order stores a snapshot of all of it;
  * an order is a reservation until XJet has reviewed the design for production feasibility — the
    confirmation says so; payment is never faked (p3/payments.py);
  * one order per click (client_request_id), an empty or non-purchasable bag cannot be ordered,
    Gold (luxury) is not on this path (it asks for a quote instead: RequestQuote);
  * terms acceptance is recorded with its version and time; the customer's known profile pre-fills
    the form but never overwrites what they typed (the browser's job);
  * no production cost or 3D price ever appears in anything the customer receives.
"""

import html
import json
import re
import sqlite3
from datetime import datetime, timezone

from p3 import addressing as Addressing
from p3 import charmprices as CharmPrices
from p3 import payments as Payments
from p3 import products as Products
from p3 import ringids as RingIds
from p3 import sessions as Sessions
from p3.accounts import Principal
from p3.accounts.local import EmailPattern
from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now

TermsVersion = "2026-10-02"
ShippingOptions = {
    "standard": {"id": "standard", "label": "Standard delivery", "price": 0.0, "eta": "up to 14 business days",
                 "detail": "Free · tracked, signature on delivery"},
    "express": {"id": "express", "label": "Express delivery", "price": 45.0, "eta": "about 5 business days",
                "detail": "$45 · priority handling, fully insured"},
}
StatusOrder = ["new", "payment_confirmed", "three_d_ready", "production", "qc", "shipped", "completed"]
StatusLabels = {"new": "New", "payment_confirmed": "Payment confirmed", "three_d_ready": "3D ready", "production": "In production",
                "qc": "Quality check", "shipped": "Shipped", "completed": "Completed", "cancelled": "Cancelled"}
ProductionStatuses = ("production", "qc", "shipped", "completed")     # need every line's 3D result complete (SetStatus)
PhoneDigits = re.compile(r"\d")
PhonePattern = re.compile(r"^\+?[0-9 ()./-]{7,24}$")
MaxLines = 20


def OrderRef(No) -> str | None:
    return f"ORD-{No}" if No is not None else None


def QuoteRef(No) -> str | None:
    return f"Q-{No}" if No is not None else None


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

    # ── checkout: what the customer sees before ordering ─────────────────
    def CheckoutInfo(self, Who: Principal) -> dict:
        Profile = self.Ctx.Accounts.Profile(Who)
        Name = (Profile.get("name") or "").strip()
        First, _, Last = Name.partition(" ")
        return {
            "customer": {"first_name": First, "last_name": Last.strip(), "email": Profile.get("email") or "", "phone": ""},
            "countries": [{"code": C, "name": N} for C, N in Addressing.Countries],
            "region_required": sorted(Addressing.RegionRequired), "no_postal_code": sorted(Addressing.NoPostalCode),
            "shipping_options": list(ShippingOptions.values()),
            "terms_version": TermsVersion,
            "payment": {"available": self.Payment.Available, "provider": self.Payment.Name if self.Payment.Available else None,
                        "message": getattr(self.Payment, "Message", "")},
            "address_validation": {"provider": self.Validator.Name if self.Validator.Name != "none" else None},
            "quote": self.Quote(Who),
        }

    def _BagLines(self, Who: Principal) -> list[dict]:
        """The bag at today's prices: a line is orderable only with a purchasable material, an available
        price and a standard size. The snapshot price is kept for the "repriced" flag only."""
        Out = []
        for L in self.Ctx.Db.All("SELECT b.*, c.asset_path, d.title FROM bag_lines b JOIN candidates c ON c.id = b.candidate_id "
                                 "JOIN designs d ON d.id = b.design_id WHERE b.owner_account_id = ? ORDER BY b.created_at", (Who.AccountId,)):
            Mat = self.Ctx.Catalog.Get(L["material_id"])
            Product = L.get("product_type") or Products.Ring
            Q = CharmPrices.QuoteFor(self.Ctx, Product, L["material_id"], L.get("charm_size")) if Mat else None
            Problem = None
            if Product == Products.Charm:
                # a charm's price depends on its size: the size is checked first
                if Mat is None or not self.Ctx.Catalog.IsPurchasableGroup(Mat.Group):
                    Problem = "Gold charms are quoted individually — remove this line and use Request a quote."
                elif L["charm_size"] is None or not self.Ctx.Products.IsValidCharmSize(L["charm_size"]):
                    Problem = "Please choose one of the charm sizes."
                elif not Q.IsAvailable:
                    Problem = "The price for this charm in this size and material is not available right now."
            elif Mat is None or not self.Ctx.Catalog.IsPurchasableGroup(Mat.Group):
                Problem = "Gold rings are quoted individually — remove this line and use Request a quote."
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
                        "quantity": L["quantity"], "unit_price": Unit, "currency": Q.currency if Q else L["currency"],
                        "pricing_version": Q.pricing_version if Q else None,
                        "line_total": round(Unit * L["quantity"], 2) if Unit is not None else None,
                        "repriced": Unit is not None and round(Unit, 2) != round(L["unit_price"], 2),
                        "snapshot_unit_price": L["unit_price"], "problem": Problem})
        return Out

    def Quote(self, Who: Principal, PromoCode: str | None = None, ShippingMethod: str = "standard") -> dict:
        Lines = self._BagLines(Who)
        Refs = RingIds.CandidateRefs(self.Ctx.Db, list({L["design_id"] for L in Lines}))
        for L in Lines:
            L["ring_id"] = Refs.get(L["candidate_id"])
        Ok = [L for L in Lines if not L["problem"]]
        Subtotal = round(sum(L["line_total"] for L in Ok), 2)
        Currency = next((L["currency"] for L in Ok), "USD")
        Ship = ShippingOptions.get(ShippingMethod or "standard")
        Promo, PromoError_ = None, None
        if PromoCode and str(PromoCode).strip():
            if not Ok:
                PromoError_ = {"code": "promo_no_items", "message": "Add a piece to your bag before using a promo code."}
            else:
                try:
                    Promo = self.Promos.Evaluate(PromoCode, Ok)
                except HttpError as E:
                    PromoError_ = {"code": E.Code, "message": E.Message}
        Discount = Promo["discount"] if Promo else 0.0
        Shipping = Ship["price"] if Ship else 0.0
        return {"lines": Lines, "count": sum(L["quantity"] for L in Ok), "currency": Currency,
                "subtotal": Subtotal, "discount": round(Discount, 2), "promo": Promo, "promo_error": PromoError_,
                "shipping_method": Ship["id"] if Ship else None, "shipping": Shipping,
                "total": round(max(0.0, Subtotal - Discount) + Shipping, 2),
                "ok": bool(Ok) and all(not L["problem"] for L in Lines) and Ship is not None,
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
            P.append({"field": "phone", "message": "Please enter a phone number we can reach you on (with the country code)."})
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
    def Create(self, Who: Principal, Body: dict, SendMail=None) -> dict:
        Db = self.Ctx.Db
        Rid = (Body.get("client_request_id") or "").strip() or None
        if Rid:
            Existing = Db.One("SELECT id FROM orders WHERE owner_account_id = ? AND client_request_id = ?", (Who.AccountId, Rid))
            if Existing:
                return self.Get(Who, Existing["id"])
        Customer, Problems = self.ValidateCustomer(Body.get("customer"))
        Addr = self.ValidateAddress(Body.get("address"))
        Problems += Addr["problems"]
        Method = str(Body.get("shipping_method") or "standard")
        if Method not in ShippingOptions:
            Problems.append({"field": "shipping_method", "message": "Please choose a delivery option."})
        if Body.get("terms_accepted") is not True:
            Problems.append({"field": "terms_accepted", "message": "Please accept the Terms of Service to place the order."})
        if Problems:
            raise _Invalid(Problems)
        Q = self.Quote(Who, Body.get("promo_code"), Method)
        if not Q["lines"]:
            raise HttpError(409, "bag_empty", "Your bag is empty.")
        if Q["promo_error"]:
            raise HttpError(400, Q["promo_error"]["code"], Q["promo_error"]["message"])
        if not Q["ok"]:
            Bad = next(L for L in Q["lines"] if L["problem"])
            raise HttpError(409, "bag_not_orderable", Bad["problem"])
        if len(Q["lines"]) > MaxLines:
            raise HttpError(409, "bag_too_large", f"An order can hold up to {MaxLines} lines.")
        # The validation service's verdict travels with the order; a confirmed suggestion replaces the address.
        Validation = Addr["validation"]
        Address = Addr["address"]
        if Body.get("use_suggested_address") and Validation.get("suggestion"):
            Address, Validation = Addressing.Normalize(Validation["suggestion"]), {**Validation, "status": "corrected"}
        Status = Validation.get("status") or "unverified"
        if Status not in ("unverified", "verified", "corrected", "failed"):
            Status = "unverified"
        OrderId, T = NewId("ord"), Now()
        Promo = Q["promo"]
        PromoSnapshot = {**{K: Promo[K] for K in ("code", "kind", "value", "discount", "label")}, "original_amount": Q["subtotal"],
                         "final_amount": round(Q["subtotal"] - Promo["discount"], 2)} if Promo else None
        with Db.Transaction() as Conn:
            Conn.execute("INSERT INTO orders (id, owner_account_id, status, payment_status, customer_json, shipping_json, "
                         "address_validation, shipping_method, currency, subtotal, discount, shipping, total, promo_code, promo_json, "
                         "terms_version, terms_accepted_at, client_request_id, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (OrderId, Who.AccountId, "new", "pending", Dumps(Customer),
                          Dumps({**Address, "validation": {K: V for K, V in Validation.items() if K != "suggestion"},
                                 "suggestion": Validation.get("suggestion")}),
                          Status, Method, Q["currency"], Q["subtotal"], Q["discount"], Q["shipping"], Q["total"],
                          Promo["code"] if Promo else None, Dumps(PromoSnapshot) if PromoSnapshot else None,
                          TermsVersion, T, Rid, T, T))
            for N, L in enumerate(Q["lines"], start=1):
                Conn.execute("INSERT INTO order_lines (id, order_id, position, design_id, candidate_id, bag_line_id, customization_id, "
                             "title, ring_id, product_type, material_id, material_label, ring_size, charm_size, quantity, unit_price, "
                             "line_total, currency, pricing_version, image_path, purchase_json) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (NewId("oln"), OrderId, N, L["design_id"], L["candidate_id"], L["bag_line_id"], L["customization_id"],
                              L["title"], L["ring_id"], L["product_type"], L["material_id"], L["material_label"],
                              L["ring_size"] if L["product_type"] == Products.Ring else None,
                              L["charm_size"] if L["product_type"] == Products.Charm else None, L["quantity"],
                              L["unit_price"], L["line_total"], L["currency"], L["pricing_version"], L["image_path"],
                              Dumps(PurchaseSnapshot(L))))
                Conn.execute("DELETE FROM bag_lines WHERE id = ? AND owner_account_id = ?", (L["bag_line_id"], Who.AccountId))
            if Promo:
                self.Promos.Redeem(Conn, Promo["promo_id"])
            Conn.execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                         (OrderId, "placed", Dumps({"total": Q["total"], "lines": len(Q["lines"]), "promo": Promo["code"] if Promo else None}),
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
        for L in Q["lines"]:
            Size = {"product_type": Products.Charm, "charm_size": L["charm_size"]} if L["product_type"] == Products.Charm \
                else {"ring_size": L["ring_size"]}
            Sessions.Record(self.Ctx, Who.AccountId, "order_placed", L["design_id"], order_id=OrderId, order_ref=Ref,
                            candidate_id=L["candidate_id"], ring_id=L["ring_id"], material_id=L["material_id"],
                            **Size, quantity=L["quantity"], unit_price=L["unit_price"],
                            line_total=L["line_total"], currency=L["currency"], promo=Promo["code"] if Promo else None)
        Out = self.Get(Who, OrderId)
        self._Email(Out, SendMail)
        return Out

    def _Email(self, Order: dict, SendMail) -> None:
        if self.Mailer is None or not Order["customer"]["email"]:
            return
        from p3.mail import OrderConfirmationEmail, StaffOrderEmail
        Subject, Body = OrderConfirmationEmail(Order)
        To = Order["customer"]["email"]
        Staff = tuple(getattr(self.Ctx.Settings, "StaffNotifyEmails", ()) or ())

        def Event(Kind: str, Data: dict) -> None:
            self.Ctx.Db.Execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                                (Order["id"], Kind, Dumps(Data), "system", Now()))

        def Send():
            try:
                self.Mailer.Send(To, Subject, Body)
                Event("email", {"to": To, "subject": Subject, "delivery": self.Mailer.Mode})
            except Exception as E:  # noqa: BLE001 — an email problem never undoes an order
                Event("email_failed", {"to": To, "error": str(E)[:200]})
            if Staff:                                            # the people who act on a new order (P3_STAFF_NOTIFY_EMAILS)
                StaffSubject, StaffBody = StaffOrderEmail(Order)
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
        if self.Mailer.Mode != "smtp":
            return {"status": "not_sent", "to": To}
        Kinds = [E["kind"] for E in self.Ctx.Db.All("SELECT kind FROM order_events WHERE order_id = ? AND kind IN ('email', 'email_failed') "
                                                     "ORDER BY id", (OrderId,))]
        return {"status": "sent" if "email" in Kinds else "failed" if "email_failed" in Kinds else "pending", "to": To}

    # ── reading ──────────────────────────────────────────────────────────
    def _Lines(self, OrderId: str) -> list[dict]:
        return self.Ctx.Db.All("SELECT * FROM order_lines WHERE order_id = ? ORDER BY position", (OrderId,))

    def ToJson(self, O: dict, Lines: list[dict], ForCustomer: bool = True) -> dict:
        Ship = ShippingOptions.get(O["shipping_method"], {})
        Shipping = json.loads(O["shipping_json"])
        Pay = json.loads(O["payment_json"] or "{}")
        Out = {
            "id": O["id"], "ref": OrderRef(O["order_no"]), "created_at": O["created_at"], "updated_at": O["updated_at"],
            "status": O["status"], "status_label": StatusLabels.get(O["status"], O["status"]),
            "payment_status": O["payment_status"], "payment_label": Payments.Labels.get(O["payment_status"], O["payment_status"]),
            "payment_message": Pay.get("message") or "", "payment_provider": O["payment_provider"],
            "customer": json.loads(O["customer_json"]),
            "address": {K: Shipping.get(K, "") for K in Addressing.Fields}, "address_lines": Addressing.Lines(Shipping),
            "address_validation": O["address_validation"], "address_validation_detail": Shipping.get("validation"),
            "shipping_method": O["shipping_method"], "shipping_label": Ship.get("label"), "shipping_eta": Ship.get("eta"),
            "currency": O["currency"], "subtotal": O["subtotal"], "discount": O["discount"], "shipping": O["shipping"], "total": O["total"],
            "promo_code": O["promo_code"], "promo": json.loads(O["promo_json"]) if O["promo_json"] else None,
            "terms_version": O["terms_version"], "terms_accepted_at": O["terms_accepted_at"],
            "confirmation_email": self.EmailState(O["id"], json.loads(O["customer_json"]).get("email")),
            "count": sum(L["quantity"] for L in Lines),
            # Which products the order holds — both for a mixed order
            "product_types": sorted({L.get("product_type") or Products.Ring for L in Lines}, key=Products.All.index),
            "lines": [{"id": L["id"], "design_id": L["design_id"], "candidate_id": L["candidate_id"], "title": L["title"],
                       "ring_id": L["ring_id"], "material_id": L["material_id"], "material_label": L["material_label"],
                       "product_type": L.get("product_type") or Products.Ring, "charm_size": L.get("charm_size"),
                       "size_label": Products.SizeLabel(L.get("product_type") or Products.Ring, L["ring_size"], L.get("charm_size")),
                       "ring_size": L["ring_size"], "quantity": L["quantity"], "unit_price": L["unit_price"],
                       "line_total": L["line_total"], "currency": L["currency"], "image_url": self.Ctx.AssetUrl(L["image_path"])}
                      for L in Lines],
        }
        if not ForCustomer:
            Out["owner_account_id"] = O["owner_account_id"]
            Out["notes"] = O["notes"]
            Out["payment_ref"] = O["payment_ref"]
            Out["client_request_id"] = O["client_request_id"]
        return Out

    def Get(self, Who: Principal, OrderId: str) -> dict:
        O = self.Ctx.Db.One("SELECT * FROM orders WHERE id = ? AND owner_account_id = ?", (OrderId, Who.AccountId))
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
        History = []
        for E in self.Ctx.Db.All("SELECT kind, data_json, created_at FROM order_events WHERE order_id = ? AND kind IN ('placed', 'status') "
                                 "ORDER BY id", (O["id"],)):
            To = "new" if E["kind"] == "placed" else json.loads(E["data_json"] or "{}").get("to")
            History.append({"status": To, "label": "Order placed" if E["kind"] == "placed" else StatusLabels.get(To, To), "at": E["created_at"]})
        J["status_history"] = History
        Quote = self.Ctx.Db.One("SELECT request_no FROM quote_requests WHERE order_id = ?", (O["id"],))
        J["quote_ref"] = QuoteRef(Quote["request_no"]) if Quote else None
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
    def RequestQuote(self, Who: Principal, Body: dict, SendMail=None) -> dict:
        Db = self.Ctx.Db
        Customer, Problems = self.ValidateCustomer(Body.get("customer"))
        if Problems:
            raise _Invalid(Problems)
        DesignId, CandidateId = str(Body.get("design_id") or ""), str(Body.get("candidate_id") or "")
        D = self.Customize.Images.RequireDesign(Who, DesignId)
        C = self.Customize.Images.RequireReadyCandidate(DesignId, CandidateId)
        Mat = self.Ctx.Catalog.Get(str(Body.get("material_id") or ""))
        if Mat is None:
            raise HttpError(400, "unknown_material", "Unknown material.")
        Charm = (D.get("product_type") or Products.Ring) == Products.Charm
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
        Out["email_status"] = "not_configured" if self.Mailer is None else ("pending" if self.Mailer.Mode == "smtp" else "not_sent")
        if self.Mailer is not None:
            from p3.mail import QuoteRequestEmail, StaffQuoteEmail
            Subject, Html = QuoteRequestEmail(Out)
            Staff = tuple(getattr(self.Ctx.Settings, "StaffNotifyEmails", ()) or ())
            Mode = self.Mailer.Mode

            def Send():
                try:
                    self.Mailer.Send(Customer["email"], Subject, Html)
                    QuoteEvent(Db, Id, "email", {"type": "request_confirmation", "to": Customer["email"], "delivery": Mode})
                except Exception as E:  # noqa: BLE001
                    QuoteEvent(Db, Id, "email_failed", {"type": "request_confirmation", "to": Customer["email"], "error": str(E)[:200]})
                if Staff:
                    StaffSubject, StaffHtml = StaffQuoteEmail(Out)
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
        it is reported as `latest` so the admin sees what exists and can prepare the right one."""
        from p3.production3d import ProductionState
        Rows = self.Ctx.Db.All("SELECT s.id, s.status, s.production_size, s.material_id, s.candidate_id, s.accepted_at, r.integrity FROM session_3d s "
                               "LEFT JOIN raw_geometry r ON r.mesh_id = s.mesh_id WHERE s.design_id = ? ORDER BY s.created_at DESC", (L["design_id"],))
        Done = ("measured", "needs_review")
        Size = LineSize(L)                               # the US size of a ring, the mm size of a charm (None: no match)

        def SameSize(R):
            return Size is not None and R["production_size"] is not None and float(R["production_size"]) == float(Size)
        Match = next((R for R in Rows if R["status"] in Done and SameSize(R) and R["material_id"] == L["material_id"]), None)
        Pending = next((R for R in Rows if R["status"] not in Done + ("failed", "cancelled") and SameSize(R)
                        and R["material_id"] == L["material_id"]), None)
        Latest = Rows[0] if Rows else None
        Model = self.Ctx.Db.One("SELECT m.candidate_id FROM meshes m JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id "
                                "WHERE b.design_id = ? AND m.status = 'ready' ORDER BY m.created_at DESC LIMIT 1", (L["design_id"],))
        Chosen = Match or Pending
        return {"three_d_id": Chosen["id"] if Chosen else None,
                "three_d_status": Chosen["status"] if Chosen else None,
                "three_d_state": ProductionState(Chosen["status"], Chosen["integrity"], bool(Chosen["accepted_at"])) if Chosen else None,
                "three_d_match": bool(Match),
                "has_model": Model is not None,
                # the modelled option: the one Hi3D model per design may be of another option than the ordered one
                "model_ring_id": RingIds.CandidateRef(self.Ctx.Db, Model["candidate_id"]) if Model else None,
                "three_d_latest": {"id": Latest["id"], "size": Latest["production_size"], "material_id": Latest["material_id"],
                                   "state": ProductionState(Latest["status"], Latest["integrity"], bool(Latest["accepted_at"]))} if Latest and not Chosen else None}

    def PrepareLine3D(self, OrderId: str, LineId: str, By: str) -> dict:
        """The STL for an ordered ring must be the ordered size and material. Reuse the design's model
        (arithmetic, no Hi3D call) when the matching result does not exist yet; never start a paid
        Hi3D request from here — a design without a model is generated on its session page."""
        O = self.AdminGet(OrderId)
        L = next((X for X in O["lines"] if X["id"] == LineId), None)
        if L is None:
            raise HttpError(404, "order_line_not_found", "Order line not found.")
        if L["three_d_id"]:
            return {"line": L, "three_d_id": L["three_d_id"], "prepared": False}
        if not L["has_model"]:
            raise HttpError(409, "no_model", f"{L['title']} ({L['ring_id']}) has no 3D model yet. Generate it on the session page first "
                                             "(a paid Hi3D call), then prepare the STL here.")
        # The STL is prepared from the option the customer ordered — never from another option's model
        if not self.Ctx.Db.One("SELECT 1 AS x FROM meshes WHERE candidate_id = ? AND status = 'ready'", (L["candidate_id"],)):
            raise HttpError(409, "model_of_other_option",
                            f"The 3D model of this design is of option {L['model_ring_id'] or '—'}; this line was ordered as "
                            f"{L['ring_id']}. Generate a model for the ordered option on the session page (a paid Hi3D call), "
                            "then prepare the STL here.")
        if self.Production is None:
            raise HttpError(503, "production_unavailable", "3D production is not available.")
        Customer = (Sessions.Summaries(self.Ctx, SessionIds=[L["session_id"]]) or [None])[0]
        T = self.Production.Request(L["design_id"], ProductionSize=LineSize(L), MaterialId=L["material_id"],
                                    CandidateId=L["candidate_id"], RequestedBy=By, Customer=Customer)
        self.Ctx.Db.Execute("INSERT INTO order_events (order_id, kind, data_json, by, created_at) VALUES (?,?,?,?,?)",
                            (O["id"], "3d_prepared", Dumps({"line_id": LineId, "session_3d_id": T["id"], "ring_size": L["ring_size"],
                                                            "charm_size": L.get("charm_size"), "product_type": L.get("product_type"),
                                                            "material_id": L["material_id"], "reused_model": bool(T.get("raw_available"))}), By, Now()))
        Fresh = next(X for X in self.AdminGet(OrderId)["lines"] if X["id"] == LineId)
        return {"line": Fresh, "three_d_id": T["id"], "prepared": True}

    @staticmethod
    def _AdminWhere(Status: str | None, Payment: str | None, Query: str | None, Product: str | None,
                    OpenOnly: bool = False) -> tuple[str, list]:
        """The Admin's order filters in SQL: status, payment, product, still open, and the search box — Order ID,
        customer name or email, promo code, a line's design name, Ring / Charm ID or design id."""
        Where, Params = [], []
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
                  Product: str | None = None, Offset: int = 0, OpenOnly: bool = False) -> list[dict]:
        """One page of orders, newest first (Limit None: every match)."""
        Where, Params = self._AdminWhere(Status, Payment, Query, Product, OpenOnly)
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
                   Product: str | None = None) -> int:
        Where, Params = self._AdminWhere(Status, Payment, Query, Product)
        return self.Ctx.Db.One("SELECT COUNT(*) AS n FROM orders o" + Where, tuple(Params))["n"]

    def AdminGet(self, OrderId: str) -> dict:
        O = self.Ctx.Db.One("SELECT * FROM orders WHERE id = ? OR order_no = ?", (OrderId, _OrderNo(OrderId)))
        if O is None:
            raise HttpError(404, "order_not_found", "Order not found.")
        Lines = self._Lines(O["id"])
        J = self.ToJson(O, Lines, ForCustomer=False)
        for L in J["lines"]:
            L.update(self._ThreeDForLine(L))
            Use = self.Ctx.Db.One("SELECT id FROM gallery_uses WHERE design_id = ? AND owner_account_id = ?", (L["design_id"], O["owner_account_id"]))
            L["session_id"] = Use["id"] if Use else L["design_id"]       # the customer's journey in Sessions
        J["three_d_state"] = _WorstState([L["three_d_state"] for L in J["lines"]])
        J["events"] = [{**E, "data": json.loads(E.pop("data_json") or "{}")} for E in
                       self.Ctx.Db.All("SELECT * FROM order_events WHERE order_id = ? ORDER BY id", (O["id"],))]
        J["allowed_statuses"] = StatusOrder + ["cancelled"]
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

    def SetStatus(self, OrderId: str, Status: str, By: str, Note: str = "", Force: bool = False) -> dict:
        """Production and every later status need each line's 3D result for the ordered size and material to be
        complete (measured and not flagged, or a flagged one accepted by the Admin). Force with a note records an
        exception (a piece made outside the pipeline) instead of letting a flagged or missing result slip through."""
        O = self.AdminGet(OrderId)
        if Status not in StatusOrder + ["cancelled"]:
            raise HttpError(400, "invalid_status", "Unknown order status.")
        if O["status"] == "completed" and Status != "completed":
            raise HttpError(409, "order_completed", "A completed order cannot change status.")
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
        self._SetStatus(O["id"], Status, By, Note)
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
        if Status == "paid" and O["status"] == "new":
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

    def CreateFromQuote(self, Q: dict, Offer: dict, AddressRaw: dict, Method: str, Note: str = "", SendMail=None) -> dict:
        """An approved quote becomes an order: the quoted piece at the quoted price (one line), the customer of the
        request, the address and the terms given on approval. One order per quote (a repeated approval returns it);
        payment as for every order — never faked."""
        Db = self.Ctx.Db
        Rid, Ref = f"quote:{Q['id']}", QuoteRef(Q["request_no"])
        Existing = Db.One("SELECT id FROM orders WHERE owner_account_id = ? AND client_request_id = ?", (Q["owner_account_id"], Rid))
        if Existing:
            return self._OrderJson(Existing["id"])
        Addr = self.ValidateAddress(AddressRaw)
        Problems = list(Addr["problems"])
        if Method not in ShippingOptions:
            Problems.append({"field": "shipping_method", "message": "Please choose a delivery option."})
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
        Total = round(Line["line_total"] + Ship["price"], 2)
        Notes = f"From quote {Ref} (version {Offer['version']})." + (f" Customer's note: {Note}" if Note else "")
        try:
            with Db.Transaction() as Conn:
                Conn.execute("INSERT INTO orders (id, owner_account_id, status, payment_status, customer_json, shipping_json, "
                             "address_validation, shipping_method, currency, subtotal, discount, shipping, total, promo_code, promo_json, "
                             "terms_version, terms_accepted_at, client_request_id, notes, created_at, updated_at) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (OrderId, Q["owner_account_id"], "new", "pending", Q["customer_json"],
                              Dumps({**Addr["address"], "validation": {K: V for K, V in Validation.items() if K != "suggestion"},
                                     "suggestion": Validation.get("suggestion")}),
                              Status, Method, Line["currency"], Line["line_total"], 0.0, Ship["price"], Total, None, None,
                              TermsVersion, T, Rid, Notes, T, T))
                Conn.execute("INSERT INTO order_lines (id, order_id, position, design_id, candidate_id, bag_line_id, customization_id, "
                             "title, ring_id, product_type, material_id, material_label, ring_size, charm_size, quantity, unit_price, "
                             "line_total, currency, pricing_version, image_path, purchase_json) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (NewId("oln"), OrderId, 1, Line["design_id"], Line["candidate_id"], None, None, Line["title"],
                              Line["ring_id"], Product, Line["material_id"], Line["material_label"], Line["ring_size"],
                              Line["charm_size"], Qty, Unit, Line["line_total"], Line["currency"], Line["pricing_version"],
                              Img["asset_path"] if Img else None, Dumps(Snapshot)))
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
        self._Email(Out, SendMail)
        return Out

    def _OrderJson(self, OrderId: str) -> dict:
        O = self.Ctx.Db.One("SELECT * FROM orders WHERE id = ?", (OrderId,))
        return self.ToJson(O, self._Lines(OrderId))

    def Summary(self, ExcludeMock: bool = True, Since: str | None = None) -> dict:
        """Dashboard numbers: counts by status and payment, revenue of live orders, open quote requests.
        Since (ISO) limits the orders to a time range; None = all time."""
        Mock = Sessions.MockDesignIds(self.Ctx) if ExcludeMock else set()
        Orders = []
        for O in self.Ctx.Db.All("SELECT * FROM orders" + (" WHERE created_at >= ?" if Since else "") + " ORDER BY created_at DESC",
                                 (Since,) if Since else ()):
            Lines = self._Lines(O["id"])
            if ExcludeMock and Lines and all(L["design_id"] in Mock for L in Lines):
                continue
            Orders.append(O)
        Live = [O for O in Orders if O["status"] != "cancelled"]
        return {"orders": len(Orders), "open": sum(1 for O in Live if O["status"] not in ("completed",)),
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
    return {"product": Product, "product_label": Products.Labels[Product], "size": Size,
            "material": {"id": L["material_id"], "label": L["material_label"]},
            "price": {"unit_price": L["unit_price"], "currency": L["currency"], "pricing_version": L["pricing_version"]}}


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
