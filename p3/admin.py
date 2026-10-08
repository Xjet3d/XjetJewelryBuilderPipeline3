"""P3 Admin — user / token management, kept apart from the customer site.

  Page:  {base}/admin/            (web/admin.html — never linked from the customer site, noindex)
  API:   {base}/api/admin/...     (every route calls RequireAdmin)

Authorization is a single seam, RequireAdmin(), which returns an AdminPrincipal. Today it accepts
the existing developer key (P3_ADMIN_KEY, as /dev does). When the public site goes live, replace
its body with a proper admin role / company sign-in (e.g. SSO) — the routes do not change.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import asyncio
import json
import logging
import os
import hashlib
import hmac
import re
import time
from statistics import mean

from fastapi import BackgroundTasks, Body, FastAPI, Header, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from p3 import adminauth as AdminAuth
from p3 import merge as Merge
from p3 import naming as Naming
from p3 import charmprices as CharmPrices
from p3.charmprices import CharmPriceError
from p3 import orders as OrdersModule
from p3 import payments as PaymentsModule
from p3 import products as Products
from p3 import ringids as RingIds
from p3 import sessions as Sessions
from p3.accounts import AccountNotFound, DuplicateEmail, RemovedAccount
from p3.auth import RequireDeveloper
from p3.context import Context, HttpError
from p3.aipricing import PriceError
from p3.materialprices import MaterialPriceError
from p3.db import Now
from p3.usage import AccountActivity
from p3 import credits as Credits
from p3 import adminkey as AdminKeys
from p3 import falkey as FalKeys
from p3.registration import PublicOrigin
from p3.modelconfig import ConfigError, ExportText, Models as ModelSpecs, RuntimeInputs, Validate as ValidateConfig
from p3.geometry import Scaled, UsSizeToInnerDiameterMm

Logger = logging.getLogger("p3.admin")


@dataclass(frozen=True)
class AdminPrincipal:
    Id: str            # who is acting (today always "developer-key")
    Method: str        # how they authenticated


def RequireAdmin(Ctx: Context, Authorization: str | None) -> AdminPrincipal:
    """The only admin authorization check. Swap this for an admin role / company auth later."""
    RequireDeveloper(Ctx, Authorization)
    return AdminPrincipal(Id="developer-key", Method="P3_ADMIN_KEY")


def _Errors(Fn):
    """Map account-domain errors to HTTP errors."""
    try:
        return Fn()
    except DuplicateEmail as E:
        raise HttpError(409, "duplicate_email", f"This email already has an account ({E.AccountId}).") from E
    except RemovedAccount as E:
        raise HttpError(409, "removed_account",
                        f"This email belongs to a removed account{' (' + E.Name + ')' if E.Name else ''}. Restore that "
                        "account instead: its history, credits and activity come back with it.",
                        {"account": {"account_id": E.AccountId, "name": E.Name, "removed_at": E.RemovedAt}}) from E
    except AccountNotFound as E:
        raise HttpError(404, "account_not_found", "User not found.") from E
    except ValueError as E:
        raise HttpError(400, "invalid_input", str(E)) from E


def _Detail(Ctx: Context, AccountId: str) -> dict:
    User = _Errors(lambda: Ctx.Accounts.AdminGet(AccountId))
    Identity = Ctx.Accounts.AdminActivity(AccountId)
    App = AccountActivity(Ctx, AccountId, IncludeMock=False)                 # mock activity stays out of the Admin
    Refs = RingIds.CandidateRefs(Ctx.Db, [G["id"] for G in App.get("designs", [])])
    for G in App.get("designs", []):
        for B in G.get("batches", []):
            for C in B["candidates"]:
                C["ring_id"] = Refs.get(C["id"])
    Usage = [U for U in Identity["usage"] if U["provider"] != "mock"]
    # Usage ledger by kind and provider/endpoint — the shape a cost report will aggregate.
    Ledger: dict[tuple, dict] = {}
    for U in Usage:
        if U["kind"] == "generation":
            continue
        Key = (U["kind"], U["provider"] or "unknown", U["endpoint"] or "unknown")
        Row = Ledger.setdefault(Key, {"kind": Key[0], "provider": Key[1], "endpoint": Key[2], "requests": 0,
                                      "units": 0, "cost_usd": None})
        Row["requests"] += 1
        Row["units"] += U["units"]
        if U["cost_usd"] is not None:
            Row["cost_usd"] = (Row["cost_usd"] or 0) + U["cost_usd"]
    Timeline = App["timeline"] + [
        {"at": E["created_at"], "kind": E["kind"], "text": E["detail"]} for E in Identity["events"]]
    Timeline.sort(key=lambda E: E["at"] or "", reverse=True)
    Daily = {D["day"]: D for D in App["daily"]}
    Since = App["daily_since"]
    for E in Identity["events"]:
        Day = E["created_at"][:10]
        if E["kind"] == "sign_in" and Day >= Since:
            Row = Daily.setdefault(Day, {"day": Day})
            Row["sign_ins"] = Row.get("sign_ins", 0) + 1
    return {
        "user": User,
        "totals": App["totals"] | {
            "generations_used": User["used"], "generations_max": User["max"],
            "sign_ins": sum(1 for E in Identity["events"] if E["kind"] == "sign_in"),
        },
        "usage_ledger": sorted(Ledger.values(), key=lambda R: (R["kind"], R["provider"], R["endpoint"])),
        "cost_reporting": "estimated",            # every submission since 2026-10-07 carries its list-price estimate (p3/credits.py)
        "designs": App["designs"],
        "timeline": Timeline[:300],
        "daily": [Daily[K] for K in sorted(Daily)],
        "last_design_activity_at": App["last_design_activity_at"],
        "sessions": Sessions.Summaries(Ctx, OwnerAccountId=AccountId, IncludeMock=False),
        "mock_excluded": True,
    }


DownloadLinkSeconds = 600


def Lineage(Ctx: Context, DesignId: str) -> dict:
    """Where a design came from (its source design and the option that was refined) and every design
    refined from it — so a master's session leads to its variations and a variation back to its master."""
    Db = Ctx.Db
    D = Db.One("SELECT source_design_id, source_candidate_id FROM designs WHERE id = ?", (DesignId,))
    Source = None
    if D and D["source_design_id"]:
        S = Db.One("SELECT id, title, ring_no, charm_no, product_type, owner_account_id FROM designs WHERE id = ?", (D["source_design_id"],))
        if S:
            Source = {"design_id": S["id"], "title": S["title"], "ring_id": RingIds.Ref(S),
                      "option_ring_id": RingIds.CandidateRef(Db, D["source_candidate_id"]) if D["source_candidate_id"] else None}
    Owner = Db.One("SELECT owner_account_id FROM designs WHERE id = ?", (DesignId,))
    Variations = []
    for V in Db.All("SELECT d.id, d.title, d.ring_no, d.charm_no, d.product_type, d.owner_account_id, d.created_at, "
                    "d.source_candidate_id FROM designs d "
                    "WHERE d.source_design_id = ? ORDER BY d.created_at", (DesignId,)):
        Use = None if Owner and V["owner_account_id"] == Owner["owner_account_id"] else Db.One(
            "SELECT id FROM gallery_uses WHERE design_id = ? AND owner_account_id = ?", (DesignId, V["owner_account_id"]))
        Variations.append({"design_id": V["id"], "title": V["title"], "ring_id": RingIds.Ref(V), "created_at": V["created_at"],
                           "own": bool(Owner and V["owner_account_id"] == Owner["owner_account_id"]),
                           "from_option": RingIds.CandidateRef(Db, V["source_candidate_id"]) if V["source_candidate_id"] else None,
                           "customer_session_id": Use["id"] if Use else None})
    return {"source": Source, "variations": Variations}


def _ForkInstruction(Ctx: Context, DesignId: str) -> str:
    """The refinement instruction a variation was born from (its first batch) — it names the variation."""
    B = Ctx.Db.One("SELECT user_text FROM batches WHERE design_id = ? ORDER BY created_at, id LIMIT 1", (DesignId,))
    return B["user_text"] if B else ""


def _Pct(Part: int, Whole: int) -> float | None:
    return round(100.0 * Part / Whole, 1) if Whole else None


Severity = {"error": 0, "warn": 1, "info": 2}


def Attention(Ctx: Context, Orders=None) -> dict:
    """What needs a human now: 3D to review or failed, failed generations, new orders, payment and
    address problems, open quote requests. Live sessions and real orders only (no mock)."""
    Items = []
    for X in Sessions.Summaries(Ctx, IncludeMock=False):
        Href = f"#/sessions/{X['session_id']}"
        Base = {"ref": X["ring_id"], "title": X["title"], "href": Href, "at": X["last_activity_at"] or X["started_at"],
                "customer": X["customer_name"] or X["customer_email"]}
        if X["three_d_state"] == "review_required":
            Items.append({**Base, "kind": "3d_review", "severity": "warn", "label": "3D needs production review"})
        elif X["three_d_state"] == "failed":
            Items.append({**Base, "kind": "3d_failed", "severity": "error", "label": "3D processing failed"})
        if "Generation failed" in X["path"]:
            Items.append({**Base, "kind": "generation_failed", "severity": "error", "label": "Design generation failed"})
    Mock = Sessions.MockDesignIds(Ctx)
    for L in Merge.LegacyCopies(Ctx):
        if L["design_id"] in Mock:
            continue
        Items.append({"kind": "legacy_copy", "severity": "info", "ref": L["ring_id"], "title": L["title"],
                      "href": f"#/sessions/{L['design_id']}", "at": L["created_at"], "customer": None,
                      "label": f"Copy of {L['master_title']} ({L['master_ring_id']}) from before shared designs — the same ring twice. Merge it."})
    if Orders is not None:
        NowIso = Now()
        for O in Orders.AdminList():
            if O["mock"] or O["status"] in ("cancelled", "completed"):
                continue
            Base = {"ref": O["ref"], "title": O["lines"][0]["title"] if O["lines"] else "", "href": f"#/orders/{O['id']}",
                    "at": O["updated_at"], "customer": (O["customer"]["first_name"] + " " + O["customer"]["last_name"]).strip()}
            if O["status"] == "new":
                Items.append({**Base, "kind": "order_new", "severity": "info", "label": "New order — review the design for production"})
            if O["payment_status"] == "failed":
                Items.append({**Base, "kind": "payment_failed", "severity": "error", "label": "Payment failed"})
            elif O["payment_status"] == "pending" and O["created_at"] < _DaysAgo(3, NowIso):
                Items.append({**Base, "kind": "payment_pending", "severity": "warn", "label": "Payment still pending after 3 days"})
            if O["address_validation"] == "failed":
                Items.append({**Base, "kind": "address_failed", "severity": "warn", "label": "Shipping address failed validation"})
            if O["status"] == "payment_confirmed" and O["three_d_state"] in (None, "failed"):
                Items.append({**Base, "kind": "order_needs_3d", "severity": "info", "label": "Paid order without a 3D model yet"})
        for Q in Orders.AdminQuoteRequests("new"):
            Items.append({"kind": "quote_request", "severity": "info", "label": "Quote request awaiting a reply", "ref": Q["ref"],
                          "title": Q["title"], "href": f"#/orders/quote/{Q['id']}", "at": Q["created_at"],
                          "customer": (Q["customer"]["first_name"] + " " + Q["customer"]["last_name"]).strip()})
    Items.sort(key=lambda I: (Severity.get(I["severity"], 9), -(_Ts(I["at"]))))
    ByKind: dict[str, int] = {}
    for I in Items:
        ByKind[I["kind"]] = ByKind.get(I["kind"], 0) + 1
    return {"count": len(Items), "items": Items, "by_kind": ByKind,
            "sessions": sorted({I["href"].split("/")[-1] for I in Items if I["href"].startswith("#/sessions/")})}


def _Ts(Iso: str | None) -> float:
    try:
        return datetime.fromisoformat(Iso).timestamp() if Iso else 0.0
    except ValueError:
        return 0.0


def _DaysAgo(Days: int, NowIso: str) -> str:
    return (datetime.fromisoformat(NowIso) - timedelta(days=Days)).isoformat(timespec="milliseconds")


def Dashboard(Ctx: Context, Orders=None, Days: int | None = None) -> dict:
    """Small, factual overview. Sessions, clicks and orders respect the time range (Days back from
    now; None = all time); 3D geometry statistics are about the models and stay all-time."""
    Since = _DaysAgo(Days, Now()) if Days else None
    S = Sessions.Summaries(Ctx, IncludeMock=False)                          # mock activity stays out of statistics
    if Since:
        S = [X for X in S if (X["started_at"] or "") >= Since]
    N = len(S)
    LiveDesigns = {X["design_id"] for X in Sessions.Summaries(Ctx, IncludeMock=False)}

    def Funnel(Rows):
        Total = len(Rows)
        return [{"stage": K, "label": Sessions.StageLabels[K], "sessions": sum(1 for X in Rows if X["stage_times"].get(K)),
                 "pct": _Pct(sum(1 for X in Rows if X["stage_times"].get(K)), Total)} for K in Sessions.StageOrder]

    Clicks = Ctx.Db.One("SELECT COUNT(*) AS n FROM session_events WHERE kind = 'new_design_clicked' "
                        "AND json_extract(data_json, '$.ai_mode') = 'live'" + (" AND created_at >= ?" if Since else ""),
                        (Since,) if Since else ())["n"]
    Geo = [G for G in Ctx.Db.All(
        "SELECT g.volume_mm3, p.material_id, p.weight_g, p.fixed_price, p.calculated_price, p.production_cost, s.design_id "
        "FROM geometry_results g JOIN price_calculations p ON p.geometry_id = g.id JOIN session_3d s ON s.id = g.session_3d_id "
        "WHERE g.stage = 'production'") if G["design_id"] in LiveDesigns]
    Selected3D = len({R["design_id"] for R in Ctx.Db.All("SELECT DISTINCT design_id FROM session_3d")} & LiveDesigns)
    Mandatory = ("silver", "vermeil", "stainless_steel")
    ByMat: dict[str, list] = {}
    for G in Geo:
        if G["weight_g"] is not None:
            ByMat.setdefault(G["material_id"], []).append(G["weight_g"])
    Pairs = [(G["fixed_price"], G["calculated_price"]) for G in Geo if G["fixed_price"] and G["calculated_price"]]
    Volumes = [G["volume_mm3"] for G in Geo if G["volume_mm3"]]
    # Every measured raw model (once per Hi3D model), scaled by arithmetic to the reference sizes, so
    # the averages do not depend on which size each request happened to ask for.
    Raws = [json.loads(R["measurement_json"]) for R in Ctx.Db.All(
        "SELECT r.measurement_json, b.design_id FROM raw_geometry r JOIN meshes m ON m.id = r.mesh_id "
        "JOIN candidates c ON c.id = m.candidate_id JOIN batches b ON b.id = c.batch_id WHERE r.status = 'measured'")
        if R["design_id"] in LiveDesigns]
    Measured = Raws
    Raws = [R for R in Measured if R.get("bore_ok") and R.get("inner_diameter") and R.get("closed_heuristic", True)]
    Created = sum(1 for R in Ctx.Db.All("SELECT b.design_id FROM meshes m JOIN candidates c ON c.id = m.candidate_id "
                                        "JOIN batches b ON b.id = c.batch_id WHERE m.status = 'ready'") if R["design_id"] in LiveDesigns)
    Book = Ctx.MaterialPrices
    Mats = list(dict.fromkeys(Mandatory + tuple(Ctx.Catalog.Materials)))

    def MaterialRow(VolumeCc):
        Out = {}
        for Mt in Mats:
            R = Book.Row(Mt)
            W = VolumeCc * R["density_g_cm3"] if VolumeCc and R.get("density_g_cm3") else None
            Out[Mt] = {"weight_g": round(W, 2) if W else None,
                       "price_3d": round(W * R["price_per_g"], 2) if W and R.get("price_per_g") else None,
                       "cost_3d": round(W * R["cost_per_g"], 2) if W and R.get("cost_per_g") else None,
                       "fixed_price": R.get("fixed_price")}
        return Out

    BySize = []
    for Sz in (7, 8, 10, 11):
        Vc = [Scaled(R, UsSizeToInnerDiameterMm(Sz))["volume_mm3"] / 1000.0 for R in Raws]
        V = mean(Vc) if Vc else None
        BySize.append({"size": Sz, "models": len(Vc), "volume_cc": round(V, 3) if V else None,
                       "materials": MaterialRow(V)})
    return {
        "sessions": N, "active_sessions": sum(1 for X in S if X["state"] == "active"),
        "new_design_clicks": Clicks,
        "funnel": Funnel(S),
        # Rings and charms apart (sessions, the funnel, generations, orders); the totals above include both
        "by_product": {P: {"sessions": len(Rows), "active_sessions": sum(1 for X in Rows if X["state"] == "active"),
                           "funnel": Funnel(Rows), "generation_failed": sum(1 for X in Rows if "Generation failed" in X["path"])}
                       for P, Rows in ((P, [X for X in S if X.get("product_type", "ring") == P]) for P in Products.All)},
        "funnel_by_origin": {K: {"sessions": len(V), "funnel": Funnel(V)} for K, V in
                             (("prompt", [X for X in S if X["origin"] != "gallery"]),
                              ("gallery", [X for X in S if X["origin"] == "gallery"]))},
        "avg_refinements": round(mean([X["refinements"] for X in S]), 2) if S else None,
        "sessions_with_refinement_pct": _Pct(sum(1 for X in S if X["refinements"]), N),
        "generation_failed": sum(1 for X in S if "Generation failed" in X["path"]),
        "selected_for_3d": Selected3D, "selected_for_3d_pct": _Pct(Selected3D, N),
        "geometry": {
            "measured": len(Geo), "models": len(Raws),
            # 3D parts behind the by-size table: Hi3D models created, measured, and used in the averages
            "models_created": Created, "models_measured": len(Measured), "models_used": len(Raws),
            "avg_volume_cc": round(mean(Volumes) / 1000.0, 3) if Volumes else None,
            "avg_weight_g_by_material": {K: (round(mean(ByMat[K]), 2) if ByMat.get(K) else None)
                                         for K in dict.fromkeys(Mandatory + tuple(ByMat))},
            "by_size": BySize,
            # What the website's fixed price assumes today: 1 cm³ of metal.
            "fixed_basis": {"volume_cc": 1.0, "materials": MaterialRow(1.0)},
            "fixed_vs_3d_price_variance_pct": round(mean([100.0 * (C - F) / F for F, C in Pairs]), 1) if Pairs else None,
            "price_pairs": len(Pairs),
        },
        "cost_model": "configured" if any(G["production_cost"] is not None for G in Geo) else "not_configured",
        "orders": Orders.Summary(Since=Since) if Orders else None,
        "needs_attention": Attention(Ctx, Orders),
        "range": {"days": Days, "since": Since},
        "mock_excluded": True,
        # Admin activity log: every admin action recorded on a journey (3D requests, new-model overrides …).
        "admin_activity": [
            {"at": E["created_at"], "kind": E["kind"], "design_id": E["design_id"], "ring_id": RingIds.Ref(E),
             "product_type": E["product_type"],
             "title": E["title"], "by": json.loads(E["data_json"] or "{}").get("by"),
             "data": {K: V for K, V in json.loads(E["data_json"] or "{}").items() if K not in ("ai_mode", "by")}}
            for E in Ctx.Db.All("SELECT e.*, d.title, d.ring_no, d.charm_no, d.product_type FROM session_events e "
                                "JOIN designs d ON d.id = e.design_id "
                                "WHERE e.kind LIKE 'admin_%' ORDER BY e.created_at DESC LIMIT 25")],
    }

def UserCost(Ctx: Context, AccountId: str, Prices, AllUsage: list | None = None) -> dict:
    """Live (non-mock) sessions of one user and their estimated AI cost at list prices."""
    if AllUsage is None:
        try:
            AllUsage = Ctx.Accounts.AdminActivity(AccountId)["usage"]
        except AccountNotFound:
            AllUsage = []
    Total, N = 0.0, 0
    MockIds = Sessions.MockDesignIds(Ctx)
    for D in Ctx.Db.All("SELECT id FROM designs WHERE owner_account_id = ? UNION ALL "
                        "SELECT design_id AS id FROM gallery_uses WHERE owner_account_id = ?", (AccountId, AccountId)):
        if D["id"] in MockIds:
            continue
        Total += Sessions.Pipeline(Ctx, D["id"], AllUsage, Prices, Owner=AccountId)["total_cost"]   # this account's requests
        N += 1
    return {"sessions": N, "ai_cost": round(Total, 4)}


def SessionDetail(Ctx: Context, Production, SessionId: str, Prices, Gallery=None) -> dict:
    """One journey: a design for its owner, or (session id = use id) a customer on a shared master design."""
    Use = Ctx.Db.One("SELECT * FROM gallery_uses WHERE id = ?", (SessionId,)) if SessionId.startswith("use_") else None
    DesignId = Use["design_id"] if Use else SessionId
    Summary = (Sessions.Summaries(Ctx, SessionIds=[SessionId]) or [None])[0]
    if Summary is None:
        raise HttpError(404, "session_not_found", "Session not found.")
    Owner = Summary["account_id"]
    MasterOwner = Ctx.Db.One("SELECT owner_account_id FROM designs WHERE id = ?", (DesignId,))["owner_account_id"]
    Design = next((G for G in AccountActivity(Ctx, MasterOwner)["designs"] if G["id"] == DesignId), None)
    Batches = (Design or {}).get("batches", [])
    Refs = RingIds.CandidateRefs(Ctx.Db, [DesignId])
    Selected = (Use["selected_candidate_id"] or Use["source_candidate_id"]) if Use else None
    for B in Batches:
        for C in B["candidates"]:
            C["ring_id"] = Refs.get(C["id"])
            if Use:                                    # the customer's own pick, not XJet's
                C["selected"] = C["id"] == Selected
    Jobs = {C["id"] for B in Batches for C in B["candidates"]}
    Jobs |= {M["id"] for B in Batches for C in B["candidates"] for M in C["movies"]}
    Jobs |= {R["id"] for R in Ctx.Db.All("SELECT m.id FROM meshes m JOIN candidates c ON c.id = m.candidate_id "
                                         "JOIN batches b ON b.id = c.batch_id WHERE b.design_id = ?", (DesignId,))}
    try:
        AllUsage = Ctx.Accounts.AdminActivity(Owner)["usage"]
        User = Ctx.Accounts.AdminGet(Owner)
    except AccountNotFound:
        AllUsage, User = [], None
    Usage = [U for U in AllUsage if U["ref_id"] in Jobs and U["kind"] != "generation"]
    Cost = [U["cost_usd"] for U in Usage if U["cost_usd"] is not None]
    Timeline = Sessions.Timeline(Ctx, DesignId, Owner=Owner, Use=Use)
    Flow = Sessions.Pipeline(Ctx, DesignId, AllUsage, Prices, Owner=Owner)
    def ShownMovie(Cands):
        """An image may have several movies (made under different movie configurations): the Admin's choice for it,
        else its newest ready one."""
        Ready = [(C, M) for C in Cands for M in C["movies"] if M["status"] == "ready"]
        Chosen = next((M for C, M in Ready if M["id"] == C.get("movie_id")), None)
        return Chosen or max((M for _, M in Ready), key=lambda M: M["created_at"], default=None)
    Movie = ShownMovie([C for B in Batches for C in B["candidates"] if C["selected"]]) \
        or ShownMovie([C for B in Batches for C in B["candidates"]])
    Keep = ("material_id", "ring_size", "charm_size", "quantity", "unit_price", "pricing_version", "pricing_status")
    Choices = [{"at": E["at"], "kind": E["kind"], **{K: V for K, V in (E.get("data") or {}).items() if K in Keep}}
               for E in Timeline if E["kind"] in ("customize_opened", "customization_changed")]
    ThreeD = Production.ForDesign(DesignId)
    Last = None                                    # the customer's final selection (changes applied in order)
    for C in Choices:
        Last = {**(Last or {}), **{K: V for K, V in C.items() if V is not None}}
    SelCand = next((C for B in Batches for C in B["candidates"] if C["selected"]), None)
    Mesh = Ctx.Db.One("SELECT m.id, m.candidate_id, m.updated_at FROM meshes m JOIN candidates c ON c.id = m.candidate_id "
                      "JOIN batches b ON b.id = c.batch_id WHERE b.design_id = ? AND m.status = 'ready' "
                      "ORDER BY m.created_at DESC LIMIT 1", (DesignId,))
    RawGeo = Ctx.Db.One("SELECT faces, status, integrity, preview_path FROM raw_geometry WHERE mesh_id = ?", (Mesh["id"],)) if Mesh else None
    # The journey's size/material: the matching result (if any) is what "Prepare / download STL" exports.
    Charm = Summary.get("product_type") == Products.Charm
    if Charm:                                       # a charm: its size in mm (the customer's, else the recommended size)
        Size = Summary["charm_size"] if Summary.get("charm_size_chosen") else Ctx.Products.CharmDefaultSize
    else:
        Size = Summary["ring_size"] if Summary["ring_size_chosen"] else 10.0
    Material = (Summary["material_id"] if Summary["material_chosen"] else None) or Ctx.Catalog.DefaultMaterialId
    Matching = next((T for T in ThreeD if Mesh and T["mesh_id"] == Mesh["id"] and T["production_size"] == Size
                     and T["material_id"] == Material and T["status"] in ("measured", "needs_review")), None) if Mesh else None
    LatestOnMesh = next((T for T in ThreeD if Mesh and T["mesh_id"] == Mesh["id"] and T["status"] in ("measured", "needs_review")), None)
    ExistingModel = {"mesh_id": Mesh["id"], "candidate_id": Mesh["candidate_id"], "ring_id": Refs.get(Mesh["candidate_id"]),
                     "ready_at": Mesh["updated_at"], "faces": RawGeo["faces"] if RawGeo else None,
                     "measured": bool(RawGeo and RawGeo["status"] == "measured"),
                     "preview_ready": bool(RawGeo and RawGeo["preview_path"]),
                     # Production readiness of the model itself (any result on it flagged → review required)
                     "production_state": (LatestOnMesh or {}).get("production_state"),
                     "review": (LatestOnMesh or {}).get("review") or [],
                     "latest_3d_id": LatestOnMesh["id"] if LatestOnMesh else None,
                     # The result for this journey's size and material, ready for a scaled STL (None = recalculate first)
                     "journey_3d_id": Matching["id"] if Matching else None,
                     "journey_size": Size, "journey_material_id": Material} if Mesh else None
    return {
        "session": Summary,
        "user": User,
        "pipeline": Flow,
        "cost": {"session": Flow["total_cost"],
                 "basis": "Estimated at list prices (Admin → AI Prompts & Params → AI prices); mock requests are $0."
                          + (" Shared gallery design: only this customer's own requests are counted." if Use else ""),
                 "price_list_version": Prices.Current()["version"]},
        "artifacts": {"image_url": Summary["thumbnail_url"], "movie_url": Movie["url"] if Movie else None,
                      "ring_id": Refs.get(SelCand["id"]) if SelCand else None},
        "last_choice": Last,
        "timeline": Timeline,
        "design": {**Design, "shared": bool(Use), "master_ring_id": Summary["ring_id"]} if Design else None,
        # A legacy gallery copy (same images as a master, no refinement of its own): where it can be merged
        "merge": None if Use else Merge.MergeTarget(Ctx, Ctx.Db.One("SELECT * FROM designs WHERE id = ?", (DesignId,))),
        # Lineage: the design this one was refined from, and the designs refined from this one (variations)
        "lineage": Lineage(Ctx, DesignId),
        "choices": Choices,
        "ai_usage": {"requests": len(Usage),
                     "by_kind": {K: sum(1 for U in Usage if U["kind"] == K) for K in sorted({U["kind"] for U in Usage})},
                     "providers": sorted({U["provider"] or "unknown" for U in Usage}),
                     "cost_usd": round(sum(Cost), 4) if Cost else None},
        "three_d": ThreeD,
        "three_d_defaults": {
            "customer_size": (Summary["charm_size"] if Summary.get("charm_size_chosen") else None) if Charm
            else (Summary["ring_size"] if Summary["ring_size_chosen"] else None),
            "production_size": Size,
            "material_id": (Summary["material_id"] if Summary["material_chosen"] else None) or Ctx.Catalog.DefaultMaterialId,
            "customer_material": Summary["material_id"] if Summary["material_chosen"] else None,
            "has_raw_mesh": any(T["hi3d"] and T["hi3d"]["status"] == "ready" for T in ThreeD),
            # The valid Hi3D model this design already has (reused for every size, material and journey).
            "existing_model": ExistingModel,
            # A variation of a gallery master: the master's model exists → a new model needs the typed confirmation
            "source_model": Production.SourceModel(Ctx.Db.One("SELECT * FROM designs WHERE id = ?", (DesignId,))) if not Mesh else None,
            "options": [{"id": C["id"], "ring_id": C["ring_id"], "selected": C["selected"]}
                        for B in Batches for C in B["candidates"] if C["status"] == "ready"],
        },
        "catalog": {"ring_sizes": list(Ctx.Catalog.RingSizes),
                    "materials": [{"id": M.Id, "label": M.Label, "density_g_cm3": M.DensityGCm3}
                                  for M in Ctx.Catalog.Materials.values()]} if not Charm else
                   {"charm_sizes": Ctx.Products.CharmSizes, "charm_default_size": Ctx.Products.CharmDefaultSize,
                    "charm_size_options": Ctx.Products.CharmSizeOptions(), "size_definition": Products.CharmSizeDefinition,
                    "materials": [{"id": M.Id, "label": CharmPrices.Label(Ctx.Catalog, M.Id), "density_g_cm3": M.DensityGCm3}
                                  for M in CharmPrices.Offered(Ctx.Catalog)]},
        # Shared gallery design: every customer journey on it (the master's own page and each use show it).
        "gallery": Gallery.ForDesign(DesignId) if Gallery else None,
        "gallery_usage": Gallery.Usage(DesignId) if Gallery else [],
    }

def RegisterAdmin(App_: FastAPI, Ctx: Context, Page, Production, Prices, Gallery=None, Orders=None, Promos=None,
                  HealthDetails=None, Quotes=None) -> None:
    """Add the admin page and API to the (inner) app. `Page(name)` renders a web/ page; `HealthDetails()` is the
    server's full health picture (admin-only; the public /api/health says only that the service is up)."""

    def Admin(Authorization: str | None) -> AdminPrincipal:
        return RequireAdmin(Ctx, Authorization)

    AdminAuth.Install(Ctx)
    AdminAuth.InstallMiddleware(App_, Ctx)       # a remembered browser's session cookie → the Bearer key

    @App_.get("/admin", include_in_schema=False)
    @App_.get("/admin/", include_in_schema=False)
    async def AdminPage():
        return HTMLResponse(Page("admin.html"), headers={"X-Robots-Tag": "noindex, nofollow"})

    @App_.get("/api/admin/session")
    async def AdminSession(authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return {"ok": True, "admin": Who.Id, "method": Who.Method, "mode": Ctx.Provider.Name}

    @App_.get("/api/admin/health")
    async def AdminHealth(authorization: str | None = Header(None)):
        """The server's full health picture (provider, mode, pricing posture, configuration versions) — admin only."""
        Admin(authorization)
        return HealthDetails() if HealthDetails else {"ok": True}

    # ── persistent sign-in: the key once per browser, then a server-side session cookie ──
    @App_.post("/api/admin/login")
    async def AdminLogin(Req: Request, Body_: dict = Body(...)):
        if not Ctx.Settings.AdminKey:
            raise HttpError(503, "developer_tools_disabled", "Admin is disabled (P3_ADMIN_KEY is not set).")
        Token = AdminAuth.Login(Ctx, str(Body_.get("key") or ""), Req.headers.get("user-agent"))
        if Token is None:
            raise HttpError(403, "developer_auth_required", "The admin key was not accepted.")
        Resp = JSONResponse({"ok": True, "mode": Ctx.Provider.Name, "remembered": True, "days": AdminAuth.SessionDays})
        AdminAuth.SetCookie(Resp, Req, Ctx.Settings.BasePath, Token)
        return Resp

    @App_.post("/api/admin/logout")
    async def AdminLogout(Req: Request):
        for T in AdminAuth.Tokens(Req):                                 # this browser's session only (every value it sent)
            AdminAuth.Revoke(Ctx, T)
        Resp = JSONResponse({"ok": True})
        AdminAuth.SetCookie(Resp, Req, Ctx.Settings.BasePath, None)
        return Resp

    @App_.post("/api/admin/logout-everywhere")
    async def AdminLogoutEverywhere(Req: Request, authorization: str | None = Header(None)):
        Admin(authorization)
        N = AdminAuth.RevokeAll(Ctx)                                      # server-side revocation of every browser
        Resp = JSONResponse({"ok": True, "revoked": N})
        AdminAuth.SetCookie(Resp, Req, Ctx.Settings.BasePath, None)
        return Resp

    @App_.get("/api/admin/users")
    async def ListUsers(include_removed: bool = False, authorization: str | None = Header(None)):
        Admin(authorization)
        Users = Ctx.Accounts.AdminList(IncludeRemoved=include_removed)
        return {"users": [{**U, **UserCost(Ctx, U["account_id"], Prices)} for U in Users],
                "cost_basis": "Live sessions only, estimated at list prices; mock requests are $0."}

    @App_.post("/api/admin/users")
    async def CreateUser(Body_: dict = Body(...), authorization: str | None = Header(None)):
        Admin(authorization)
        return _Errors(lambda: Ctx.Accounts.AdminCreate(Body_.get("Name", ""), Body_.get("Email", ""),
                                                        Body_.get("MaxGenerations", 10)))

    @App_.get("/api/admin/users/{AccountId}")
    async def GetUser(AccountId: str, authorization: str | None = Header(None)):
        Admin(authorization)
        return _Detail(Ctx, AccountId)

    @App_.patch("/api/admin/users/{AccountId}")
    async def EditUser(AccountId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Admin(authorization)
        return _Errors(lambda: Ctx.Accounts.AdminUpdate(AccountId, Body_.get("Name", ""), Body_.get("Email", ""),
                                                        Body_.get("MaxGenerations"), bool(Body_.get("ResetUsage"))))

    @App_.post("/api/admin/users/{AccountId}/{Action}")
    async def UserAction(AccountId: str, Action: str, authorization: str | None = Header(None)):
        Admin(authorization)
        Actions = {
            "deactivate": lambda: Ctx.Accounts.AdminSetActive(AccountId, False),
            "activate": lambda: Ctx.Accounts.AdminSetActive(AccountId, True),
            "remove": lambda: Ctx.Accounts.AdminRemove(AccountId),
            "restore": lambda: Ctx.Accounts.AdminRestore(AccountId),
        }
        if Action not in Actions:
            raise HttpError(404, "unknown_action", "Unknown action.")
        return _Errors(Actions[Action])

    # ── sessions / dashboard / 3D ─────────────────────────────────────────
    @App_.get("/api/admin/dashboard")
    async def AdminDashboard(days: int | None = None, authorization: str | None = Header(None)):
        Admin(authorization)
        return Dashboard(Ctx, Orders, days if days and days > 0 else None)

    @App_.get("/api/admin/attention")
    async def AdminAttention(authorization: str | None = Header(None)):
        Admin(authorization)
        return Attention(Ctx, Orders)

    # ── a design's unique name (gallery masters should never share one) ──
    @App_.patch("/api/admin/designs/{DesignId}")
    async def AdminRenameDesign(DesignId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        D = Ctx.Db.One("SELECT id, title, prompt, owner_account_id, product_type FROM designs WHERE id = ?", (DesignId,))
        if D is None:
            raise HttpError(404, "design_not_found", "Design not found.")
        Product = D["product_type"] or "ring"
        Title = " ".join(str(Body_.get("title") or "").split())
        if not 2 <= len(Title) <= 60:
            raise HttpError(400, "invalid_title", "The name must be 2–60 characters.")
        Dup = Ctx.Db.One("SELECT d.id, d.ring_no, d.charm_no, d.product_type FROM gallery_items g JOIN designs d ON d.id = g.design_id "
                         "WHERE lower(d.title) = lower(?) AND d.id != ?", (Title, DesignId))
        if Dup and not Body_.get("force"):
            raise HttpError(409, "duplicate_title", f"Another gallery design is already called “{Title}” ({RingIds.Ref(Dup)}). "
                                                    "Give each master design a distinctive name, or confirm to use it anyway.")
        Ctx.Db.Execute("UPDATE designs SET title = ?, updated_at = ? WHERE id = ?", (Title, Now(), DesignId))
        Sessions.Record(Ctx, D["owner_account_id"], "admin_design_renamed", DesignId, from_title=D["title"], to_title=Title, by=Who.Id)
        # Variations that share the old family word follow the master: "Fil Lattice" under a master renamed
        # "Aurora Twist" becomes "Aurora Lattice" (a variation renamed by hand keeps its own name).
        Followed = []
        OldFamily, NewFamily = Naming.FamilyOf(D["title"], Product).lower(), Naming.FamilyOf(Title, Product).lower()
        if Body_.get("cascade", True) and OldFamily and NewFamily and OldFamily != NewFamily:
            for V in Ctx.Db.All("SELECT id, title, owner_account_id FROM designs WHERE source_design_id = ?", (DesignId,)):
                if Naming.FamilyOf(V["title"], Product).lower() != OldFamily:
                    continue
                New = Naming.FollowRename(Ctx.Db, V["id"], V["title"], Title, _ForkInstruction(Ctx, V["id"]), D["prompt"], Product)
                if New == V["title"]:
                    continue
                Ctx.Db.Execute("UPDATE designs SET title = ?, updated_at = ? WHERE id = ?", (New, Now(), V["id"]))
                Sessions.Record(Ctx, V["owner_account_id"], "admin_design_renamed", V["id"], from_title=V["title"], to_title=New,
                                follows=DesignId, by=Who.Id)
                Followed.append({"id": V["id"], "title": New, "was": V["title"]})
        return {"id": DesignId, "title": Title, "was": D["title"], "variations": Followed}

    @App_.get("/api/admin/designs/{DesignId}/names")
    async def AdminNameSuggestions(DesignId: str, authorization: str | None = Header(None)):
        """Free names for the Rename form: local rules on the design's own prompt, no AI call of any kind."""
        Admin(authorization)
        D = Ctx.Db.One("SELECT id, title, prompt, source_design_id, product_type FROM designs WHERE id = ?", (DesignId,))
        if D is None:
            raise HttpError(404, "design_not_found", "Design not found.")
        Product = D["product_type"] or "ring"              # a charm gets charm names (no "Pendant", "Chain" …)
        Taken = Naming.TakenTitles(Ctx.Db)                 # the current name is not a suggestion
        Master = Ctx.Db.One("SELECT title FROM designs WHERE id = ?", (D["source_design_id"],)) if D["source_design_id"] else None
        if Master:
            Names = Naming.Suggestions(D["prompt"], Taken, Lineage=Master["title"], Instruction=_ForkInstruction(Ctx, DesignId), N=8,
                                       Product=Product)
        else:
            Names = Naming.Suggestions(D["prompt"], Taken, N=8, Product=Product)
        return {"id": DesignId, "title": D["title"], "suggestions": Names, "lineage": Master["title"] if Master else None}

    @App_.post("/api/admin/batches/{BatchId}/split")
    async def AdminSplitRefinement(BatchId: str, authorization: str | None = Header(None)):
        """A refinement that landed inside a master becomes a design of its own (p3/merge.py SplitRefinement)."""
        Who = Admin(authorization)
        return Merge.SplitRefinement(Ctx, BatchId, Who.Id)

    @App_.post("/api/admin/designs/{DesignId}/merge")
    async def AdminMergeLegacyCopy(DesignId: str, authorization: str | None = Header(None)):
        """Fold a legacy gallery copy back into its master: one ring, one Ring ID, one 3D model (p3/merge.py)."""
        Who = Admin(authorization)
        return Merge.MergeLegacyCopy(Ctx, DesignId, Who.Id)

    # ── orders (operational), promo codes, quote requests ─────────────────
    @App_.get("/api/admin/orders")
    async def AdminOrders(status: str | None = None, payment: str | None = None, q: str | None = None,
                          product: str | None = None, authorization: str | None = Header(None)):
        Admin(authorization)
        return {"orders": Orders.AdminList(status or None, payment or None, q or None, Product=product or None),
                "statuses": [{"id": S, "label": OrdersModule.StatusLabels[S]} for S in OrdersModule.StatusOrder + ["cancelled"]],
                "payment_statuses": [{"id": S, "label": PaymentsModule.Labels[S]} for S in PaymentsModule.Statuses],
                "quote_requests": Orders.AdminQuoteRequests()}

    @App_.get("/api/admin/orders/{OrderId}")
    async def AdminOrder(OrderId: str, authorization: str | None = Header(None)):
        Admin(authorization)
        return Orders.AdminGet(OrderId)

    @App_.post("/api/admin/orders/{OrderId}/status")
    async def AdminOrderStatus(OrderId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Orders.SetStatus(OrderId, str(Body_.get("status") or ""), Who.Id, str(Body_.get("note") or ""), Force=bool(Body_.get("force")))

    @App_.post("/api/admin/orders/{OrderId}/payment")
    async def AdminOrderPayment(OrderId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Orders.SetPayment(OrderId, str(Body_.get("status") or ""), Who.Id, str(Body_.get("note") or ""), Body_.get("ref"))

    @App_.post("/api/admin/orders/{OrderId}/note")
    async def AdminOrderNote(OrderId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Orders.AddNote(OrderId, str(Body_.get("note") or ""), Who.Id)

    # The STL for an ordered ring: the design's model at the ordered size and material (no Hi3D call)
    @App_.post("/api/admin/orders/{OrderId}/lines/{LineId}/3d")
    async def AdminOrderLine3D(OrderId: str, LineId: str, authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Orders.PrepareLine3D(OrderId, LineId, Who.Id)

    @App_.get("/api/admin/promo-codes")
    async def AdminPromos(authorization: str | None = Header(None)):
        Admin(authorization)
        return {"promo_codes": Promos.List()}

    @App_.post("/api/admin/promo-codes")
    async def AdminPromoCreate(Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Promos.Save(Body_, Who.Id)

    @App_.patch("/api/admin/promo-codes/{PromoId}")
    async def AdminPromoEdit(PromoId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        if set(Body_) == {"active"}:
            return Promos.SetActive(PromoId, bool(Body_["active"]))
        return Promos.Save(Body_, Who.Id, PromoId)

    @App_.post("/api/admin/quote-requests/{RequestId}/status")
    async def AdminQuoteStatus(RequestId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Orders.SetQuoteStatus(RequestId, str(Body_.get("status") or ""), Who.Id)

    # ── the quote workspace (p3/quotes.py): every detail, a 3D-based price, the quote email, the history ──
    @App_.get("/api/admin/quote-requests/{RequestId}")
    async def AdminQuote(RequestId: str, authorization: str | None = Header(None)):
        Admin(authorization)
        return Quotes.AdminDetail(RequestId)

    @App_.post("/api/admin/quote-requests/{RequestId}/offer")
    async def AdminQuoteOffer(RequestId: str, request: Request, Background: BackgroundTasks, Body_: dict = Body(...),
                              authorization: str | None = Header(None)):
        """Send (or revise) the quote: the customer's link points at the public site, never at the Admin host."""
        Who = Admin(authorization)
        Origin = Ctx.Settings.PublicBaseUrl or PublicOrigin(request)
        return Quotes.SendOffer(RequestId, Body_, Who.Id, Origin, Background.add_task)

    @App_.post("/api/admin/quote-requests/{RequestId}/note")
    async def AdminQuoteNote(RequestId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Quotes.AddNote(RequestId, str(Body_.get("note") or ""), Who.Id)

    @App_.get("/api/admin/sessions")
    async def ListSessions(include_mock: bool = False, authorization: str | None = Header(None)):
        Admin(authorization)
        return {"sessions": Sessions.Summaries(Ctx, IncludeMock=include_mock), "idle_minutes": Sessions.IdleMinutes,
                "mock_sessions": len(Sessions.MockDesignIds(Ctx)), "include_mock": include_mock,
                "retired": RingIds.Retired(Ctx.Db)}

    @App_.get("/api/admin/sessions/{DesignId}")
    async def GetSession(DesignId: str, authorization: str | None = Header(None)):
        Admin(authorization)
        D = SessionDetail(Ctx, Production, DesignId, Prices, Gallery)
        for T in D["three_d"]:                          # the Hi3D thumbnail is shown first (an <img>: signed link)
            if (T["live"].get("raw") or {}).get("thumbnail"):
                T["live"]["thumbnail_url"] = _SignedUrl(T["id"], "thumbnail")
        return D

    @App_.post("/api/admin/sessions/{SessionId}/3d")
    async def Generate3D(SessionId: str, Body_: dict = Body(default={}), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        Customer, DesignId = None, SessionId
        if SessionId.startswith("use_"):                 # a customer's journey on a shared master design
            Customer = (Sessions.Summaries(Ctx, SessionIds=[SessionId]) or [None])[0]
            if Customer is None:
                raise HttpError(404, "session_not_found", "Session not found.")
            DesignId = Customer["design_id"]
        return Production.Request(DesignId, Body_.get("production_size"), Body_.get("material_id") or None,
                                  Body_.get("candidate_id") or None, RequestedBy=Who.Id, Customer=Customer,
                                  Override=Body_.get("override"))

    # Large files (a 5M-face STL is ~250 MB) are downloaded natively by the browser — streamed to disk
    # with its progress bar — through a short-lived signed link, instead of being loaded into the page.
    def _DownloadSig(Sid: str, Stage: str, Exp: int) -> str:
        # Its own secret in production (P3_SIGNING_SECRET); the admin key only where none is configured (development)
        Secret = Ctx.Settings.SigningSecret or Ctx.Settings.AdminKey or ""
        return hmac.new(Secret.encode(), f"3d-download:{Sid}:{Stage}:{Exp}".encode(), hashlib.sha256).hexdigest()

    def _Stage(Stage: str) -> str:
        if Stage in ("raw", "production", "preview", "thumbnail") or Stage.startswith("export-"):
            return Stage
        raise HttpError(404, "geometry_not_found", "Unknown file.")

    def _OrderRef(Raw: str | None) -> str | None:
        """Only a real-looking order reference reaches a file name (it is informational, never trusted)."""
        Raw = (Raw or "").strip().upper()
        return Raw if Raw and __import__("re").fullmatch(r"ORD-\d{1,9}", Raw) else None

    def _SignedUrl(Sid: str, Stage: str, OrderRef: str | None = None) -> str:
        Exp = int(time.time()) + DownloadLinkSeconds
        Tail = f"&order={OrderRef}" if _OrderRef(OrderRef) else ""
        return f"{Ctx.Settings.BasePath}/api/admin/3d/{Sid}/stl/{Stage}?exp={Exp}&sig={_DownloadSig(Sid, Stage, Exp)}{Tail}"

    @App_.post("/api/admin/3d/{Sid}/download-link")
    async def Download3DLink(Sid: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Admin(authorization)
        Stage = _Stage(str(Body_.get("stage") or ""))
        Path_ = Production.FilePath(Sid, Stage)                    # 404 if the file does not exist (yet)
        return {"url": _SignedUrl(Sid, Stage), "bytes": Path_.stat().st_size, "expires_in_s": DownloadLinkSeconds}

    @App_.get("/api/admin/3d/{Sid}/stl/{Stage}")
    async def Download3D(Sid: str, Stage: str, exp: int | None = None, sig: str | None = None, order: str | None = None,
                         authorization: str | None = Header(None)):
        Signed = (exp is not None and sig and exp >= time.time() and Ctx.Settings.AdminKey
                  and hmac.compare_digest(sig, _DownloadSig(Sid, Stage, exp)))
        if not Signed:
            Admin(authorization)
        Path_ = Production.FilePath(Sid, _Stage(Stage))
        Types = {".stl": "model/stl", ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
        Name = Production.FileName(Sid, Stage, Path_.suffix, OrderRef=_OrderRef(order))   # ORD-… only when an order exists
        Inline = Stage in ("thumbnail", "preview")
        return FileResponse(Path_, filename=None if Inline else Name,
                            media_type=Types.get(Path_.suffix.lower(), "application/octet-stream"),
                            headers={"Cache-Control": "private, max-age=600"} if Inline else None)

    @App_.get("/api/admin/3d/{Sid}/status")
    async def Status3D(Sid: str, authorization: str | None = Header(None)):
        Admin(authorization)
        S = Production.Status(Sid)
        if S["raw"] and S["raw"]["thumbnail"]:
            S["thumbnail_url"] = _SignedUrl(Sid, "thumbnail")
        return S

    @App_.post("/api/admin/3d/{Sid}/cancel")
    async def Cancel3D(Sid: str, authorization: str | None = Header(None)):
        Admin(authorization)
        return Production.Cancel(Sid)

    @App_.post("/api/admin/3d/{Sid}/retry")
    async def Retry3D(Sid: str, authorization: str | None = Header(None)):
        Admin(authorization)
        return Production.Retry(Sid)

    @App_.post("/api/admin/3d/{Sid}/fix-bore")
    async def FixBore3D(Sid: str, authorization: str | None = Header(None)):
        """A ring's bore that is not round is made round for this result (local geometry work, no Hi3D call)."""
        Who = Admin(authorization)
        return Production.FixBore(Sid, Who.Id)

    @App_.post("/api/admin/3d/{Sid}/accept")
    async def Accept3D(Sid: str, Body_: dict = Body(default={}), authorization: str | None = Header(None)):
        """A flagged result is accepted for production as measured (the reasons stay with it)."""
        Who = Admin(authorization)
        return Production.Accept(Sid, Who.Id, str(Body_.get("note") or ""))

    # Scaled STL: queue export → temporary file → slot released → native signed download → TTL delete.
    @App_.post("/api/admin/3d/{Sid}/export")
    async def Export3D(Sid: str, authorization: str | None = Header(None)):
        Admin(authorization)
        return Production.StartExport(Sid)

    @App_.get("/api/admin/3d/{Sid}/export/{Jid}")
    async def Export3DStatus(Sid: str, Jid: str, order: str | None = None, authorization: str | None = Header(None)):
        Admin(authorization)
        if Jid == "stored":                           # a production STL kept on disk (a v2 row)
            S = Production.StoredProduction(Sid)
            if S is None:
                raise HttpError(404, "job_not_found", "Export not found.")
            S["url"] = _SignedUrl(Sid, "production", _OrderRef(order))
            return S
        S = Production.ExportStatus(Sid, Jid)
        if S["status"] == "done":
            S["url"] = _SignedUrl(Sid, f"export-{Jid}", _OrderRef(order))
        return S

    @App_.get("/api/admin/storage")
    async def Storage(authorization: str | None = Header(None)):
        Admin(authorization)
        Production.Queue.CleanupExports()
        return await asyncio.get_running_loop().run_in_executor(None, Production.Queue.Storage)

    # ── AI prompts & parameters ───────────────────────────────────────────
    def _Model(ModelId: str):
        if ModelId not in ModelSpecs:
            raise HttpError(404, "unknown_model", "Unknown model.")
        return ModelId

    def _Invalid(E: ConfigError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": {
            "code": "invalid_configuration", "message": "The configuration is not valid.", "problems": E.Problems}})

    @App_.get("/api/admin/models")
    async def ListModels(authorization: str | None = Header(None)):
        Admin(authorization)
        return {"models": [{**Ctx.Models.State(M), "history": None} for M in ModelSpecs],
                "runtime_placeholders": RuntimeInputs}

    @App_.get("/api/admin/models/export")
    async def ExportModels(model: str = "all", format: str = "json", product: str = "ring", authorization: str | None = Header(None)):
        Admin(authorization)
        Product = Products.Normalize(product)            # "all" = every model of ONE product (rings by default, as before)
        Ids = [M for M, S in ModelSpecs.items() if S.Product == Product] if model == "all" else [_Model(model)]
        Data = Ctx.Models.Export(Ids)                    # configurations only: no keys or credentials exist here
        Name = f"p3-ai-config-{'all' if model == 'all' else model}" + (f"-{Product}" if model == "all" and Product != "ring" else "")
        if format == "txt":
            return PlainTextResponse(ExportText(Data), headers={"Content-Disposition": f'attachment; filename="{Name}.txt"'})
        return JSONResponse(Data, headers={"Content-Disposition": f'attachment; filename="{Name}.json"'})

    @App_.get("/api/admin/models/{ModelId}")
    async def GetModel(ModelId: str, authorization: str | None = Header(None)):
        Admin(authorization)
        return Ctx.Models.State(_Model(ModelId))

    @App_.post("/api/admin/models/{ModelId}/validate")
    async def ValidateModel(ModelId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Admin(authorization)
        try:
            return {"ok": True, "params": ValidateConfig(_Model(ModelId), Body_.get("params"))}
        except ConfigError as E:
            return {"ok": False, "problems": E.Problems}

    @App_.post("/api/admin/models/{ModelId}/preview")
    async def PreviewModel(ModelId: str, Body_: dict = Body(default={}), authorization: str | None = Header(None)):
        Admin(authorization)
        try:
            return Ctx.Models.Preview(_Model(ModelId), Body_.get("params"))   # never submits to the provider
        except ConfigError as E:
            return _Invalid(E)

    @App_.post("/api/admin/models/{ModelId}/activate")
    async def ActivateModel(ModelId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        try:
            return Ctx.Models.SaveAndActivate(_Model(ModelId), Body_.get("params"), Who.Id, str(Body_.get("note") or "")[:200])
        except ConfigError as E:
            return _Invalid(E)

    @App_.post("/api/admin/models/{ModelId}/restore")
    async def RestoreModel(ModelId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        try:
            return Ctx.Models.Restore(_Model(ModelId), str(Body_.get("version_id") or ""), Who.Id)
        except ConfigError as E:
            return _Invalid(E)

    # ── Products: rings and charms (customer availability per product, charm sizes) ──
    def _ProductsState() -> dict:
        St = Ctx.Products.State()
        return {**St, "charm_configuration_ready": Ctx.Models.Supports(Products.Charm),
                "products": [{"id": P, "label": Products.Labels[P], "plural": Products.Plurals[P],
                              "available": St["availability"][P], "changed": St["availability_changed"][P],
                              "movie": St["movies"][P], "movie_changed": St["movies_changed"][P],
                              "configuration_ready": Ctx.Models.Supports(P)} for P in Products.All]}

    @App_.get("/api/admin/products")
    async def AdminProducts(authorization: str | None = Header(None)):
        Admin(authorization)
        return _ProductsState()

    @App_.put("/api/admin/products/availability")
    async def SetAvailability(Body_: dict = Body(...), authorization: str | None = Header(None)):
        """Customer availability of one product, ON / OFF: {"product": "ring" | "charm", "available": true | false}
        (the Admin confirms the change in a normal dialog). The older {"charms_available": bool} body still works."""
        Who = Admin(authorization)
        if "product" in Body_:
            Product, On = Products.Normalize(Body_.get("product")), Body_.get("available")
        else:
            Product, On = Products.Charm, Body_.get("charms_available")
        if not isinstance(On, bool):
            raise HttpError(400, "invalid_value", "available must be true or false.")
        if On and not Ctx.Models.Supports(Product):
            raise HttpError(409, f"{Product}_configuration_missing", f"The {Products.Labels[Product]} AI configuration is not ready.")
        if On != Ctx.Products.Available(Product):
            Ctx.Products.SetAvailable(Product, On, Who.Id, str(Body_.get("note") or "")[:300])
        return _ProductsState()

    @App_.put("/api/admin/products/movie")
    async def SetProductMovie(Body_: dict = Body(...), authorization: str | None = Header(None)):
        """The 360° movie switch of one product: {"product": "ring" | "charm", "on": true | false}. OFF: no movie is
        made or shown in that product's flow (Customize shows the still image); existing movies are kept and shown
        again once the switch is ON."""
        Who = Admin(authorization)
        Product, On = Products.Normalize(Body_.get("product")), Body_.get("on")
        if not isinstance(On, bool):
            raise HttpError(400, "invalid_value", "on must be true or false.")
        if On != Ctx.Products.MovieOn(Product):
            Ctx.Products.SetMovieOn(Product, On, Who.Id, str(Body_.get("note") or "")[:300])
        return _ProductsState()

    @App_.put("/api/admin/products/charm-sizes")
    async def SetCharmSizes(Body_: dict = Body(...), authorization: str | None = Header(None)):
        """The charm sizes on offer, in mm, as products.CharmSizeDefinition says (for now the charm's total height,
        the loop included), with their names ({"14": "Classic"}) and the recommended size (the one Customize
        suggests and a 3D is made in by default). Prices are set per size (Pricing & Materials → Charm); a size
        without a price is "Price unavailable"."""
        Who = Admin(authorization)
        Note = str(Body_.get("note") or "")[:300]
        New, Old = Products.ValidateCharmSizes(Body_.get("sizes")), Ctx.Products.CharmSizes
        Names = Products.ValidateCharmSizeNames(Body_.get("names", Ctx.Products.CharmSizeNames), New)
        Default = Products.ValidateCharmDefaultSize(Body_.get("default"), New) or Products.CharmDefaultSize(New, Ctx.Products.CharmDefaultSize)
        if New != Old:
            Ctx.Products.Set("charm_sizes", New, Who.Id, Note)
        if Names != Ctx.Products.CharmSizeNames:
            Ctx.Products.Set("charm_size_names", Names, Who.Id, Note)
        if Default != Ctx.Products.Get("charm_default_size"):       # stored explicitly (also a kept or fallen-back one)
            Ctx.Products.Set("charm_default_size", Default, Who.Id, Note)
        InBags = [{"size": S, "bag_lines": Ctx.Db.One("SELECT COUNT(*) AS n FROM bag_lines WHERE product_type = 'charm' AND charm_size = ?",
                                                     (S,))["n"]} for S in sorted(set(Old) - set(New))]
        return {**_ProductsState(), "removed_in_bags": [X for X in InBags if X["bag_lines"]]}

    # ── Credits: what a customer's credit buys (the tariff) and the day's AI spend against the cap ──
    @App_.get("/api/admin/credits")
    async def AdminCredits(authorization: str | None = Header(None)):
        Admin(authorization)
        return Credits.Status(Ctx)

    @App_.put("/api/admin/credits/tariff")
    async def SetTariff(Body_: dict = Body(...), authorization: str | None = Header(None)):
        """{"tariff": {"image": 1, "refinement_image": 1, "movie": 1, "mesh": 0}} — credits per piece of work."""
        Who = Admin(authorization)
        New = Credits.ValidateTariff(Body_.get("tariff"))
        if New != Credits.Tariff(Ctx):
            Ctx.Products.Set(Credits.TariffKey, New, Who.Id, str(Body_.get("note") or "")[:300])
        return Credits.Status(Ctx)

    # ── Charm pricing: its own versioned table (a fixed price per material and size; price and cost per gram) ──
    @App_.get("/api/admin/charm-prices")
    async def GetCharmPrices(authorization: str | None = Header(None)):
        Admin(authorization)
        return Ctx.CharmPrices.AdminTable()

    @App_.put("/api/admin/charm-prices")
    async def SaveCharmPrices(Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        try:
            Ctx.CharmPrices.Save({"materials": Body_.get("materials") or {}}, Who.Id, str(Body_.get("note") or "Edited in Admin")[:200])
        except CharmPriceError as E:
            raise HttpError(400, "invalid_charm_prices", str(E)) from E
        return Ctx.CharmPrices.AdminTable()

    # ── Inspiration Gallery: curated XJet designs shown on the customer site ──────
    @App_.get("/api/admin/gallery")
    async def AdminGallery(authorization: str | None = Header(None)):
        Admin(authorization)
        return {"items": Gallery.AdminList()}

    # ── the movie shown for an image: it may have several, made under different movie configurations ──
    @App_.post("/api/admin/candidates/{CandidateId}/movie")
    async def ChooseMovie(CandidateId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        from p3.movies import MovieService
        Who = Admin(authorization)
        Cand = Ctx.Db.One("SELECT c.id, b.design_id, d.owner_account_id FROM candidates c JOIN batches b ON b.id = c.batch_id "
                          "JOIN designs d ON d.id = b.design_id WHERE c.id = ?", (CandidateId,))
        if Cand is None:
            raise HttpError(404, "candidate_not_found", "Image not found.")
        M = MovieService(Ctx).Choose(CandidateId, str(Body_.get("movie_id") or ""))
        Sessions.Record(Ctx, Cand["owner_account_id"], "admin_movie_chosen", Cand["design_id"], candidate_id=CandidateId,
                        movie_id=M["id"], by=Who.Id)
        return M

    # ── "Make a new movie": a new paid movie for an image with the movie configuration active now ──
    @App_.post("/api/admin/candidates/{CandidateId}/movies")
    async def NewMovie(CandidateId: str, authorization: str | None = Header(None)):
        from p3.movies import MovieService
        Who = Admin(authorization)
        Cand = Ctx.Db.One("SELECT c.id, b.design_id, d.owner_account_id FROM candidates c JOIN batches b ON b.id = c.batch_id "
                          "JOIN designs d ON d.id = b.design_id WHERE c.id = ?", (CandidateId,))
        if Cand is None:
            raise HttpError(404, "candidate_not_found", "Image not found.")
        M = MovieService(Ctx).Remake(CandidateId, Who.Id)
        Sessions.Record(Ctx, Cand["owner_account_id"], "admin_movie_requested", Cand["design_id"], candidate_id=CandidateId,
                        movie_id=M["id"], config_version=M["config_version"], by=Who.Id)
        return M

    @App_.get("/api/admin/gallery/usage/{DesignId}")
    async def AdminGalleryUsage(DesignId: str, authorization: str | None = Header(None)):
        Admin(authorization)
        return {"design_id": DesignId, "uses": Gallery.Usage(DesignId)}

    @App_.post("/api/admin/gallery")
    async def AdminGalleryAdd(Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        return Gallery.Add(str(Body_.get("design_id") or ""), Body_.get("candidate_id") or None, Who.Id,
                           str(Body_.get("owner_kind") or "xjet"), str(Body_.get("consent_note") or ""))

    @App_.delete("/api/admin/gallery/{ItemId}")
    async def AdminGalleryRemove(ItemId: str, authorization: str | None = Header(None)):
        Admin(authorization)
        Gallery.Remove(ItemId)
        return {"items": Gallery.AdminList()}

    @App_.post("/api/admin/gallery/{ItemId}/move")
    async def AdminGalleryMove(ItemId: str, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Admin(authorization)
        Gallery.Move(ItemId, str(Body_.get("direction") or "up"))
        return {"items": Gallery.AdminList()}

    # ── Gallery sync: another site's approved gallery pushed here (p3/gallerysync.py; gallery content only) ─────
    from p3 import gallerysync as GallerySync
    GallerySync.RegisterRoutes(App_, Ctx, Admin)

    # ── Material pricing (density, price $/g, cost $/g, website fixed price) ─────
    def _MaterialTable() -> dict:
        Doc = Ctx.MaterialPrices.Current()
        return {**Doc, "history": Ctx.MaterialPrices.History(),
                "rows": [{"id": M.Id, "label": M.Label, "group": M.Group, **Ctx.MaterialPrices.Row(M.Id)}
                         for M in Ctx.Catalog.Materials.values()]}

    @App_.get("/api/admin/material-prices")
    async def GetMaterialPrices(authorization: str | None = Header(None)):
        Admin(authorization)
        return _MaterialTable()

    @App_.put("/api/admin/material-prices")
    async def SaveMaterialPrices(Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        try:
            Ctx.MaterialPrices.Save({"materials": Body_.get("materials") or {}}, Who.Id,
                                    str(Body_.get("note") or "Edited in Admin")[:200])
        except MaterialPriceError as E:
            raise HttpError(400, "invalid_material_prices", str(E)) from E
        return _MaterialTable()

    # ── AI mode (Settings → System): mock (no provider, no cost) or live (fal.ai, billed) ───────────────
    @App_.get("/api/admin/ai-mode")
    async def GetAiMode(authorization: str | None = Header(None)):
        Admin(authorization)
        return App_.state.Modes.Status()

    @App_.put("/api/admin/ai-mode")
    async def SetAiMode(Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        Modes = App_.state.Modes
        Before = Modes.Mode
        State = Modes.Switch(Body_.get("mode"), Body_.get("confirmation"))   # refuses: locked, no key, no confirmation, jobs running
        if State["mode"] != Before:
            Logger.warning("AI mode switched to %s in the Admin by %s", State["mode"].upper(), Who.Id)
        return State

    # ── Admin key (Settings → Keys): changing it needs the current key again and signs every other browser out ──
    def _AdminKeyState() -> dict:
        S = Ctx.Settings
        return {"source": "admin" if S.AdminKeyFromAdmin else "environment", "length": len(S.AdminKey or ""),
                "editable": not S.Production, "min_chars": AdminKeys.MinChars,
                "note": ("In production the Admin key is part of the server configuration (P3_ADMIN_KEY in the environment "
                         "file: 24+ random characters); it cannot be changed here." if S.Production else "")}

    def _NewAdminKey(Req: Request, Key: str | None, FromAdmin: bool, Who) -> JSONResponse:
        Ctx.Settings.AdminKey, Ctx.Settings.AdminKeyFromAdmin = Key, FromAdmin
        N = AdminAuth.RevokeAll(Ctx)                                    # every remembered browser, this one included...
        Token = AdminAuth.NewSession(Ctx, Req.headers.get("user-agent"))    # ...which stays signed in with a new session
        Resp = JSONResponse({**_AdminKeyState(), "signed_out_browsers": N})
        AdminAuth.SetCookie(Resp, Req, Ctx.Settings.BasePath, Token)
        return Resp

    @App_.get("/api/admin/admin-key")
    async def GetAdminKey(authorization: str | None = Header(None)):
        Admin(authorization)
        return _AdminKeyState()

    @App_.put("/api/admin/admin-key")
    async def SetAdminKey(Req: Request, Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        if Ctx.Settings.Production:
            raise HttpError(409, "admin_key_locked", "In production the Admin key is set in the server configuration.")
        Supplied = str(Body_.get("current") or "").strip()
        if not Supplied or not hmac.compare_digest(Supplied.encode(), (Ctx.Settings.AdminKey or "").encode()):
            raise HttpError(400, "current_key_wrong", "The current Admin key is not right.")
        Key = AdminKeys.Normalize(Body_.get("new"), Ctx.Settings.AdminKey)
        AdminKeys.WriteSaved(Ctx.Settings.DataDir, Key)
        Logger.warning("Admin key changed in the Admin by %s; every browser was signed out", Who.Id)
        return _NewAdminKey(Req, Key, True, Who)

    @App_.delete("/api/admin/admin-key")
    async def RemoveAdminKey(Req: Request, Body_: dict = Body(default={}), authorization: str | None = Header(None)):
        """Go back to P3_ADMIN_KEY from the environment (needs the current key again)."""
        Who = Admin(authorization)
        if Ctx.Settings.Production:
            raise HttpError(409, "admin_key_locked", "In production the Admin key is set in the server configuration.")
        Supplied = str(Body_.get("current") or "").strip()
        if not Supplied or not hmac.compare_digest(Supplied.encode(), (Ctx.Settings.AdminKey or "").encode()):
            raise HttpError(400, "current_key_wrong", "The current Admin key is not right.")
        Env = os.environ.get("P3_ADMIN_KEY") or None
        if not Env:
            raise HttpError(409, "no_environment_key", "The server configuration has no P3_ADMIN_KEY to go back to.")
        AdminKeys.RemoveSaved(Ctx.Settings.DataDir)
        Logger.warning("Admin key saved in the Admin removed by %s; every browser was signed out", Who.Id)
        return _NewAdminKey(Req, Env, False, Who)

    # ── fal.ai API key (Settings → Keys): shown masked, never returned ───────────────────────────────
    def _FalKeyState() -> dict:
        S = Ctx.Settings
        Source = "none" if not S.FalKey else ("admin" if S.FalKeyFromAdmin else "environment")
        return {"configured": bool(S.FalKey), "source": Source, "last4": FalKeys.Last4(S.FalKey),
                "editable": not S.Production,
                "note": ("In production the key is part of the server configuration (FAL_KEY in the environment file); "
                         "it cannot be changed here." if S.Production else "")}

    def _ApplyFalKey(Key: str | None, Admin_: bool, Who) -> dict:
        """Use `Key` from now on; a live provider is rebuilt with it, but never while generations are running."""
        Modes = App_.state.Modes
        Live = Modes.Mode == "live"
        if Live and Modes.ActiveJobs():
            raise HttpError(409, "jobs_running", "Wait until the running generations finish before changing the key.")
        if Live and not Key:
            raise HttpError(409, "live_would_lose_key", "The AI mode is live: switch to mock before removing the only key.")
        Ctx.Settings.FalKey, Ctx.Settings.FalKeyFromAdmin = Key, Admin_
        if Live:
            Ctx.Provider = Modes.Factories["live"]()
        return _FalKeyState()

    @App_.get("/api/admin/fal-key")
    async def GetFalKey(authorization: str | None = Header(None)):
        Admin(authorization)
        return _FalKeyState()

    @App_.put("/api/admin/fal-key")
    async def SetFalKey(Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        if Ctx.Settings.Production:
            raise HttpError(409, "fal_key_locked", "In production the fal.ai key is set in the server configuration.")
        Key = FalKeys.Normalize(Body_.get("key"))
        await FalKeys.CheckKey(Key)                                    # free; nothing is saved when it fails
        State = _ApplyFalKey(Key, True, Who)
        FalKeys.WriteSaved(Ctx.Settings.DataDir, Key)
        Logger.warning("fal.ai key set in the Admin by %s (ends ...%s)", Who.Id, FalKeys.Last4(Key))
        return State

    @App_.post("/api/admin/fal-key/check")
    async def CheckFalKey(authorization: str | None = Header(None)):
        """Test the key in use with a free upload (no model runs, no cost). Allowed in production too: it changes nothing."""
        Admin(authorization)
        if not Ctx.Settings.FalKey:
            raise HttpError(409, "no_fal_key", "No fal.ai key is configured.")
        await FalKeys.CheckKey(Ctx.Settings.FalKey)
        return {"ok": True, "source": _FalKeyState()["source"], "last4": FalKeys.Last4(Ctx.Settings.FalKey)}

    @App_.delete("/api/admin/fal-key")
    async def RemoveFalKey(authorization: str | None = Header(None)):
        Who = Admin(authorization)
        if Ctx.Settings.Production:
            raise HttpError(409, "fal_key_locked", "In production the fal.ai key is set in the server configuration.")
        Env = (os.environ.get("FAL_KEY") or None)
        State = _ApplyFalKey(Env, False, Who)
        FalKeys.RemoveSaved(Ctx.Settings.DataDir)
        Logger.warning("fal.ai key saved in the Admin removed by %s", Who.Id)
        return State

    # ── AI price list (cost estimates) ─────────────────────────────────────
    @App_.get("/api/admin/ai-prices")
    async def GetPrices(authorization: str | None = Header(None)):
        Admin(authorization)
        return {**Prices.Current(), "fal_key_configured": bool(Ctx.Settings.FalKey)}

    @App_.put("/api/admin/ai-prices")
    async def SavePrices(Body_: dict = Body(...), authorization: str | None = Header(None)):
        Who = Admin(authorization)
        try:
            return Prices.Save(Body_.get("prices") or {}, Who.Id, str(Body_.get("note") or "Edited in Admin")[:200])
        except PriceError as E:
            raise HttpError(400, "invalid_price_list", str(E)) from E

    @App_.post("/api/admin/ai-prices/refresh")
    async def RefreshPrices(authorization: str | None = Header(None)):
        Who = Admin(authorization)
        try:
            return await asyncio.to_thread(Prices.RefreshFromFal, Ctx.Settings.FalKey, Who.Id)   # read-only, free
        except PriceError as E:
            raise HttpError(400, "price_refresh_failed", str(E)) from E
