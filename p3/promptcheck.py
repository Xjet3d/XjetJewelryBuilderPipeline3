"""The prompt check: an LLM reads a customer's request before any paid image request is made.

Admin → Settings → AI models & prompts → any-llm (rings) / any-llm · Charm: one switch per product, OFF by default
(product setting "prompt_check"), so a site runs it only where an Admin turned it on. With it on, every new design and
every refinement is first sent to fal-ai/any-llm with the product's active instructions, which answer with one JSON
object: is_jewelry, is_feasible, reason_for_rejection, refined_prompt.

- Both booleans true: accepted. The design is made exactly as before — the image requests use the customer's words;
  the rewrite (refined_prompt) is kept for the Admin only.
- Otherwise: rejected. Nothing is created — no design, no credit, no image request. The customer reads the reason (in
  their own language, as the instructions ask) and can change the words: 422 prompt_rejected.
- No decision (a provider error, the time limit, an answer that is not that JSON object): the request goes ahead. The
  check saves money; it is not the safety net — the image model keeps its own rules.

Each check is a row in prompt_checks and one usage event at its list price (XJet's cost, never a customer credit; it
counts in the day's spend), shown on the session's AI pipeline and Journey, the customer's activity and the any-llm
page. A switch only turns on while that product's active instructions ask for the decision this check reads.
"""

import json
import logging
import time

from p3 import credits as Credits
from p3 import products as Products
from p3.accounts import Principal, UsagePromptCheck
from p3.context import Context, HttpError
from p3.db import NewId, Now
from p3.images import ValidateText
from p3.modelconfig import BuildRequest
from p3.providers import endpoints
from p3.runner import PollUntilDone

Logger = logging.getLogger("p3.promptcheck")

Key = "prompt_check"                                     # product setting: {"ring": bool, "charm": bool}
Models = {Products.Ring: "any-llm", Products.Charm: "any-llm-charm"}
Fields = ("is_jewelry", "is_feasible")                    # the decision the instructions must ask for
TimeoutS = 20.0                                           # longer than this: no decision, the request goes ahead
PollS = 0.3                                               # a check takes about 2 s; the 2 s image poll would double it
MaxReason = 300
DefaultReason = "This request can't be made into a design here. Please change your description."


def State(Ctx: Context) -> dict:
    """The switch per product: off unless an Admin turned it on on this site."""
    On = Ctx.Products.Get(Key) or {}
    return {P: On.get(P) is True for P in Products.All}


def Enabled(Ctx: Context, Product: str) -> bool:
    return State(Ctx)[Products.Normalize(Product)]


def InstructionsProblem(Ctx: Context, Product: str) -> str | None:
    """Why the product's active instructions can't drive the check (None = they can): they must ask for the JSON
    decision it reads. The seeded ring version has no instructions at all."""
    Model = Models[Products.Normalize(Product)]
    System = str(Ctx.Models.Active(Model).Params.get("system_prompt") or "")
    Missing = [F for F in Fields if F not in System]
    if Missing:
        return (f"The active {Model} instructions don't ask for {', '.join(Missing)}, the decision this check reads. "
                "Activate instructions that do, then turn the check on.")
    return None


def SetEnabled(Ctx: Context, Product: str, On: bool, By: str) -> dict:
    Product = Products.Normalize(Product)
    if On and (Problem := InstructionsProblem(Ctx, Product)):
        raise HttpError(409, "prompt_check_not_ready", Problem)
    Value = State(Ctx) | {Product: bool(On)}
    Ctx.Products.Set(Key, Value, By, f"prompt check {'on' if On else 'off'} for {Products.Plurals[Product].lower()}")
    return Value


def Frame(Text: str, Previous: str | None = None, Reference: bool = False) -> str:
    """What the LLM reads: the customer's words, with the application's context lines when there are any (the active
    instructions explain both lines)."""
    if Previous is not None:
        return f"Refinement of an existing design.\nPrevious request: {Previous}\nRequested change: {Text}"
    if Reference:
        return f"Reference image: attached (not shown to you).\nCustomer request: {Text}"
    return Text


def _Decision(Output) -> dict | None:
    """The JSON object the instructions ask for, or None (prose, Markdown around nothing usable, a cut-off answer)."""
    T = str(Output or "").strip()
    try:
        J = json.loads(T[T.find("{"):T.rfind("}") + 1])
    except (ValueError, TypeError):
        return None
    return J if isinstance(J, dict) and all(isinstance(J.get(F), bool) for F in Fields) else None


def Repeated(Ctx: Context, Who: Principal, ClientRequestId: str | None, DesignId: str | None = None) -> bool:
    """A request the app already made (the same client request id): it returns what exists, so nothing is checked."""
    if not ClientRequestId:
        return False
    Db = Ctx.Db
    if Db.One("SELECT 1 AS x FROM designs WHERE owner_account_id = ? AND client_request_id = ?", (Who.AccountId, ClientRequestId)):
        return True
    return bool(DesignId and Db.One("SELECT 1 AS x FROM batches WHERE design_id = ? AND client_request_id = ?",
                                    (DesignId, ClientRequestId)))


def _CanAfford(Ctx: Context, Who: Principal, Kind: str) -> bool:
    """A customer without the credits for the request gets the usual answer (402) without a check paid first."""
    try:
        Left = Ctx.Accounts.Profile(Who).get("remaining")
    except Exception:  # noqa: BLE001 — never block a request on this read
        return True
    return Left is None or Left >= Credits.Cost(Ctx, Kind)


async def Before(Ctx: Context, Who: Principal, Text: str, Product: str, Kind: str = "design", Previous: str | None = None,
                 Reference: bool = False, SourceDesignId: str | None = None) -> str | None:
    """Check a request before it is created. Returns the check's id (accepted, or no decision: the request goes ahead)
    or None (the check is off for the product); a rejection raises 422 prompt_rejected with the reason."""
    Product = Products.Normalize(Product)
    if not Enabled(Ctx, Product):
        return None
    Text = ValidateText(Text, "refinement" if Kind == "refinement" else "design")     # the same 400 as without the check
    if not _CanAfford(Ctx, Who, Credits.Refinement if Kind == "refinement" else Credits.Design):
        return None
    Model = Models[Product]
    Version = Ctx.Models.Active(Model)
    Checked = Frame(Text, Previous, Reference)
    Cid, T0 = NewId("pck"), time.monotonic()
    RequestId = Reason = Refined = Error = None
    try:
        Est = Credits.CheckSpendCap(Ctx, endpoints.Llm, Version.Params)
        try:
            RequestId = await Ctx.Provider.Submit(endpoints.Llm, BuildRequest(Model, Version.Params, {"user_prompt": Checked}))
            Credits.RecordUsage(Ctx, Who.AccountId, UsagePromptCheck, Cid, endpoints.Llm, Version.Params, Internal=True)
        finally:
            Credits.SubmitDone(Ctx, Est)
        Result = await PollUntilDone(Ctx.Provider, endpoints.Llm, RequestId, TimeoutS, PollS, Ctx.Settings.MaxTransientPollErrors)
        J = _Decision(Result.get("output"))
        if J is None:
            Decision = "undecided"
            Error = "The answer was not the JSON decision: " + str(Result.get("output") or Result.get("error") or "")[:300]
        elif J["is_jewelry"] and J["is_feasible"]:
            Decision, Refined = "accepted", J.get("refined_prompt") if isinstance(J.get("refined_prompt"), str) else None
        else:
            Why = J.get("reason_for_rejection")
            Decision, Reason = "rejected", (Why.strip()[:MaxReason] if isinstance(Why, str) else "") or DefaultReason
    except Exception as E:  # noqa: BLE001 — no decision: the request goes ahead (the image model keeps its own rules)
        Decision, Error = "undecided", f"{type(E).__name__}: {E}"[:400]
        Logger.warning("Prompt check %s made no decision: %s", Cid, Error)
    Ctx.Db.Execute(
        "INSERT INTO prompt_checks (id, owner_account_id, kind, product_type, text, checked_text, source_design_id, decision, "
        "reason, refined_prompt, config_version, provider_request_id, seconds, error, ai_mode, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (Cid, Who.AccountId, Kind, Product, Text, Checked, SourceDesignId, Decision, Reason, Refined, Version.Id, RequestId,
         round(time.monotonic() - T0, 2), Error, "mock" if Ctx.Provider.Name == "mock" else "live", Now()))
    if Decision == "rejected":
        raise HttpError(422, "prompt_rejected", Reason)
    return Cid


def Link(Ctx: Context, CheckId: str | None, Batch: dict) -> None:
    """The design and batch a check let through."""
    if CheckId:
        Ctx.Db.Execute("UPDATE prompt_checks SET design_id = ?, batch_id = ? WHERE id = ?",
                       (Batch["design_id"], Batch["id"], CheckId))


def Recent(Ctx: Context, Product: str, Limit: int = 50) -> list[dict]:
    """The latest checks of a product for the any-llm page: what was asked, the decision and why, who, the session."""
    Names: dict = {}

    def Who_(AccountId):
        if AccountId not in Names:
            try:
                A = Ctx.Accounts.AdminGet(AccountId)
                Names[AccountId] = A.get("name") or A.get("email") or AccountId
            except Exception:  # noqa: BLE001 — an unknown account must not hide the row
                Names[AccountId] = AccountId
        return Names[AccountId]

    Rows = Ctx.Db.All("SELECT * FROM prompt_checks WHERE product_type = ? ORDER BY created_at DESC LIMIT ?",
                      (Products.Normalize(Product), int(Limit)))
    return [{"id": R["id"], "created_at": R["created_at"], "kind": R["kind"], "text": R["text"], "decision": R["decision"],
             "reason": R["reason"], "refined_prompt": R["refined_prompt"], "error": R["error"], "seconds": R["seconds"],
             "config_version": R["config_version"], "ai_mode": R["ai_mode"], "customer": Who_(R["owner_account_id"]),
             "account_id": R["owner_account_id"], "session_id": R["design_id"] or R["source_design_id"]} for R in Rows]


def Counts(Ctx: Context, Product: str) -> dict:
    """Checks of a product per decision (all time, live and mock apart)."""
    Out = {"live": {}, "mock": {}}
    for R in Ctx.Db.All("SELECT ai_mode, decision, COUNT(*) AS n FROM prompt_checks WHERE product_type = ? "
                        "GROUP BY ai_mode, decision", (Products.Normalize(Product),)):
        Out.setdefault(R["ai_mode"], {})[R["decision"]] = R["n"]
    return Out


def ForDesign(Ctx: Context, DesignId: str, Owner: str) -> list[dict]:
    """A design's checks for its session page: the ones that let its batches through, and its refinements that were
    stopped (this customer's)."""
    return Ctx.Db.All("SELECT * FROM prompt_checks WHERE (design_id = ? OR (source_design_id = ? AND decision = 'rejected')) "
                      "AND owner_account_id = ? ORDER BY created_at", (DesignId, DesignId, Owner))
