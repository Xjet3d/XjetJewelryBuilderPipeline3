"""Quote requests (gold, and any piece without a fixed price): the Admin's quote and the customer's decision.

The customer asks for a price (p3/orders.py RequestQuote). The Admin answers in the quote workspace (Admin → Orders →
a quote request): every customer and request detail, where the request notification went and the address the quote
goes to; a price suggestion when the design has a 3D model — its weight at the requested size in the requested
material × that material's price $/g (Admin → Settings → Pricing & Materials); the size, weight, $/g, price, quantity
and validity all editable; a suggested reply to edit; internal notes.

Sending emails the customer the quote with Approve and Decline buttons on a secret link (stored hashed; a revised
quote replaces it, so only the latest quote can be decided). Approve asks for the shipping address and the terms and
turns the quote into an order at the quoted price — payment is arranged after, as for every order. Decline marks the
quote rejected. Every step — the request, its emails, notes, each quote sent, the decision and the customer's note —
is kept in the quote's history (quote_events).
"""

import hashlib
import html
import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from p3 import addressing as Addressing
from p3 import charmgeometry as CharmGeo
from p3 import media as Media
from p3 import products as Products
from p3 import ringids as RingIds
from p3.context import Context, HttpError
from p3.db import Dumps, Now
from p3.geometry import Scaled, UsSizeToInnerDiameterMm
from p3.orders import OrderRef, QuoteEvent, QuoteManualStatuses, QuoteOpen, QuoteRef, ShippingOptions
from p3.settings import WebDir

DefaultValidDays = 14
MaxValidDays = 180
DefaultRingSize = 10.0
MaxQuantity = 10


def _Hash(Token: str) -> str:
    return hashlib.sha256(("quote:" + (Token or "").strip()).encode("utf-8")).hexdigest()


def _Today() -> date:
    return datetime.now(timezone.utc).date()


def _QuoteNo(Ref: str) -> int:
    M = re.fullmatch(r"(?:Q-)?(\d+)", (Ref or "").strip().upper())
    return int(M.group(1)) if M else -1


def _Num(Raw, Label: str, Hi: float, Required: bool = False) -> float | None:
    if Raw is None or (isinstance(Raw, str) and not Raw.strip()):
        if Required:
            raise HttpError(400, "invalid_offer", f"{Label} is required.")
        return None
    try:
        V = float(Raw)
    except (TypeError, ValueError):
        raise HttpError(400, "invalid_offer", f"{Label} must be a number.")
    if not 0 < V <= Hi:
        raise HttpError(400, "invalid_offer", f"{Label} must be more than 0 and at most {Hi:,g}.")
    return V


def _Money(V) -> str:
    return f"${float(V):,.2f}"


def _E(V) -> str:
    return html.escape("" if V is None else str(V), quote=True)


class QuoteDesk:
    def __init__(self, Ctx: Context, Orders, Mailer=None):
        self.Ctx, self.Orders, self.Mailer = Ctx, Orders, Mailer

    # ── reading ──────────────────────────────────────────────────────────
    def _Row(self, RequestId: str) -> dict:
        R = self.Ctx.Db.One("SELECT * FROM quote_requests WHERE id = ? OR request_no = ?", (RequestId, _QuoteNo(RequestId)))
        if R is None:
            raise HttpError(404, "request_not_found", "Quote request not found.")
        return R

    def History(self, RequestId: str) -> list[dict]:
        return [{**E, "data": json.loads(E.pop("data_json") or "{}")}
                for E in self.Ctx.Db.All("SELECT id, kind, data_json, by, created_at FROM quote_events WHERE request_id = ? "
                                         "ORDER BY id", (RequestId,))]

    @staticmethod
    def _Offer(R: dict) -> dict | None:
        return json.loads(R["offer_json"]) if R.get("offer_json") else None

    def _SizeLabel(self, R: dict, Size) -> str | None:
        Product = R.get("product_type") or Products.Ring
        if Size is None:
            return None
        return Products.SizeLabel(Product, None if Product == Products.Charm else Size, Size if Product == Products.Charm else None)

    def Suggestion(self, R: dict) -> dict:
        """The price the design's 3D model suggests: its weight at the requested size in the requested material × that
        material's price $/g. Without a 3D model (or a price $/g), why not — the Admin enters the numbers."""
        Ctx, Db = self.Ctx, self.Ctx.Db
        Product = R.get("product_type") or Products.Ring
        Charm = Product == Products.Charm
        Mat = Ctx.Catalog.Get(R["material_id"])
        Book = Ctx.CharmPrices if Charm else Ctx.MaterialPrices
        PerG = (Book.Row(R["material_id"]) or {}).get("price_per_g")
        Asked = R.get("charm_size") if Charm else R.get("ring_size")
        Out = {"available": False, "price_per_g": PerG, "density_g_cm3": Mat.DensityGCm3 if Mat else None,
               "weight_g": None, "unit_price": None, "size": Asked, "basis": None, "warning": None}
        Rows = Db.All("SELECT s.id, s.candidate_id, s.production_size, r.measurement_json FROM session_3d s "
                      "JOIN raw_geometry r ON r.mesh_id = s.mesh_id WHERE s.design_id = ? AND r.status = 'measured' "
                      "AND s.status IN ('measured', 'needs_review') ORDER BY (s.candidate_id = ?) DESC, s.updated_at DESC",
                      (R["design_id"], R["candidate_id"]))
        for S in Rows:
            M = json.loads(S["measurement_json"] or "{}")
            try:
                if Charm:
                    if M.get("method_version") != CharmGeo.CharmMethodVersion:
                        continue
                    Size = float(Asked if Asked is not None else S["production_size"])
                    Vol = CharmGeo.CharmScaled(M, Products.Charm3DHeight(Size))["volume_mm3"]
                else:
                    if not M.get("bore_ok") or not M.get("inner_diameter"):
                        continue
                    Size = float(Asked if Asked is not None else (S["production_size"] or DefaultRingSize))
                    Vol = Scaled(M, UsSizeToInnerDiameterMm(Size))["volume_mm3"]
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
            if not Mat or not Vol:
                continue
            Weight = round(Vol / 1000.0 * Mat.DensityGCm3, 2)
            Ref = RingIds.CandidateRef(Db, S["candidate_id"])
            Out.update({"weight_g": Weight, "volume_mm3": round(Vol, 1), "size": Size, "model_ring_id": Ref,
                        "same_option": S["candidate_id"] == R["candidate_id"], "session_3d_id": S["id"],
                        "unit_price": round(Weight * PerG, 2) if PerG else None, "available": bool(PerG),
                        "basis": f"3D model of {Ref or 'this design'} at {self._SizeLabel(R, Size)}: {Vol:,.0f} mm³ × "
                                 f"{Mat.DensityGCm3:g} g/cm³ = {Weight:g} g"})
            if not PerG:
                Out["warning"] = f"No price $/g is set for {R['material_label']} (Admin → Settings → Pricing & Materials): enter it."
            elif not Out["same_option"]:
                Out["warning"] = f"The 3D model is of {Ref}, not the quoted option {R['ring_id']}: the weight is an estimate."
            elif not M.get("closed_heuristic", True):
                Out["warning"] = "The 3D volume check suggests the mesh may not be closed: check the weight."
            return Out
        Out["warning"] = ("This design has no 3D model yet: enter the weight, or generate a 3D model on the session page "
                          "(a paid Hi3D call)." + ("" if PerG else f" No price $/g is set for {R['material_label']} either."))
        return Out

    def SuggestedReply(self, R: dict) -> str:
        """The reply the Admin starts from: no numbers in it — the quote box of the email carries those, so editing the
        price never leaves the text behind."""
        C = json.loads(R["customer_json"])
        Name = (C.get("first_name") or "").strip()
        Noun = Products.Labels[R.get("product_type") or Products.Ring].lower()
        return "\n\n".join([
            f"Hello {Name}," if Name else "Hello,",
            f"Thank you for your interest in {R['title']} in {R['material_label']}. Our quote for your {Noun} is below.",
            "To go ahead, approve the quote with the button below: we will ask for your shipping address and confirm your "
            "order. Payment is arranged with you before production starts, and production takes 7–10 business days.",
            "If you would like a change, decline the quote and tell us what you have in mind: we will gladly prepare a new one.",
            "Kind regards,\nThe XJet Atelier team"])

    def AdminDetail(self, RequestId: str) -> dict:
        R = self._Row(RequestId)
        Q = self.Orders.QuoteRequestJson(R)
        History = self.History(R["id"])
        Product = Q["product_type"]
        Charm = Product == Products.Charm
        Staff = list(self.Ctx.Settings.StaffNotifyEmails or ())
        Confirm = next((E for E in reversed(History) if E["kind"] in ("email", "email_failed")
                        and E["data"].get("type") == "request_confirmation"), None)
        Offer = self._Offer(R)
        Sug = self.Suggestion(R)
        Size = (Offer or {}).get("size")
        if Size is None:
            Size = R.get("charm_size") if Charm else R.get("ring_size")
        if Size is None:
            Size = Sug.get("size")
        Order = self.Ctx.Db.One("SELECT id, order_no, status, payment_status FROM orders WHERE id = ?", (R["order_id"],)) \
            if R.get("order_id") else None
        return {**Q, "email_to": Q["customer"]["email"],
                "notification": {"staff": Staff,
                                 "customer_confirmation": Confirm and {"to": Confirm["data"].get("to"), "at": Confirm["created_at"],
                                                                       "delivery": Confirm["data"].get("delivery"),
                                                                       "failed": Confirm["kind"] == "email_failed"}},
                "mail_mode": getattr(self.Mailer, "Mode", None),
                "offer": Offer and {K: V for K, V in Offer.items() if K != "link_hash"},
                "offer_expired": bool(Offer and Offer.get("valid_until") and date.fromisoformat(Offer["valid_until"]) < _Today()),
                "history": History, "suggestion": Sug, "suggested_reply": self.SuggestedReply(R),
                "defaults": {"size": Size, "quantity": R["quantity"],
                             "valid_until": (_Today() + timedelta(days=DefaultValidDays)).isoformat()},
                "sizes": [float(S) for S in (self.Ctx.Products.CharmSizes if Charm else self.Ctx.Catalog.RingSizes)],
                "statuses": list(QuoteManualStatuses), "can_send": R["status"] in QuoteOpen,
                "decision": {"at": R.get("decided_at"), "note": R.get("decision_note")} if R.get("decided_at") else None,
                "order": Order and {"id": Order["id"], "ref": OrderRef(Order["order_no"]), "status": Order["status"],
                                    "payment_status": Order["payment_status"]}}

    # ── the Admin's actions ──────────────────────────────────────────────
    def AddNote(self, RequestId: str, Note: str, By: str) -> dict:
        R = self._Row(RequestId)
        Note = (Note or "").strip()[:2000]
        if not Note:
            raise HttpError(400, "note_required", "Write the note first.")
        QuoteEvent(self.Ctx.Db, R["id"], "note", {"note": Note}, By)
        self.Ctx.Db.Execute("UPDATE quote_requests SET updated_at = ? WHERE id = ?", (Now(), R["id"]))
        return self.AdminDetail(R["id"])

    def SendOffer(self, RequestId: str, Body: dict, By: str, Origin: str, SendMail=None) -> dict:
        """The Admin's quote: validated, kept as the request's current offer (a revised quote replaces the link of the
        one before), emailed to the customer with Approve and Decline, and written to the history."""
        Db, R = self.Ctx.Db, self._Row(RequestId)
        if R["status"] not in QuoteOpen:
            raise HttpError(409, "quote_decided", "This quote request is already decided or closed.")
        Product = R.get("product_type") or Products.Ring
        Charm = Product == Products.Charm
        Size = _Num(Body.get("size"), "The size", 1000, Required=True)
        if Charm and not self.Ctx.Products.IsValidCharmSize(Size):
            raise HttpError(400, "invalid_offer", "Choose one of the charm sizes on offer.")
        if not Charm and not self.Ctx.Catalog.IsValidSize(Size):
            raise HttpError(400, "invalid_offer", "Choose a standard US ring size.")
        Weight = _Num(Body.get("weight_g"), "The weight", 10_000)
        PerG = _Num(Body.get("price_per_g"), "The price per gram", 100_000)
        Unit = round(_Num(Body.get("unit_price"), "The price", 1_000_000, Required=True), 2)
        Qty = Body.get("quantity", R["quantity"])
        if isinstance(Qty, bool) or not isinstance(Qty, (int, float)) or int(Qty) != Qty or not 1 <= int(Qty) <= MaxQuantity:
            raise HttpError(400, "invalid_offer", f"The quantity must be a whole number from 1 to {MaxQuantity}.")
        Qty = int(Qty)
        try:
            Valid = date.fromisoformat(str(Body.get("valid_until") or ""))
        except ValueError:
            raise HttpError(400, "invalid_offer", "Give the date the quote is valid until.")
        if not _Today() <= Valid <= _Today() + timedelta(days=MaxValidDays):
            raise HttpError(400, "invalid_offer", f"The quote must be valid from today up to {MaxValidDays} days.")
        Message = str(Body.get("message") or "").strip()[:5000] or self.SuggestedReply(R)
        Note = str(Body.get("note") or "").strip()[:2000]
        Prev = self._Offer(R) or {}
        Token = secrets.token_urlsafe(24)
        C = json.loads(R["customer_json"])
        Offer = {"version": int(Prev.get("version") or 0) + 1, "size": Size, "size_label": self._SizeLabel(R, Size),
                 "weight_g": Weight, "price_per_g": PerG, "unit_price": Unit, "quantity": Qty, "total": round(Unit * Qty, 2),
                 "currency": "USD", "valid_until": Valid.isoformat(), "message": Message, "sent_at": Now(), "sent_by": By,
                 "to": C["email"], "link": f"{Origin}{self.Ctx.Settings.BasePath}/quote?token={Token}", "link_hash": _Hash(Token)}
        with Db.Transaction() as Conn:
            Conn.execute("UPDATE quote_requests SET offer_json = ?, link_hash = ?, status = 'quoted', updated_at = ? WHERE id = ?",
                         (Dumps(Offer), Offer["link_hash"], Now(), R["id"]))
            if Note:
                QuoteEvent(Conn, R["id"], "note", {"note": Note}, By)
            QuoteEvent(Conn, R["id"], "offer_sent", {**{K: Offer[K] for K in ("version", "size", "size_label", "weight_g", "price_per_g",
                                                                                "unit_price", "quantity", "total", "valid_until", "message", "to",
                                                                                "link_hash")},
                                                     "revised": Offer["version"] > 1}, By)
        self._Email(R, Offer, SendMail)
        return self.AdminDetail(R["id"])

    def _Email(self, R: dict, Offer: dict, SendMail=None) -> None:
        if self.Mailer is None:
            QuoteEvent(self.Ctx.Db, R["id"], "email_failed", {"type": "offer", "to": Offer["to"], "error": "no mail delivery"}, "system")
            return
        from p3.mail import QuoteOfferEmail
        Subject, Body = QuoteOfferEmail(self.Orders.QuoteRequestJson(R), Offer)
        Mode = getattr(self.Mailer, "Mode", None)

        def Send():
            try:
                self.Mailer.Send(Offer["to"], Subject, Body)
                QuoteEvent(self.Ctx.Db, R["id"], "email", {"type": "offer", "to": Offer["to"], "version": Offer["version"],
                                                           "delivery": Mode}, "system")
            except Exception as E:  # noqa: BLE001 — the quote stays sent; the Admin sees that the email failed
                QuoteEvent(self.Ctx.Db, R["id"], "email_failed", {"type": "offer", "to": Offer["to"], "version": Offer["version"],
                                                                  "error": str(E)[:200]}, "system")
        (SendMail or (lambda Fn: Fn()))(Send)

    # ── the customer's decision (the emailed link) ───────────────────────
    def _ByToken(self, Token: str) -> tuple[dict | None, str]:
        """(request, "current") for the latest quote's link; (request, "superseded") for an earlier quote's link."""
        if not Token or len(Token) > 200:
            return None, "invalid"
        H = _Hash(Token)
        R = self.Ctx.Db.One("SELECT * FROM quote_requests WHERE link_hash = ?", (H,))
        if R:
            return R, "current"
        E = self.Ctx.Db.One("SELECT request_id FROM quote_events WHERE kind = 'offer_sent' AND json_extract(data_json, '$.link_hash') = ?",
                            (H,))
        return (self._Row(E["request_id"]), "superseded") if E else (None, "invalid")

    def Page(self, Token: str, Action: str = "", Problems: list | None = None, Form: dict | None = None) -> tuple[str, int]:
        R, State = self._ByToken(Token)
        if R is None:
            return self._Render("This link is not valid", "<p>The link may be incomplete. Please use the button in your quote "
                                                          "email, or contact us.</p>", Bad=True), 404
        Ref = QuoteRef(R["request_no"])
        Offer = self._Offer(R) or {}
        if State == "superseded":
            return self._Render("A newer quote replaced this one",
                                f"<p>We sent you a revised quote for {_E(R['title'])} ({_E(Ref)}). Please use the buttons in the "
                                "most recent email.</p>"), 200
        if R["status"] == "approved":
            O = self.Ctx.Db.One("SELECT order_no FROM orders WHERE id = ?", (R["order_id"],)) if R.get("order_id") else None
            return self._Render("You approved this quote",
                                f"<p>Your order <strong>{_E(OrderRef(O['order_no']) if O else '')}</strong> was placed from quote "
                                f"{_E(Ref)}. We will contact you to arrange payment before production starts.</p>"), 200
        if R["status"] == "rejected" and R.get("decided_at"):
            return self._Render("You declined this quote", f"<p>Thank you for letting us know. If you would like a new quote for "
                                                           f"{_E(R['title'])}, contact us and quote {_E(Ref)}.</p>"), 200
        if R["status"] not in QuoteOpen or not Offer:
            return self._Render("This quote is closed", f"<p>Quote {_E(Ref)} is no longer open. Please contact us if you have a "
                                                        "question.</p>"), 200
        Expired = date.fromisoformat(Offer["valid_until"]) < _Today()
        Body = self._QuoteBox(R, Offer)
        if Expired:
            return self._Render("This quote has expired", f"<p>The quote was valid until {_E(Offer['valid_until'])}. Contact us and "
                                                          f"quote {_E(Ref)} for a new one.</p>" + Body), 200
        Forms = [self._ApproveForm(Token, R, Problems if Action != "reject" else None, Form),
                 self._RejectForm(Token, Problems if Action == "reject" else None, Form)]
        if Action == "reject":
            Forms.reverse()
        Msg = "".join(f"<p class='msg'>{_E(P).replace(chr(10), '<br>')}</p>" for P in (Offer.get("message") or "").split("\n\n") if P.strip())
        return self._Render(f"Your quote {_E(Ref)}", Msg + Body + "".join(Forms), Wide=True), (400 if Problems else 200)

    def _QuoteBox(self, R: dict, Offer: dict) -> str:
        Asset = self.Ctx.Db.One("SELECT asset_path FROM candidates WHERE id = ?", (R["candidate_id"],))
        Img = Media.ThumbUrl(self.Ctx.AssetUrl(Asset["asset_path"]), 320) if Asset else None   # shown at 110 px; a made width
        Rows = [("Design", f"{R['title']} · {R['ring_id'] or ''}"), ("Material", R["material_label"]),
                ("Size", Offer.get("size_label") or "—"), ("Quantity", str(Offer["quantity"])),
                ("Price per piece", _Money(Offer["unit_price"])), ("Total", _Money(Offer["total"])),
                ("Valid until", Offer["valid_until"])]
        Table = "".join(f"<tr><th>{_E(K)}</th><td>{_E(V)}</td></tr>" for K, V in Rows)
        Picture = f"<img src=\"{_E(Img)}\" alt=\"{_E(R['title'])}\">" if Img else ""
        return (f"<div class='quote'>{Picture}<table>{Table}</table></div>"
                "<p class='small'>Delivery is chosen when you approve: Standard delivery is free, Express is $45. Prices in US dollars; "
                "import duties or VAT, where charged, are paid by the recipient.</p>")

    def _Err(self, Problems: list | None, Field: str) -> str:
        M = next((P["message"] for P in (Problems or []) if P.get("field") == Field), None)
        return f"<span class='err'>{_E(M)}</span>" if M else ""

    def _ApproveForm(self, Token: str, R: dict, Problems: list | None, Form: dict | None) -> str:
        F = Form or {}
        C = json.loads(R["customer_json"])
        V = lambda K, D="": _E(F.get(K, D))
        Country = F.get("country") or "US"
        Opts = "".join(f"<option value='{_E(Code)}'{' selected' if Code == Country else ''}>{_E(Name)}</option>"
                       for Code, Name in Addressing.Countries)
        Method = F.get("shipping_method") or "standard"
        Ship = "".join(f"<label class='opt'><input type='radio' name='shipping_method' value='{_E(K)}'{' checked' if K == Method else ''}>"
                       f"<span><strong>{_E(S['label'])}</strong> · {_E(S['detail'])} · {_E(S['eta'])}</span></label>"
                       for K, S in ShippingOptions.items())
        General = "".join(f"<p class='err'>{_E(P['message'])}</p>" for P in (Problems or []) if not P.get("field"))
        Recipient = F.get("recipient", f"{C.get('first_name', '')} {C.get('last_name', '')}".strip())
        return f"""
<form method="post" action="{_E(self.Ctx.Settings.BasePath)}/quote/approve" class="panel" id="approve">
  <input type="hidden" name="token" value="{_E(Token)}">
  <h2>Approve the quote</h2>
  <p>We turn the quote into an order at the price above and contact you to arrange payment before production starts.</p>
  {General}
  <div class="grid">
    <label>Recipient<input name="recipient" value="{_E(Recipient)}" autocomplete="name">{self._Err(Problems, 'recipient')}</label>
    <label>Address<input name="line1" value="{V('line1')}" autocomplete="address-line1">{self._Err(Problems, 'line1')}</label>
    <label>Apartment, suite (optional)<input name="line2" value="{V('line2')}" autocomplete="address-line2"></label>
    <label>City<input name="city" value="{V('city')}" autocomplete="address-level2">{self._Err(Problems, 'city')}</label>
    <label>State / region<input name="region" value="{V('region')}" autocomplete="address-level1">{self._Err(Problems, 'region')}</label>
    <label>Postal code<input name="postal_code" value="{V('postal_code')}" autocomplete="postal-code">{self._Err(Problems, 'postal_code')}</label>
    <label>Country<select name="country" autocomplete="country">{Opts}</select>{self._Err(Problems, 'country')}</label>
  </div>
  <fieldset><legend>Delivery</legend>{Ship}{self._Err(Problems, 'shipping_method')}</fieldset>
  <label class="check"><input type="checkbox" name="terms" value="1"{' checked' if F.get('terms') else ''}>
    <span>I accept the <a href="{_E(self.Ctx.Settings.BasePath)}/terms" target="_blank" rel="noopener">Terms of Service</a> and understand that the piece is made to order (design and size) and is not returnable
    for change of mind.</span></label>{self._Err(Problems, 'terms')}
  <label>A note for our team (optional)<textarea name="note" rows="3" maxlength="1000">{V('note') if (Form or {}).get('_form') == 'approve' else ''}</textarea></label>
  <button type="submit">Approve and place the order</button>
</form>"""

    def _RejectForm(self, Token: str, Problems: list | None, Form: dict | None) -> str:
        Note = _E((Form or {}).get("note", "")) if (Form or {}).get("_form") == "reject" else ""
        General = "".join(f"<p class='err'>{_E(P['message'])}</p>" for P in (Problems or []))
        return f"""
<form method="post" action="{_E(self.Ctx.Settings.BasePath)}/quote/reject" class="panel" id="reject">
  <input type="hidden" name="token" value="{_E(Token)}">
  <h2>Decline the quote</h2>
  {General}
  <label>Tell us why, or what you would like instead (optional)<textarea name="note" rows="3" maxlength="1000">{Note}</textarea></label>
  <button type="submit" class="secondary">Decline the quote</button>
</form>"""

    def _Render(self, Title: str, Body: str, Bad: bool = False, Wide: bool = False) -> str:
        Page = (WebDir / "quote.html").read_text(encoding="utf-8")
        return (Page.replace("{{CARD_CLASS}}", " ".join(C for C in ("bad" if Bad else "", "wide" if Wide else "") if C))
                .replace("{{TITLE}}", Title).replace("{{BODY}}", Body).replace("{{BASE}}", self.Ctx.Settings.BasePath))

    def Approve(self, Token: str, Form: dict, SendMail=None) -> tuple[str, int]:
        """The customer approves: the address and the terms; the quote becomes an order at the quoted price."""
        R, State = self._ByToken(Token)
        if R is None or State != "current" or R["status"] not in QuoteOpen:
            return self.Page(Token)
        Offer = self._Offer(R) or {}
        if not Offer or date.fromisoformat(Offer["valid_until"]) < _Today():
            return self.Page(Token)
        F = {**Form, "_form": "approve"}
        Problems = []
        if str(Form.get("terms") or "") not in ("1", "on", "true"):
            Problems.append({"field": "terms", "message": "Please accept the Terms of Service to place the order."})
        Address = {K: str(Form.get(K) or "") for K in Addressing.Fields}
        Method = str(Form.get("shipping_method") or "standard")
        Note = str(Form.get("note") or "").strip()[:1000]
        Addr = self.Orders.ValidateAddress(Address)
        Problems += Addr["problems"]
        if Method not in ShippingOptions:
            Problems.append({"field": "shipping_method", "message": "Please choose a delivery option."})
        if Problems:
            return self.Page(Token, "approve", Problems, F)
        try:
            Order = self.Orders.CreateFromQuote(R, Offer, Address, Method, Note, SendMail)
        except HttpError as E:
            return self.Page(Token, "approve", getattr(E, "Problems", None) or [{"field": "", "message": E.Message}], F)
        Db, T = self.Ctx.Db, Now()
        with Db.Transaction() as Conn:
            Conn.execute("UPDATE quote_requests SET status = 'approved', decided_at = ?, decision_note = ?, order_id = ?, updated_at = ? "
                         "WHERE id = ?", (T, Note or None, Order["id"], T, R["id"]))
            QuoteEvent(Conn, R["id"], "approved", {"note": Note, "order_id": Order["id"], "order_ref": Order["ref"],
                                                    "version": Offer["version"], "total": Order["total"],
                                                    "shipping_method": Method, "address_lines": Order["address_lines"]}, "customer")
        Email = self.Orders.EmailState(Order["id"], Order["customer"]["email"])
        Mail = {"sent": "A confirmation email has been sent to you.", "pending": "A confirmation email is on its way to you."}.get(
            Email["status"], "Please keep your order reference.")
        return self._Render("Thank you — your order is placed",
                            f"<p>Quote {_E(QuoteRef(R['request_no']))} is approved and your order <strong>{_E(Order['ref'])}</strong> is "
                            f"placed: {_E(Order['lines'][0]['title'])}, {_E(Order['lines'][0]['material_label'])}, "
                            f"{_E(Order['lines'][0]['size_label'])}, total {_Money(Order['total'])}.</p>"
                            f"<p>{_E(Order.get('payment_message') or '')}</p><p>{_E(Mail)}</p>"), 200

    def Reject(self, Token: str, Form: dict) -> tuple[str, int]:
        R, State = self._ByToken(Token)
        if R is None or State != "current" or R["status"] not in QuoteOpen:
            return self.Page(Token)
        Note = str(Form.get("note") or "").strip()[:1000]
        Db, T = self.Ctx.Db, Now()
        with Db.Transaction() as Conn:
            Conn.execute("UPDATE quote_requests SET status = 'rejected', decided_at = ?, decision_note = ?, updated_at = ? WHERE id = ?",
                         (T, Note or None, T, R["id"]))
            QuoteEvent(Conn, R["id"], "rejected", {"note": Note, "version": (self._Offer(R) or {}).get("version")}, "customer")
        self._NotifyStaff(R, "declined", Note)
        return self._Render("Thank you — we have noted your answer",
                            f"<p>You declined quote {_E(QuoteRef(R['request_no']))}"
                            + (". Your note was passed to our team" if Note else "")
                            + ". If you would like a different quote, contact us and quote its reference.</p>"), 200

    def _NotifyStaff(self, R: dict, Decision: str, Note: str) -> None:
        Staff = tuple(self.Ctx.Settings.StaffNotifyEmails or ())
        if not Staff or self.Mailer is None:
            return
        from p3.mail import StaffQuoteDecisionEmail
        Subject, Body = StaffQuoteDecisionEmail(self.Orders.QuoteRequestJson(R), Decision, Note)
        for Address in Staff:
            try:
                self.Mailer.Send(Address, Subject, Body)
                QuoteEvent(self.Ctx.Db, R["id"], "staff_notified", {"to": Address, "about": Decision}, "system")
            except Exception as E:  # noqa: BLE001
                QuoteEvent(self.Ctx.Db, R["id"], "staff_notify_failed", {"to": Address, "about": Decision, "error": str(E)[:200]}, "system")
