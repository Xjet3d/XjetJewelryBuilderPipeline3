"""Cookie consent and analytics (docs/ANALYTICS.md): what the customer pages may load, and only after consent.

Nothing optional runs before the visitor decides (web/consent.js). Analytics is configured in Admin → Settings →
System → Analytics & cookies, and only there: on or off, the one site host the tools may run on, and for each tool
(Providers below) its own ID and whether it is on. The IDs are never in the code: each environment keeps its own in its
own database, so proto and Atelier never share one. Off — the default — or with no tool both on and holding a valid ID,
a customer page gets no analytics configuration, no consent banner is needed and the Content-Security-Policy stays as
strict as before. An ID configured for one host cannot run on another: the browser starts a tool only on that host.

A tool may load only when all of these hold: analytics is offered on this environment, the visitor accepted Analytics,
the page is on the configured host, the tool is switched on and its ID is valid. A tool without an ID never loads, and
its hosts never enter the page's policy.

Google Consent Mode, basic: the Google tag is not loaded at all until Analytics is accepted; then the four consent
signals start denied and only analytics_storage is granted (Atelier runs no advertising). Clarity loads only after
Analytics consent, told so through its consent API v2 (analytics granted, ads denied). The pages mask customer details
in recordings; the Admin and the developer tools never load either tool."""

import re

from p3.context import HttpError

Key = "analytics"                                   # product_settings (Admin → Settings → System), changes logged
ConsentVersion = 1                                  # web/consent.js asks again when this changes, or when a tool is added
HostPattern = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")

# The hosts each tool needs. Google: the Content Security Policy guide, Google Analytics "without any Ads features";
# Microsoft: the Clarity CSP guide (Clarity balances its collectors over several *.clarity.ms hosts).
GoogleSources = {"script-src": ("https://www.googletagmanager.com",),
                 "img-src": ("https://www.googletagmanager.com", "https://*.google-analytics.com"),
                 "connect-src": ("https://www.googletagmanager.com", "https://*.google-analytics.com", "https://*.google.com")}
ClaritySources = {"script-src": ("https://*.clarity.ms",),
                  "img-src": ("https://*.clarity.ms", "https://c.bing.com"),
                  "connect-src": ("https://*.clarity.ms", "https://c.bing.com")}

# The tools Atelier may offer, by consent category, in the order the Admin shows them. A marketing tool later (Google
# Ads, Meta Pixel …) is one more entry with category "marketing" — plus its policy hosts, its loader in web/consent.js
# and a new consent version; the Admin card lists it with no change of its own, and its ID is entered there like these.
Providers = {
    "ga4": {"name": "Google Analytics 4", "category": "analytics", "id_label": "Measurement ID", "example": "G-XXXXXXXXXX",
            "pattern": re.compile(r"G-[A-Z0-9]{4,20}"), "case": "upper", "test": "G-TEST", "sources": GoogleSources,
            "error": "invalid_ga4", "hint": "A GA4 measurement ID looks like G-XXXXXXXXXX (Google Analytics → Admin → Data streams)."},
    "clarity": {"name": "Microsoft Clarity", "category": "analytics", "id_label": "Project ID", "example": "abcd1234ef",
                "pattern": re.compile(r"[a-z0-9]{6,20}"), "case": "lower", "test": "test", "sources": ClaritySources,
                "error": "invalid_clarity", "hint": "A Clarity project ID is the short code in Settings → Overview of the Clarity project."},
}
Ga4Pattern, ClarityPattern = Providers["ga4"]["pattern"], Providers["clarity"]["pattern"]
Defaults = {"enabled": False, "host": "", "providers": {K: {"enabled": False, "id": ""} for K in Providers}}


def Placeholder(Tool: str, Id: str | None) -> bool:
    """A functional-test ID (G-TEST… for GA4, test… for Clarity): web/consent.js runs the banner and the events but
    loads neither tool and sends nothing — and the page's policy does not open the tool's hosts either."""
    Spec = Providers.get(Tool)
    return bool(Spec and Id) and Id.lower().startswith(Spec["test"].lower())


def Valid(Tool: str, Id: str | None) -> bool:
    return bool(Id) and bool(Providers[Tool]["pattern"].fullmatch(Id))


def Stored(Products) -> dict:
    """The saved configuration in today's shape, {"enabled", "host", "providers": {tool: {"enabled", "id"}}}, whatever
    shape it was saved in: before 2026-10-14 the IDs were flat ({"ga4": …, "clarity": …}) and a tool was on whenever
    its ID was set — read as exactly that, so nothing changes for a site until its Admin saves again."""
    Raw = Products.Get(Key)
    Raw = Raw if isinstance(Raw, dict) else {}
    Saved = Raw.get("providers") if isinstance(Raw.get("providers"), dict) else {}
    Out = {"enabled": Raw.get("enabled") is True, "host": str(Raw.get("host") or ""), "providers": {}}
    for Tool in Providers:
        P = Saved.get(Tool)
        if isinstance(P, dict):
            Out["providers"][Tool] = {"enabled": P.get("enabled") is True, "id": str(P.get("id") or "")}
        else:
            Id = str(Raw.get(Tool) or "")
            Out["providers"][Tool] = {"enabled": bool(Id), "id": Id}
    return Out


def Active(C: dict, Tool: str) -> bool:
    """The tool may load for a visitor who accepts Analytics: analytics offered, a host set, the tool on, a valid ID."""
    P = C["providers"][Tool]
    return C["enabled"] and bool(C["host"]) and P["enabled"] and Valid(Tool, P["id"])


def _Id(Tool: str, Raw) -> str:
    Spec = Providers[Tool]
    Id = str(Raw or "").strip()
    Id = Id.upper() if Spec["case"] == "upper" else Id.lower()
    if Id and not Spec["pattern"].fullmatch(Id):
        raise HttpError(400, Spec["error"], Spec["hint"])
    return Id


def Validate(Body: dict, Current: dict) -> dict:
    """The Admin's change, checked, on top of the current configuration. Body: any of "enabled", "host" and
    "providers": {tool: {"enabled", "id"}} — or, as before 2026-10-14, a flat "ga4" / "clarity" ID (that tool then on
    exactly when its ID is set). An ID must have its tool's format (empty clears it); a tool cannot be switched on
    without its ID; analytics cannot be offered without the host."""
    if not isinstance(Body, dict) or not any(K in Body for K in ("enabled", "host", "providers", *Providers)):
        raise HttpError(400, "invalid_value", "Nothing to change.")
    Out = {"enabled": Current["enabled"], "host": Current["host"],
           "providers": {T: dict(Current["providers"][T]) for T in Providers}}
    if "enabled" in Body:
        if not isinstance(Body["enabled"], bool):
            raise HttpError(400, "invalid_value", "enabled must be true or false.")
        Out["enabled"] = Body["enabled"]
    if "host" in Body:
        Host = str(Body.get("host") or "").strip().lower()
        Host = re.sub(r"^https?://", "", Host).split("/", 1)[0]
        if Host and not HostPattern.fullmatch(Host):
            raise HttpError(400, "invalid_host", "The site host is a host name only, e.g. xjetatelier.xjet3d.com (no https://, path or port).")
        Out["host"] = Host
    for Tool in Providers:
        if Tool in Body:                                   # the flat shape: on exactly when an ID is set
            Id = _Id(Tool, Body[Tool])
            Out["providers"][Tool] = {"enabled": bool(Id), "id": Id}
    if "providers" in Body:
        New = Body["providers"]
        if not isinstance(New, dict) or any(T not in Providers or not isinstance(P, dict) for T, P in New.items()):
            raise HttpError(400, "invalid_value", "providers names known tools, each with enabled and id.")
        for Tool, P in New.items():
            if "id" in P:
                Out["providers"][Tool]["id"] = _Id(Tool, P["id"])
            if "enabled" in P:
                if not isinstance(P["enabled"], bool):
                    raise HttpError(400, "invalid_value", "enabled must be true or false.")
                Out["providers"][Tool]["enabled"] = P["enabled"]
    for Tool, P in Out["providers"].items():
        if P["enabled"] and not P["id"]:
            Spec = Providers[Tool]
            raise HttpError(400, "id_required", f"Enter the {Spec['name']} {Spec['id_label']} before switching it on.")
    if Out["enabled"] and not Out["host"]:
        raise HttpError(400, "host_required", "Set the site host the analytics may run on before turning analytics on.")
    return Out


def Note(C: dict) -> str:
    """The change log's line for a configuration."""
    Tools = " · ".join(f"{Spec['name']} {'on' if C['providers'][T]['enabled'] else 'off'} {C['providers'][T]['id'] or '—'}"
                       for T, Spec in Providers.items())
    return f"analytics {'on' if C['enabled'] else 'off'} · host {C['host'] or '—'} · {Tools}"


def AdminProviders(C: dict) -> list[dict]:
    """Each tool as the Admin card shows it: its name and ID format, what is saved, and its state."""
    return [{"key": T, "name": Spec["name"], "category": Spec["category"], "id_label": Spec["id_label"], "example": Spec["example"], "case": Spec["case"],
             "enabled": C["providers"][T]["enabled"], "id": C["providers"][T]["id"], "configured": Valid(T, C["providers"][T]["id"]),
             "active": Active(C, T), "placeholder": Placeholder(T, C["providers"][T]["id"])} for T, Spec in Providers.items()]


def Public(Products) -> dict | None:
    """What a customer page may know (window.__p3.analytics): None unless at least one tool may load (Active) — then
    nothing optional exists on the page and no banner is shown. A tool that may not load is null, never its ID."""
    C = Stored(Products)
    Ids = {T: (C["providers"][T]["id"] if Active(C, T) else None) for T in Providers}
    if not any(Ids.values()):
        return None
    return {"version": ConsentVersion, "host": C["host"], **Ids}


def Csp(Base: str, P: dict | None) -> str:
    """The Content-Security-Policy for a customer page: the base policy, plus the hosts of each tool that may load."""
    if not P:
        return Base
    Extra: dict[str, list[str]] = {}
    for Tool, Spec in Providers.items():
        if P.get(Tool) and not Placeholder(Tool, P[Tool]):
            for Directive, Hosts in Spec["sources"].items():
                Extra.setdefault(Directive, []).extend(H for H in Hosts if H not in Extra.get(Directive, []))
    Parts = []
    for Part in Base.split(";"):
        Words = Part.split()
        if Words and Words[0] in Extra:
            Words += [H for H in Extra.pop(Words[0]) if H not in Words]
        if Words:
            Parts.append(" ".join(Words))
    Parts += [f"{D} {' '.join(H)}" for D, H in Extra.items()]           # a directive the base did not name
    return "; ".join(Parts)
