"""FastAPI application for Pipeline 3.

Run:  .venv/Scripts/python -m uvicorn p3.app:App --port 8310
With P3_BASE_PATH=/JewelryB2C3 every page, API, static file and asset is served
under that prefix (the deployment layout behind proto/tron); without it, at "/".
"""

import html
import json
import logging
import mimetypes
import os
import re
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Body, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware

from p3 import assets
from p3 import media as Media
from p3 import products as Products
from p3 import showcase as Showcase

# Font files: some platforms' mimetypes tables lack WOFF2, and browsers want the right type for preloaded fonts
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")
from p3.accounts import BuildProvider as BuildAccountProvider, InsufficientCredits
from p3.auth import RequireDeveloper, RequirePrincipal
from p3.config import LoadCatalog, LoadGenerationConfig
from p3.context import Context, HttpError
from p3.customize import CustomizeService
from p3.db import Database
from p3.designs import DesignService
from p3.images import ImageService
from p3.meshes import MeshService
from p3.migrations import MigrateToAccounts
from p3.admin import RegisterAdmin
from p3.aipricing import PriceBook
from p3.modelconfig import ModelConfigStore
from p3.charmprices import CharmPriceBook
from p3 import charmprices as CharmPrices
from p3.materialprices import MaterialPriceBook
from p3.production3d import Production3D
from p3.geoqueue import GeometryQueue
from p3.gallery import GalleryService
from p3.mail import BuildMailer
from p3.orders import OrderService
from p3.promos import PromoService
from p3.payments import BuildPaymentProvider
from p3.addressing import BuildValidator
from p3.registration import PublicOrigin, RegistrationService
from p3 import sessions as Sessions
from p3.usage import BackfillUsageAnnotations
from p3.modes import DefaultFactories, ModeManager, ResolveStartupMode
from p3.movies import MovieService
from p3.pricing.service import PricingService
from p3.settings import LoadSettings, Settings, WebDir

Logger = logging.getLogger("p3.app")


class Services:
    def __init__(self, Ctx: Context, Mailer=None):
        self.Ctx = Ctx
        self.Images = ImageService(Ctx)
        self.Movies = MovieService(Ctx)
        self.Customize = CustomizeService(Ctx, self.Images, self.Movies)
        self.Designs = DesignService(Ctx, self.Images, self.Customize)
        self.Meshes = MeshService(Ctx)
        self.Geometry = GeometryQueue(Ctx)                       # one heavy local STL job at a time, persisted
        self.Production3D = Production3D(Ctx, self.Meshes, self.Geometry)   # admin-only; never automatic
        self.Gallery = GalleryService(Ctx)                       # Inspiration Gallery: curated XJet designs
        self.Promos = PromoService(Ctx)                          # promo codes (Admin), evaluated server-side
        # Checkout + orders: payment and address validation sit behind adapters (none connected yet).
        self.Orders = OrderService(Ctx, self.Customize, self.Promos, BuildPaymentProvider(), BuildValidator(), Mailer, self.Production3D)

    def Reconcile(self) -> dict:
        return {"candidates": self.Images.Reconcile(), "movies": self.Movies.Reconcile(),
                "meshes": self.Meshes.Reconcile(), "geometry_jobs": self.Geometry.Reconcile(),
                "production_3d": self.Production3D.Reconcile()}


class _NoGzipForMedia:
    """ASGI middleware: strips Accept-Encoding for media paths so GZipMiddleware (inner) passes them through."""
    Prefixes = ("/assets/", "/thumb/", "/poster/", "/clip/", "/static/videos/", "/static/images/", "/static/vendor/fonts/",
                "/stl/", "/download")                 # 3D downloads (up to ~250 MB STL) stream as they are

    def __init__(self, App):
        self.App = App

    async def __call__(self, Scope, Receive, Send):
        if Scope["type"] == "http" and any(P in Scope.get("path", "") for P in self.Prefixes):
            Scope = dict(Scope)
            Scope["headers"] = [(K, V) for K, V in Scope.get("headers", []) if K.lower() != b"accept-encoding"]
        await self.App(Scope, Receive, Send)


def _ScriptJson(Value) -> str:
    """JSON that is safe inside an inline <script> (no "</script>" or HTML-significant characters can break out)."""
    return json.dumps(Value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _VersionedPage(Name: str, BasePath: str) -> str:
    """Render a page: every "{{BASE}}" becomes the base path, and local scripts/styles get
    ?v=<mtime> so a browser can never pair a new page with a cached older app.js."""
    Html = (WebDir / Name).read_text(encoding="utf-8")
    for Asset in ("app.js", "admin.js", "products.js", "metal.js", "showcase.js", "showcase.css", "styles.css", "vendor/tailwind.css", "vendor/fonts.css", "vendor/alpine.min.js",
                  "vendor/three.min.js", "vendor/STLLoader.js", "vendor/OrbitControls.js"):
        Path_ = WebDir / Asset
        if Path_.is_file():
            Html = Html.replace(f'"{{{{BASE}}}}/static/{Asset}"', f'"{{{{BASE}}}}/static/{Asset}?v={Path_.stat().st_mtime_ns}"')
    return Html.replace("{{BASE}}", BasePath)


def CreateApp(SettingsObj: Settings | None = None, ProviderObj=None, ProviderFactories: dict | None = None) -> FastAPI:
    S = SettingsObj or LoadSettings()
    Base = S.BasePath
    S.AssetsDir.mkdir(parents=True, exist_ok=True)
    S.DevDir.mkdir(parents=True, exist_ok=True)
    # Asset paths add ~130 characters (designs/<id>/candidates/<id>.png); Windows fails past 260.
    if os.name == "nt" and len(str(S.AssetsDir)) > 120:
        Logger.warning("P3_DATA_DIR is %d characters long; generated asset paths may exceed the Windows "
                       "260-character limit and fail to save. Use a shorter data directory.", len(str(S.AssetsDir)))
    Catalog = LoadCatalog()
    # Identity is a separate domain (own provider + own storage); app data keys rows by account id only.
    Accounts = BuildAccountProvider(S.AccountProvider, S.DataDir)
    Migrated = MigrateToAccounts(S.DbPath, Accounts)    # one-time, pre-accounts databases only
    Factories = ProviderFactories or DefaultFactories(S)
    if ProviderObj is not None:                       # tests inject a provider instance
        Mode, ModeSource = ("live" if ProviderObj.Name == "fal" else "mock"), "injected"
    else:
        Mode, ModeSource = ResolveStartupMode(S)
    Ctx = Context(Settings=S, Db=Database(S.DbPath), Provider=ProviderObj or Factories[Mode](),
                  Gen=LoadGenerationConfig(), Catalog=Catalog,
                  Pricing=PricingService(Catalog, S.PricingProfilePath, S.AllowUnapprovedPricing),
                  Accounts=Accounts)
    Ctx.Products = Products.ProductSettings(Ctx.Db)   # rings and charms (charms hidden from customers by default)
    Ctx.Models = ModelConfigStore(Ctx.Db)        # seeds v1 from generation.json + prompts on first start
    Ctx.MaterialPrices = MaterialPriceBook(Ctx.Db, Catalog)   # material pricing table (Admin), seeded once
    Ctx.Pricing.Book = Ctx.MaterialPrices         # the website's fixed price per material comes from it
    Ctx.CharmPrices = CharmPriceBook(Ctx.Db, Catalog, Ctx.Products)   # charm prices per material and size, seeded empty
    Mailer = BuildMailer(S.DataDir)
    Svc = Services(Ctx, Mailer)
    Ctx.MaterialPrices.OnSave.append(Svc.Production3D.RepriceMissing)
    Ctx.CharmPrices.OnSave.append(Svc.Production3D.RepriceMissing)      # a charm result waiting for charm $/g
    Sessions.BackfillBagEvents(Ctx)               # bag lines can be removed later; keep their bag_added
    Sessions.BackfillDesignModes(Ctx)             # mark older sessions mock / live from their requests
    Annotated = BackfillUsageAnnotations(Ctx)     # provider/endpoint on usage recorded before they were captured
    if Annotated:
        Logger.info("Annotated %d earlier usage events with provider/endpoint", Annotated)
    Modes = ModeManager(Ctx, Mode, ModeSource, Factories)
    Registration = RegistrationService(Ctx, Mailer)

    @asynccontextmanager
    async def Lifespan(_App):
        Summary = Svc.Reconcile()
        Logger.info("Startup reconciliation: %s (provider=%s)", Summary, Ctx.Provider.Name)
        Logger.warning("AI mode: %s (%s); base path: %s", Modes.Mode.upper(), Modes.Source, Base or "/")
        _App.state.Reconciliation = Summary
        if Migrated:
            Logger.warning("Account migration on startup: %s", Migrated)
        yield
        await Ctx.Runner.Shutdown()

    # With a base path the routes live on an inner app mounted at Base; Starlette does not run a
    # mounted app's lifespan, so the outer app owns it.
    App_ = FastAPI(title="XJet Jewelry Builder — Pipeline 3", lifespan=None if Base else Lifespan,
                   docs_url="/docs", openapi_url="/openapi.json")
    App_.state.Ctx = Ctx
    App_.state.Services = Svc
    App_.state.Modes = Modes
    App_.state.Mailer = Mailer

    Immutable = "public, max-age=31536000, immutable"
    # Compress text (HTML, JSON, JS, CSS) on the way out; media is already compressed and must stay byte-exact
    # for range requests, so those paths never see an Accept-Encoding header.
    App_.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    App_.add_middleware(_NoGzipForMedia)

    @App_.middleware("http")
    async def _NoStaleUi(Req: Request, CallNext):
        # The page and its scripts change with every release; make browsers revalidate
        # (cheap 304s via ETag) instead of running a cached, outdated app.js.
        Resp = await CallNext(Req)
        Rel = Req.url.path[len(Base):] if Base and Req.url.path.startswith(Base) else Req.url.path
        if Rel.startswith(("/assets/", "/thumb/", "/poster/", "/clip/")):
            # generated files never change under their URL (unique ids); derived media follows its source
            Resp.headers.setdefault("Cache-Control", Immutable)
        elif Rel.startswith("/static/vendor/fonts/"):
            Resp.headers.setdefault("Cache-Control", "public, max-age=2592000")
        elif Rel in ("/", "/dev", "/admin", "/admin/", "/showcase") or Rel.startswith(("/static/", "/design/")):
            Resp.headers["Cache-Control"] = Immutable if Req.query_params.get("v") else "no-cache"
        return Resp

    @App_.exception_handler(HttpError)
    async def _HttpError(_Req: Request, E: HttpError):
        Err = {"code": E.Code, "message": E.Message}
        if getattr(E, "Problems", None):              # field-level problems (checkout forms)
            Err["problems"] = E.Problems
        return JSONResponse(status_code=E.Status, content={"error": Err})

    @App_.exception_handler(InsufficientCredits)
    async def _NoCredits(_Req: Request, E: InsufficientCredits):
        return JSONResponse(status_code=402, content={"error": {"code": "quota_exhausted", "message": E.Message}})

    def Tok(XAccessToken: str | None):
        """Resolve the caller to a Principal (the only way routes learn who is calling)."""
        return RequirePrincipal(Ctx, XAccessToken)

    # ── pages / static ───────────────────────────────────────────────────
    @App_.get("/", include_in_schema=False)
    async def Index():
        return HTMLResponse(_VersionedPage("index.html", Base))

    @App_.get("/dev", include_in_schema=False)
    async def DevPage():
        return HTMLResponse(_VersionedPage("dev.html", Base))

    @App_.get("/design/{Slug}", include_in_schema=False)
    async def SharedDesignPage(Slug: str, request: Request):
        """A shared gallery design by name (/design/aurora-twist): the site opens with that design's preview, and
        the page carries the social-preview tags — design name, "Designed with XJet Atelier", the ring image —
        that messaging apps and social networks read before anyone taps. No Ring ID anywhere in it."""
        Html = _VersionedPage("index.html", Base)
        SiteTitle = html.unescape(re.search(r"<title>(.*?)</title>", Html, re.S).group(1))
        Found = Svc.Gallery.Resolve(Slug, Products.VisibleProducts(Ctx, request))
        if Found is None:
            return HTMLResponse(Html.replace("</head>", '<script>window.__p3Open = {"gallery": null};</script>\n</head>', 1), status_code=404)
        Origin = PublicOrigin(request)
        Url = f"{Origin}{Base}/design/{Found['slug']}"
        Title = html.escape(Found["title"], quote=True)
        Image = html.escape(f"{Origin}{Media.ThumbUrl(Found['image_url'], 800, 'jpg')}", quote=True)
        Noun = "charm" if Found.get("product_type") == Products.Charm else "ring"
        Description = f"Designed with XJet Atelier — a {Noun} from the Inspiration Gallery. See it in 360°, choose your metal and make it yours."
        Tags = "\n".join([
            f'<meta property="og:type" content="website">',
            f'<meta property="og:site_name" content="XJet Atelier">',
            f'<meta property="og:title" content="{Title}">',
            f'<meta property="og:description" content="{Description}">',
            f'<meta property="og:image" content="{Image}">',
            f'<meta property="og:image:alt" content="{Title} — a {Noun} designed with XJet Atelier">',
            f'<meta property="og:url" content="{html.escape(Url, quote=True)}">',
            f'<meta name="twitter:card" content="summary_large_image">',
            f'<meta name="twitter:title" content="{Title}">',
            f'<meta name="twitter:description" content="{Description}">',
            f'<meta name="twitter:image" content="{Image}">',
            f'<link rel="canonical" href="{html.escape(Url, quote=True)}">',
            f'<script>window.__p3Open = {_ScriptJson({"gallery": Found["item_id"], "site_title": SiteTitle})};</script>',
        ])
        # Literal replacements (a function, so nothing in a title or the JSON is read as a regex escape)
        Html = re.sub(r"<title>.*?</title>", lambda _M: f"<title>{Title} · XJet Atelier</title>", Html, count=1, flags=re.S)
        Html = re.sub(r'<meta name="description" content="[^"]*">',
                      lambda _M: f'<meta name="description" content="{Description}">\n    {Tags}', Html, count=1)
        return HTMLResponse(Html)

    @App_.get("/showcase", include_in_schema=False)
    async def ShowcasePage():
        """Hero showcase — a prototype for review (p3/showcase.py). Not linked from the site and not indexed; the
        homepage hero stays as it is until the showcase is approved."""
        return HTMLResponse(_VersionedPage("showcase.html", Base), headers={"X-Robots-Tag": "noindex, nofollow"})

    @App_.get("/api/showcase")
    async def ShowcaseRoute(design: str | None = None):
        """The story of one real gallery design for the showcase (read-only; nothing is generated or charged)."""
        return Showcase.Story(Ctx, design)

    RegisterAdmin(App_, Ctx, lambda Name: _VersionedPage(Name, Base), Svc.Production3D, PriceBook(Ctx.Db), Svc.Gallery,
                  Svc.Orders, Svc.Promos)

    # ── derived media: thumbnails and movie posters, made on first request and cached (p3/media.py) ──
    @App_.get("/thumb/{Rel:path}", include_in_schema=False)
    async def ThumbRoute(Rel: str, request: Request, w: int = 320, f: str | None = None):
        Fmt = f if f in Media.Formats else ("webp" if "image/webp" in request.headers.get("accept", "") else "jpg")
        try:
            Path_, Ctype = await run_in_threadpool(Media.Thumb, S.AssetsDir, Rel, int(w), Fmt)
        except (assets.AssetError, ValueError, OSError) as E:
            raise HttpError(404, "asset_not_found", str(E))
        return FileResponse(Path_, media_type=Ctype, headers={"Cache-Control": Immutable, "Vary": "Accept"})

    @App_.get("/clip/{Rel:path}", include_in_schema=False)
    async def ClipRoute(Rel: str, tail: float = 2.0, w: int = 720):
        try:
            Path_ = await run_in_threadpool(Media.Clip, S.AssetsDir, Rel, float(tail), int(w))
        except (assets.AssetError, ValueError, OSError) as E:
            raise HttpError(404, "asset_not_found", str(E))
        if Path_ is None:
            raise HttpError(404, "clip_unavailable", "No clip for this movie.")
        return FileResponse(Path_, media_type="video/mp4", headers={"Cache-Control": Immutable})

    @App_.get("/poster/{Rel:path}", include_in_schema=False)
    async def PosterRoute(Rel: str):
        try:
            Path_ = await run_in_threadpool(Media.Poster, S.AssetsDir, Rel)
        except (assets.AssetError, OSError) as E:
            raise HttpError(404, "asset_not_found", str(E))
        if Path_ is None:
            raise HttpError(404, "poster_unavailable", "No poster frame for this movie.")
        return FileResponse(Path_, media_type="image/jpeg", headers={"Cache-Control": Immutable})

    App_.mount("/static", StaticFiles(directory=WebDir), name="static")
    App_.mount("/assets", StaticFiles(directory=S.AssetsDir), name="assets")

    # ── session / catalog / quote ────────────────────────────────────────
    @App_.get("/api/health")
    async def Health():
        return {"ok": True, "provider": Ctx.Provider.Name,
                "mode": Modes.Mode, "mode_source": Modes.Source, "base_path": Base or "/",
                "pricing_profile": Ctx.Pricing.ProfileVersion,
                "pricing_profile_approved": bool(Ctx.Pricing.Profile and Ctx.Pricing.Profile["approved"]),
                "unapproved_pricing_allowed": S.AllowUnapprovedPricing,
                "config_versions": {M: Ctx.Models.Active(M).Id for M in ("nano-banana-pro", "nano-banana-pro-edit",
                                                                          "minimax-camera", "hi3d")},
                "charm_config_versions": {M: Ctx.Models.Active(M).Id for M in ("nano-banana-pro-charm", "nano-banana-pro-edit-charm",
                                                                                "minimax-camera-charm", "hi3d-charm")}}

    @App_.get("/api/session")
    async def Session(x_access_token: str | None = Header(None)):
        return Ctx.Accounts.Profile(Tok(x_access_token))

    # ── sign-in / registration (Pipeline 2 JewelryB2C2 flow) ─────────────
    @App_.post("/api/register")
    async def Register(Req: Request, Background: BackgroundTasks, Body_: dict = Body(...)):
        return Registration.Register(Req, Body_.get("Name", ""), Body_.get("Email", ""), Background.add_task)

    @App_.post("/api/register-token")
    async def RegisterToken(Body_: dict = Body(...)):
        return Registration.SignInWithToken(Body_.get("Token", ""), Body_.get("Via", "token"))

    @App_.get("/api/token-status")
    async def TokenStatus(x_access_token: str | None = Header(None)):
        return Ctx.Accounts.Profile(Tok(x_access_token))

    @App_.get("/verify", include_in_schema=False)
    async def VerifyEmail(Req: Request, Background: BackgroundTasks, token: str = ""):
        return HTMLResponse(Registration.VerifyPage(Req, token, Background.add_task))

    @App_.post("/api/events")
    async def SessionEvent(Body_: dict = Body(...), x_access_token: str | None = Header(None)):
        return Sessions.RecordClientEvent(Ctx, Tok(x_access_token).AccountId, str(Body_.get("kind", "")),
                                          Body_.get("design_id") or None)

    # ── while charms are hidden, a ring-only customer gets exactly the answers of before: no product fields ──
    ProductKeys = ("product_type", "product_types", "charm_size", "size_label")

    def _HasCharm(X) -> bool:
        if isinstance(X, dict):
            return (X.get("product_type") == Products.Charm or Products.Charm in (X.get("product_types") or ())
                    or any(_HasCharm(V) for V in X.values()))
        return isinstance(X, list) and any(_HasCharm(V) for V in X)

    def _Strip(X):
        if isinstance(X, dict):
            return {K: _Strip(V) for K, V in X.items() if K not in ProductKeys}
        return [_Strip(V) for V in X] if isinstance(X, list) else X

    def RingOnly(Data, request: Request):
        """Charms hidden from this browser and nothing about charms in the answer → the ring-only answer of before.
        A customer whose bag or orders hold charms (made while charms were shown) still gets them as they are."""
        if Products.CharmsVisible(Ctx, request) or _HasCharm(Data):
            return Data
        return _Strip(Data)

    # ── inspiration gallery: public tiles; "Make it yours" copies the batch into the customer's own design ──
    def Seen(request: Request) -> tuple[tuple, bool]:
        """The products this browser may see, and whether to label tiles with their product (only beside charms)."""
        Visible = Products.VisibleProducts(Ctx, request)
        return Visible, Products.Charm in Visible

    @App_.get("/api/gallery")
    async def GalleryTiles(request: Request):
        return {"items": Svc.Gallery.List(*Seen(request))}

    @App_.post("/api/gallery/{ItemId}/start")
    async def GalleryStart(ItemId: str, request: Request, Body_: dict = Body(default={}), x_access_token: str | None = Header(None)):
        Who = Tok(x_access_token)
        return Svc.Designs.Get(Who, Svc.Gallery.Start(Who, ItemId, Body_.get("client_request_id") or None, Seen(request)[0]))

    @App_.get("/api/gallery/{ItemId}/share")
    async def GalleryShare(ItemId: str, request: Request):
        """What the Share button (and Admin's Copy link) uses: the design name, the line under it and the clean
        customer link by name — no Ring ID in anything a customer passes on."""
        Share = Svc.Gallery.ShareFor(ItemId, Seen(request)[0])
        if Share is None:
            raise HttpError(404, "gallery_item_not_found", "This gallery design is no longer available.")
        return {"slug": Share["slug"], "title": Share["title"], "text": "Designed with XJet Atelier",
                "url": f"{PublicOrigin(request)}{Base}/design/{Share['slug']}"}

    # ── ♥ favorites: saved references to gallery masters, per account ──
    @App_.get("/api/favorites")
    async def FavoritesRoute(request: Request, x_access_token: str | None = Header(None)):
        return {"items": Svc.Gallery.Favorites(Tok(x_access_token), *Seen(request))}

    @App_.put("/api/favorites/{DesignId}")
    async def FavoriteRoute(DesignId: str, request: Request, x_access_token: str | None = Header(None)):
        return {"items": Svc.Gallery.Favorite(Tok(x_access_token), DesignId, *Seen(request))}

    @App_.delete("/api/favorites/{DesignId}")
    async def UnfavoriteRoute(DesignId: str, request: Request, x_access_token: str | None = Header(None)):
        return {"items": Svc.Gallery.Unfavorite(Tok(x_access_token), DesignId, *Seen(request))}

    @App_.get("/api/catalog")
    async def CatalogRoute(request: Request):
        Out = Ctx.Catalog.ToJson()
        if Products.CharmsVisible(Ctx, request):
            # Rings and charms: what the Design screen offers. Absent while charms are hidden — the ring-only site.
            Out["products"] = {"available": list(Products.All), "default": Products.Default,
                               "preview": not Ctx.Products.CharmsAvailable,      # an admin previewing hidden charms
                               "charm": {"sizes": Ctx.Products.CharmSizes, "size_options": Ctx.Products.CharmSizeOptions(),
                                         "recommended_size": Ctx.Products.CharmDefaultSize,
                                         "size_definition": Products.CharmSizeDefinition,
                                         "materials": [{"id": M.Id, "label": CharmPrices.Label(Ctx.Catalog, M.Id), "group": M.Group,
                                                        "purchasable": Ctx.Catalog.IsPurchasableGroup(M.Group)}
                                                       for M in CharmPrices.Offered(Ctx.Catalog)]}}
        return JSONResponse(Out, headers={"Cache-Control": "no-store"})

    @App_.get("/api/quote")
    async def QuoteRoute(request: Request, material_id: str, ring_size: float | None = None, product: str | None = None,
                         charm_size: float | None = None):
        if product is not None and Products.Normalize(product) == Products.Charm:
            # A charm's price depends on its material and size (the charm price book; never a ring price)
            Products.RequireVisible(Ctx, Products.Charm, request)
            try:
                Q = Ctx.CharmPrices.QuoteFor(material_id, charm_size)
            except KeyError:
                raise HttpError(404, "unknown_material", "Unknown material.")
            return {**Q.ToJson(), "product": Products.Charm, "charm_size": charm_size}
        # ring_size is accepted for clarity but deliberately ignored: size never changes price.
        try:
            Q = Ctx.Pricing.QuoteFor(material_id)
        except KeyError:
            raise HttpError(404, "unknown_material", "Unknown material.")
        return {**Q.ToJson(), "ring_size": ring_size}

    # ── designs & batches ────────────────────────────────────────────────
    @App_.post("/api/designs")
    async def CreateDesign(request: Request, prompt: str = Form(...), client_request_id: str | None = Form(None),
                           rights_confirmed: bool = Form(False), reference: UploadFile | None = File(None),
                           product: str | None = Form(None), x_access_token: str | None = Header(None)):
        Who = Tok(x_access_token)
        # The product is fixed here, once, for the design and everything refined from it. A ring needs nothing
        # new (no product sent = a ring, exactly as before); a charm only when charms are visible to this browser.
        Product = Products.RequireVisible(Ctx, Products.Normalize(product), request)
        ReferencePng = None
        if reference is not None and reference.filename:
            if not rights_confirmed:
                raise HttpError(400, "rights_not_confirmed",
                                "Please confirm you have the rights to use the uploaded image.")
            Raw = await reference.read(assets.MaxReferenceBytes + 1)
            try:
                ReferencePng = assets.NormalizeReferenceImage(Raw)
            except assets.AssetError as E:
                raise HttpError(400, "invalid_reference", str(E))
        return Svc.Images.CreateInitial(Who, prompt, ReferencePng, client_request_id, Product)

    @App_.get("/api/designs")
    async def ListDesigns(request: Request, x_access_token: str | None = Header(None)):
        return RingOnly({"designs": Svc.Designs.List(Tok(x_access_token), Products.VisibleProducts(Ctx, request))}, request)

    @App_.get("/api/designs/{DesignId}")
    async def GetDesign(DesignId: str, request: Request, x_access_token: str | None = Header(None)):
        return RingOnly(Svc.Designs.Get(Tok(x_access_token), DesignId, Products.VisibleProducts(Ctx, request)), request)

    @App_.delete("/api/designs/{DesignId}")
    async def RemoveDesign(DesignId: str, x_access_token: str | None = Header(None)):
        return Svc.Designs.Remove(Tok(x_access_token), DesignId)

    @App_.post("/api/designs/{DesignId}/batches")
    async def Refine(DesignId: str, request: Request, Body_: dict = Body(...), x_access_token: str | None = Header(None)):
        return Svc.Images.CreateRefinement(Tok(x_access_token), DesignId, Body_.get("parent_candidate_id"),
                                           Body_.get("instruction", ""), Body_.get("client_request_id"),
                                           Products.VisibleProducts(Ctx, request))

    @App_.get("/api/batches/{BatchId}")
    async def GetBatch(BatchId: str, x_access_token: str | None = Header(None)):
        B = Svc.Images._OwnedBatch(Tok(x_access_token), BatchId)
        return Svc.Images.GetBatch(B["id"])

    @App_.post("/api/batches/{BatchId}/retry-failed")
    async def RetryFailed(BatchId: str, x_access_token: str | None = Header(None)):
        return Svc.Images.RetryFailed(Tok(x_access_token), BatchId)

    @App_.post("/api/candidates/{CandidateId}/retry")
    async def RetryCandidate(CandidateId: str, x_access_token: str | None = Header(None)):
        return Svc.Images.RetryCandidate(Tok(x_access_token), CandidateId)

    @App_.put("/api/designs/{DesignId}/selection")
    async def Select(DesignId: str, Body_: dict = Body(...), x_access_token: str | None = Header(None)):
        return Svc.Customize.Select(Tok(x_access_token), DesignId, Body_.get("candidate_id"))

    # ── customize & movie ────────────────────────────────────────────────
    @App_.post("/api/designs/{DesignId}/customize")
    async def Proceed(DesignId: str, Body_: dict = Body(...), x_access_token: str | None = Header(None)):
        return Svc.Customize.Proceed(Tok(x_access_token), DesignId, Body_.get("candidate_id"))

    @App_.get("/api/customizations/{CustomizationId}")
    async def GetCustomization(CustomizationId: str, x_access_token: str | None = Header(None)):
        return Svc.Customize.Get(Tok(x_access_token), CustomizationId)

    @App_.patch("/api/customizations/{CustomizationId}")
    async def UpdateCustomization(CustomizationId: str, Body_: dict = Body(...),
                            x_access_token: str | None = Header(None)):
        return Svc.Customize.Update(Tok(x_access_token), CustomizationId, Body_)

    @App_.post("/api/candidates/{CandidateId}/movie")
    async def EnsureMovie(CandidateId: str, x_access_token: str | None = Header(None)):
        Who = Tok(x_access_token)
        Cand, _Batch = Svc.Images._OwnedCandidate(Who, CandidateId)
        if Cand["status"] != "ready":
            raise HttpError(409, "candidate_not_ready", "That image is not ready yet.")
        return Svc.Movies.Ensure(Who, CandidateId)

    # ── bag ──────────────────────────────────────────────────────────────
    @App_.get("/api/bag")
    async def GetBag(request: Request, x_access_token: str | None = Header(None)):
        return RingOnly(Svc.Customize.Bag(Tok(x_access_token)), request)

    @App_.post("/api/bag")
    async def AddToBag(request: Request, Body_: dict = Body(...), x_access_token: str | None = Header(None)):
        return RingOnly(Svc.Customize.AddToBag(Tok(x_access_token), Body_.get("customization_id")), request)

    @App_.delete("/api/bag/{LineId}")
    async def RemoveFromBag(LineId: str, request: Request, x_access_token: str | None = Header(None)):
        return RingOnly(Svc.Customize.RemoveFromBag(Tok(x_access_token), LineId), request)

    # ── checkout & orders (fixed-price materials); gold asks for a quote ──
    @App_.get("/api/checkout")
    async def CheckoutInfo(request: Request, x_access_token: str | None = Header(None)):
        return RingOnly(Svc.Orders.CheckoutInfo(Tok(x_access_token)), request)

    @App_.post("/api/checkout/quote")
    async def CheckoutQuote(request: Request, Body_: dict = Body(default={}), x_access_token: str | None = Header(None)):
        return RingOnly(Svc.Orders.Quote(Tok(x_access_token), Body_.get("promo_code"), str(Body_.get("shipping_method") or "standard")), request)

    @App_.post("/api/checkout/address")
    async def CheckoutAddress(Body_: dict = Body(default={}), x_access_token: str | None = Header(None)):
        Tok(x_access_token)
        return Svc.Orders.ValidateAddress(Body_.get("address") or Body_)

    @App_.post("/api/orders")
    async def PlaceOrder(request: Request, Background: BackgroundTasks, Body_: dict = Body(...), x_access_token: str | None = Header(None)):
        return RingOnly(Svc.Orders.Create(Tok(x_access_token), Body_, Background.add_task), request)

    @App_.get("/api/orders")
    async def MyOrders(request: Request, x_access_token: str | None = Header(None)):
        return RingOnly({"orders": Svc.Orders.List(Tok(x_access_token))}, request)

    @App_.get("/api/orders/{OrderId}")
    async def MyOrder(OrderId: str, request: Request, x_access_token: str | None = Header(None)):
        return RingOnly(Svc.Orders.Get(Tok(x_access_token), OrderId), request)

    @App_.post("/api/quote-requests")
    async def RequestQuote(request: Request, Background: BackgroundTasks, Body_: dict = Body(...), x_access_token: str | None = Header(None)):
        return RingOnly(Svc.Orders.RequestQuote(Tok(x_access_token), Body_, Background.add_task), request)

    # ── developer-only mesh tools ────────────────────────────────────────
    @App_.get("/api/dev/status")
    async def DevStatus(authorization: str | None = Header(None)):
        RequireDeveloper(Ctx, authorization)
        Mesh = Ctx.Models.Active("hi3d")
        return {"ok": True, "mesh_defaults": Mesh.Params, "mesh_config_version": Mesh.Id}

    @App_.get("/api/dev/mode")
    async def DevMode(authorization: str | None = Header(None)):
        RequireDeveloper(Ctx, authorization)
        return Modes.Status()

    @App_.post("/api/dev/mode")
    async def DevSwitchMode(Body_: dict = Body(...), authorization: str | None = Header(None)):
        RequireDeveloper(Ctx, authorization)
        return Modes.Switch(Body_.get("mode"), Body_.get("confirmation"))

    @App_.get("/api/dev/outbox")
    async def DevOutbox(authorization: str | None = Header(None)):
        RequireDeveloper(Ctx, authorization)
        Items = Mailer.List() if hasattr(Mailer, "List") else []
        return {"mode": Mailer.Mode, "messages": [{K: M.get(K) for K in ("id", "to", "subject", "sent_at", "delivery")} for M in Items]}

    @App_.get("/api/dev/outbox/{MessageId}")
    async def DevOutboxMessage(MessageId: str, authorization: str | None = Header(None)):
        RequireDeveloper(Ctx, authorization)
        M = Mailer.Get(MessageId) if hasattr(Mailer, "Get") else None
        if M is None:
            raise HttpError(404, "message_not_found", "Message not found.")
        return M

    @App_.get("/api/dev/candidates")
    async def DevCandidates(authorization: str | None = Header(None)):
        RequireDeveloper(Ctx, authorization)
        Rows = Ctx.Db.All("SELECT c.id, c.slot, c.asset_path, b.id AS batch_id, b.kind, d.id AS design_id, d.title, "
                          "c.updated_at FROM candidates c JOIN batches b ON b.id = c.batch_id "
                          "JOIN designs d ON d.id = b.design_id WHERE c.status = 'ready' "
                          "ORDER BY c.updated_at DESC LIMIT 60")
        return {"candidates": [{**{K: R[K] for K in ("id", "slot", "batch_id", "kind", "design_id", "title",
                                                     "updated_at")},
                                "image_url": Ctx.AssetUrl(R["asset_path"])} for R in Rows]}

    @App_.post("/api/dev/candidates/{CandidateId}/meshes")
    async def DevCreateMesh(CandidateId: str, Body_: dict = Body(default={}), authorization: str | None = Header(None)):
        RequireDeveloper(Ctx, authorization)
        return Svc.Meshes.Create(CandidateId, Body_.get("settings"))

    @App_.get("/api/dev/meshes")
    async def DevListMeshes(candidate_id: str | None = None, authorization: str | None = Header(None)):
        RequireDeveloper(Ctx, authorization)
        return {"meshes": Svc.Meshes.List(candidate_id)}

    @App_.get("/api/dev/meshes/{MeshId}")
    async def DevGetMesh(MeshId: str, authorization: str | None = Header(None)):
        RequireDeveloper(Ctx, authorization)
        return Svc.Meshes.Get(MeshId)

    @App_.post("/api/dev/meshes/{MeshId}/convert-stl")
    async def DevConvert(MeshId: str, authorization: str | None = Header(None)):
        RequireDeveloper(Ctx, authorization)
        return Svc.Meshes.ConvertToStl(MeshId)

    @App_.get("/api/dev/meshes/{MeshId}/download")
    async def DevDownload(MeshId: str, kind: str = "stl", authorization: str | None = Header(None)):
        RequireDeveloper(Ctx, authorization)
        if kind not in ("stl", "original"):
            raise HttpError(400, "invalid_kind", "kind must be 'stl' or 'original'.")
        FsPath, Name = Svc.Meshes.FilePath(MeshId, kind)
        return FileResponse(FsPath, filename=f"{MeshId}_{Name}", media_type="application/octet-stream")

    if not Base:
        return App_

    Outer = FastAPI(title="XJet Jewelry Builder — Pipeline 3", lifespan=Lifespan, docs_url=None,
                    redoc_url=None, openapi_url=None)
    Outer.state.Ctx = Ctx
    Outer.state.Services = Svc
    Outer.state.Modes = Modes
    Outer.state.Mailer = Mailer

    @Outer.get(Base, include_in_schema=False)
    async def _BaseSlash():
        return RedirectResponse(Base + "/", status_code=308)

    @Outer.get("/", include_in_schema=False)
    async def _RootToBase():
        # Local convenience only: behind proto/tron, "/" never reaches Pipeline 3.
        return RedirectResponse(Base + "/", status_code=307)

    Outer.mount(Base, App_)
    return Outer


def __getattr__(Name):
    # Lazy module-level `App` for uvicorn (`p3.app:App`) so importing this module in tests has no side effects.
    if Name == "App":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        globals()["App"] = CreateApp()
        return globals()["App"]
    raise AttributeError(Name)
