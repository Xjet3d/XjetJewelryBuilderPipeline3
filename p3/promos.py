"""Promo codes: percentage or fixed discounts with validity dates, a usage limit, an optional material
restriction and a minimum subtotal. Validation is server-side only; the order keeps a snapshot of the
code, the original amount, the discount and the final amount (p3/orders.py)."""

import json
import re
from datetime import datetime, timezone

from p3.context import Context, HttpError
from p3.db import Dumps, NewId, Now

CodePattern = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,19}$")
Kinds = ("percent", "fixed")


class PromoError(HttpError):
    def __init__(self, Code: str, Message: str):
        super().__init__(400, Code, Message)


class PromoService:
    def __init__(self, Ctx: Context):
        self.Ctx = Ctx

    # ── admin ────────────────────────────────────────────────────────────
    @staticmethod
    def _Row(R: dict) -> dict:
        return {**R, "active": bool(R["active"]), "materials": json.loads(R["materials_json"]) if R["materials_json"] else None}

    def List(self) -> list[dict]:
        return [self._Row(R) for R in self.Ctx.Db.All("SELECT * FROM promo_codes ORDER BY created_at DESC")]

    def Get(self, PromoId: str) -> dict:
        R = self.Ctx.Db.One("SELECT * FROM promo_codes WHERE id = ?", (PromoId,))
        if R is None:
            raise HttpError(404, "promo_not_found", "Promo code not found.")
        return self._Row(R)

    def _Validate(self, Data: dict) -> dict:
        Code = str(Data.get("code") or "").strip().upper()
        if not CodePattern.match(Code):
            raise HttpError(400, "invalid_promo", "The code must be 3–20 letters, digits or dashes.")
        Kind = str(Data.get("kind") or "").strip()
        if Kind not in Kinds:
            raise HttpError(400, "invalid_promo", "The discount kind must be 'percent' or 'fixed'.")
        try:
            Value = float(Data.get("value"))
        except (TypeError, ValueError):
            raise HttpError(400, "invalid_promo", "Please enter the discount value.") from None
        if Kind == "percent" and not 0 < Value <= 100:
            raise HttpError(400, "invalid_promo", "A percentage discount must be between 0 and 100.")
        if Kind == "fixed" and Value <= 0:
            raise HttpError(400, "invalid_promo", "A fixed discount must be more than 0.")
        Dates = {}
        for K in ("starts_at", "ends_at"):
            V = Data.get(K)
            if V in (None, ""):
                Dates[K] = None
                continue
            try:
                Dates[K] = datetime.fromisoformat(str(V).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="milliseconds")
            except ValueError:
                raise HttpError(400, "invalid_promo", f"{K.replace('_', ' ')} must be a date.") from None
        if Dates["starts_at"] and Dates["ends_at"] and Dates["ends_at"] <= Dates["starts_at"]:
            raise HttpError(400, "invalid_promo", "The end date must be after the start date.")
        Limit = Data.get("usage_limit")
        if Limit not in (None, ""):
            try:
                Limit = int(Limit)
            except (TypeError, ValueError):
                raise HttpError(400, "invalid_promo", "The usage limit must be a whole number.") from None
            if Limit < 1:
                raise HttpError(400, "invalid_promo", "The usage limit must be at least 1.")
        else:
            Limit = None
        Materials = Data.get("materials")
        if Materials:
            if not isinstance(Materials, list) or any(self.Ctx.Catalog.Get(M) is None for M in Materials):
                raise HttpError(400, "invalid_promo", "Unknown material in the restriction list.")
        MinSub = Data.get("min_subtotal")
        MinSub = None if MinSub in (None, "") else float(MinSub)
        return {"code": Code, "kind": Kind, "value": round(Value, 2), **Dates, "usage_limit": Limit,
                "materials_json": Dumps(sorted(Materials)) if Materials else None, "min_subtotal": MinSub,
                "note": str(Data.get("note") or "")[:200], "active": 1 if Data.get("active", True) else 0}

    def Save(self, Data: dict, By: str, PromoId: str | None = None) -> dict:
        V = self._Validate(Data)
        T = Now()
        Dup = self.Ctx.Db.One("SELECT id FROM promo_codes WHERE code = ? AND id != ?", (V["code"], PromoId or ""))
        if Dup:
            raise HttpError(409, "duplicate_promo", f"The code {V['code']} already exists.")
        if PromoId:
            self.Get(PromoId)
            self.Ctx.Db.Execute("UPDATE promo_codes SET code = ?, active = ?, kind = ?, value = ?, starts_at = ?, ends_at = ?, "
                                "usage_limit = ?, materials_json = ?, min_subtotal = ?, note = ?, updated_at = ? WHERE id = ?",
                                (V["code"], V["active"], V["kind"], V["value"], V["starts_at"], V["ends_at"], V["usage_limit"],
                                 V["materials_json"], V["min_subtotal"], V["note"], T, PromoId))
            return self.Get(PromoId)
        Id = NewId("pro")
        self.Ctx.Db.Execute("INSERT INTO promo_codes (id, code, active, kind, value, starts_at, ends_at, usage_limit, usage_count, "
                            "materials_json, min_subtotal, note, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)",
                            (Id, V["code"], V["active"], V["kind"], V["value"], V["starts_at"], V["ends_at"], V["usage_limit"],
                             V["materials_json"], V["min_subtotal"], V["note"], By, T, T))
        return self.Get(Id)

    def SetActive(self, PromoId: str, Active: bool) -> dict:
        self.Get(PromoId)
        self.Ctx.Db.Execute("UPDATE promo_codes SET active = ?, updated_at = ? WHERE id = ?", (1 if Active else 0, Now(), PromoId))
        return self.Get(PromoId)

    # ── checkout ─────────────────────────────────────────────────────────
    def Find(self, Code: str | None) -> dict | None:
        Code = str(Code or "").strip().upper()
        R = self.Ctx.Db.One("SELECT * FROM promo_codes WHERE code = ?", (Code,)) if Code else None
        return self._Row(R) if R else None

    def Evaluate(self, Code: str, Lines: list[dict], AtIso: str | None = None) -> dict:
        """The discount a code gives on these lines (each {material_id, line_total}), or a PromoError
        with a customer-friendly message. Server-side only — the browser never computes a discount."""
        P = self.Find(Code)
        if P is None:
            raise PromoError("promo_unknown", "This promo code is not valid.")
        if not P["active"]:
            raise PromoError("promo_inactive", "This promo code is no longer active.")
        At = AtIso or Now()
        if P["starts_at"] and At < P["starts_at"]:
            raise PromoError("promo_not_started", "This promo code is not valid yet.")
        if P["ends_at"] and At > P["ends_at"]:
            raise PromoError("promo_expired", "This promo code has expired.")
        if P["usage_limit"] is not None and P["usage_count"] >= P["usage_limit"]:
            raise PromoError("promo_exhausted", "This promo code has been fully used.")
        Eligible = [L for L in Lines if not P["materials"] or L["material_id"] in P["materials"]]
        Subtotal = round(sum(L["line_total"] for L in Lines), 2)
        EligibleSubtotal = round(sum(L["line_total"] for L in Eligible), 2)
        if not Eligible:
            Names = ", ".join(self.Ctx.Catalog.Get(M).Label for M in P["materials"] if self.Ctx.Catalog.Get(M))
            raise PromoError("promo_not_applicable", f"This promo code applies to {Names} pieces only.")
        if P["min_subtotal"] and Subtotal < P["min_subtotal"]:
            raise PromoError("promo_minimum", f"This promo code needs a subtotal of at least ${P['min_subtotal']:.0f}.")
        if P["kind"] == "percent":
            Discount = round(EligibleSubtotal * P["value"] / 100.0, 2)
        else:
            Discount = round(min(P["value"], EligibleSubtotal), 2)
        return {"promo_id": P["id"], "code": P["code"], "kind": P["kind"], "value": P["value"],
                "eligible_subtotal": EligibleSubtotal, "discount": Discount,
                "label": f"{P['value']:g}% off" if P["kind"] == "percent" else f"${P['value']:.2f} off"}

    def Redeem(self, Conn, PromoId: str) -> None:
        """Count one use inside the order transaction; the limit is re-checked under the write lock."""
        N = Conn.execute("UPDATE promo_codes SET usage_count = usage_count + 1, updated_at = ? WHERE id = ? "
                         "AND (usage_limit IS NULL OR usage_count < usage_limit)", (Now(), PromoId)).rowcount
        if not N:
            raise PromoError("promo_exhausted", "This promo code has just been fully used.")
