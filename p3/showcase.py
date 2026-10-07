"""Hero showcase — prototype (/showcase, not linked from the site, not indexed).

The story of one real Inspiration Gallery design, read from the database for a scripted ~16 s sequence:
its prompt → its first four options → the one chosen → its refinement → its 360° movie → Silver and Yellow Gold at
the same angle → the finished ring in its original look, with the call to action. The same ring is the hero from
the selection to the end; the final image is the design's own render, shown as a design preview.
Read-only and free: nothing is generated, charged or saved. Only the design's own history is used (the batches
of the gallery master itself — never a customer's variation), and no Ring ID is part of what it returns.

The best story is chosen automatically: four ready first options, a refinement of one of them, and a real movie
of the refined result score highest. The movie always shows the ring just on screen: when the refined ring has no
movie of its own, the story shows the chosen option and its movie instead (no refinement beat). A placeholder movie
from a session run in mock mode is used only where nothing real exists; a design started from an uploaded image
scores lower, because its prompt alone does not explain the result. A gallery design that is its owner's own
variation tells the story from the design it came from. ?design=<link name> picks another one for review.

What may be told (2026-10-07): only XJet's own gallery designs (a customer's design keeps its prompt and its
refinement words private), of a product on offer to the visitor (Admin → Settings → Products; an Admin's browser
previews every product), whose product's 360° movie switch is ON (a movie that may not be shown is never played).
When no story may be told, the answer carries a still — the first such gallery design's image and name, a customer's
included (the gallery shows it anyway) — and the hero shows it instead of the animation.
"""

import re
from functools import lru_cache
from pathlib import Path

from PIL import Image

from p3 import assets
from p3 import products as Products
from p3.context import Context
from p3.gallery import ShareSlug

Metals = ("silver", "gold_18k_yellow")      # the metal beat compares Silver and 18K Yellow Gold, then ends in gold
Fillers = ("kind of", "sort of", "something like", "a bit", "like", "etc")    # trailing filler of a typed instruction


def _Cap(Text: str) -> str:
    return Text[:1].upper() + Text[1:]


def ShortPrompt(Text: str, Max: int = 72) -> str:
    """The prompt as the composer types it: whole when short, else its first clause, else cut at a word."""
    T = " ".join((Text or "").split())
    if len(T) <= Max:
        return _Cap(T)
    First = re.split(r"(?<=[.!?;,])\s", T)[0].rstrip(".,;:!? ")
    if 24 <= len(First) <= Max:
        return _Cap(First)
    return _Cap(T[:Max].rsplit(" ", 1)[0].rstrip(".,;:!? ")) + "…"


def ShortInstruction(Text: str, Max: int = 64) -> str:
    """The refinement request as the chip types it: whole, without a trailing filler ("… wave lattice kind of" →
    "… wave lattice"), so it reads as a finished sentence; very long ones are cut at a word."""
    T = " ".join((Text or "").split()).rstrip(" .,;:!")
    while True:
        Low = T.lower()
        Hit = next((F for F in Fillers if Low.endswith(" " + F)), None)
        if not Hit:
            break
        T = T[:-len(Hit)].rstrip(" .,;:!")
    return _Cap(T if len(T) <= Max else T[:Max].rsplit(" ", 1)[0] + "…")


@lru_cache(maxsize=64)
def _ToneOf(Path_: str, Mtime: float) -> str:
    """"gold" when the ring in the image is warm (yellow or rose metal), else "silver" — so the metal beat shows the
    render untouched for its own metal and filters it only for the other one."""
    with Image.open(Path_) as Img:
        Img = Img.convert("RGB")
        Img.thumbnail((96, 96))
        Px, Warm, N = Img.tobytes(), 0.0, 0
        for I in range(0, len(Px), 3):
            R, G, B = Px[I], Px[I + 1], Px[I + 2]
            Lum = 0.2126 * R + 0.7152 * G + 0.0722 * B
            if 30 < Lum < 238:                        # the ring, not the white background or deep shadow
                Warm += R - B; N += 1
    return "gold" if N and Warm / N / 255 > 0.10 else "silver"


def Tone(Ctx: Context, AssetPath: str) -> str:
    try:
        P = assets.Resolve(Ctx.Settings.AssetsDir, AssetPath)
        return _ToneOf(str(P), P.stat().st_mtime)
    except Exception:  # noqa: BLE001 — a missing or unreadable image only changes which metal is filtered
        return "gold"


def _Movies(Db, DesignIds: list[str]) -> dict[str, tuple[str, bool]]:
    """candidate id → (movie asset, real?). A movie made by the mock provider (request id "mockreq_…", e.g. a
    placeholder clip from a session run in mock mode) counts only when no real movie exists for that ring."""
    Out: dict[str, tuple[str, bool]] = {}
    Q = ",".join("?" * len(DesignIds))
    for M in Db.All(f"SELECT m.candidate_id, m.asset_path, m.provider_request_id FROM movies m JOIN candidates c ON c.id = m.candidate_id "
                    f"JOIN batches b ON b.id = c.batch_id WHERE b.design_id IN ({Q}) AND m.status = 'ready' "
                    f"AND m.asset_path IS NOT NULL ORDER BY m.updated_at", DesignIds):
        Real = not (M["provider_request_id"] or "").startswith("mockreq_")
        if Real or not Out.get(M["candidate_id"], ("", False))[1]:
            Out[M["candidate_id"]] = (M["asset_path"], Real)
    return Out


def _Stories(Ctx: Context, Visible: tuple = Products.All) -> list[dict]:
    Db = Ctx.Db
    Out = []
    for R in Db.All("SELECT g.id AS item_id, g.design_id, g.candidate_id, g.position, g.owner_kind, d.title, d.prompt, "
                    "d.share_slug, d.source_design_id, d.owner_account_id, d.product_type FROM gallery_items g "
                    "JOIN designs d ON d.id = g.design_id ORDER BY g.position, g.created_at"):
        Product = R["product_type"] or Products.Ring
        if (Product not in Visible or not Products.MovieAvailable(Ctx, Product)
                or (R["owner_kind"] or "xjet") != "xjet"):
            continue                    # not on offer, its movie switched off, or a customer's design (private words)
        Prompt, Designs = R["prompt"], [R["design_id"]]
        Initial = Db.One("SELECT * FROM batches WHERE design_id = ? AND kind = 'initial' ORDER BY created_at LIMIT 1", (R["design_id"],))
        if Initial is None and R["source_design_id"]:
            # a variation the owner made of their own design: its first options are in the design it came from
            Src = Db.One("SELECT id, prompt, owner_account_id FROM designs WHERE id = ?", (R["source_design_id"],))
            if Src and Src["owner_account_id"] == R["owner_account_id"]:
                Initial = Db.One("SELECT * FROM batches WHERE design_id = ? AND kind = 'initial' ORDER BY created_at LIMIT 1", (Src["id"],))
                Prompt, Designs = Src["prompt"], [R["design_id"], Src["id"]]
        if Initial is None:
            continue
        Options = Db.All("SELECT id, slot, asset_path FROM candidates WHERE batch_id = ? AND status = 'ready' "
                         "AND asset_path IS NOT NULL ORDER BY slot", (Initial["id"],))[:4]
        if len(Options) < 4:
            continue
        Ids = [O["id"] for O in Options]
        Movies = _Movies(Db, Designs)
        if not Movies:
            continue
        Base = (-3 if Initial["reference_asset"] else 0) + (1 if 20 <= len(" ".join((Prompt or "").split())) <= 160 else 0)
        Variants = []
        # 1. the whole story: a refinement of one of the four, and the 360° movie of the refined ring
        for B in Db.All("SELECT * FROM batches WHERE design_id = ? AND kind = 'refine' ORDER BY created_at", (R["design_id"],)):
            if B["parent_candidate_id"] not in Ids:
                continue
            for C in Db.All("SELECT id, asset_path FROM candidates WHERE batch_id = ? AND status = 'ready' "
                            "AND asset_path IS NOT NULL ORDER BY slot", (B["id"],)):
                if C["id"] in Movies:
                    Asset, Real = Movies[C["id"]]
                    Variants.append((10 + Base - (0 if Real else 8) + (1 if C["id"] == R["candidate_id"] else 0),
                                     Ids.index(B["parent_candidate_id"]), (B, C), Asset))
        # 2. no refinement with a movie of its own: the chosen option, enlarged, then its own movie (never a movie of
        #    a different ring than the one just shown)
        for I, O in enumerate(Options):
            if O["id"] in Movies:
                Asset, Real = Movies[O["id"]]
                Variants.append((5 + Base - (0 if Real else 8) + (1 if O["id"] == R["candidate_id"] else 0), I, None, Asset))
        if not Variants:
            continue
        Score, Picked, Refined, Movie = max(Variants, key=lambda V: V[0])
        Out.append({"row": R, "prompt": Prompt, "options": Options, "picked": Picked, "refined": Refined, "movie": Movie,
                    "score": Score, "slug": R["share_slug"] or ShareSlug(R["title"])})
    return Out


def _Still(Ctx: Context, Visible: tuple) -> dict | None:
    """The hero without a story: the first gallery design of a product on offer — its image and its name."""
    for R in Ctx.Db.All("SELECT d.title, d.product_type, c.asset_path FROM gallery_items g JOIN designs d ON d.id = g.design_id "
                        "JOIN candidates c ON c.id = g.candidate_id WHERE c.status = 'ready' AND c.asset_path IS NOT NULL "
                        "ORDER BY g.position, g.created_at"):
        if (R["product_type"] or Products.Ring) in Visible:
            return {"title": R["title"], "image_url": Ctx.AssetUrl(R["asset_path"])}
    return None


def Story(Ctx: Context, Design: str | None = None, Visible: tuple = Products.All) -> dict:
    Stories = _Stories(Ctx, Visible)
    Ranked = sorted(Stories, key=lambda S: (-S["score"], S["row"]["position"]))
    Choices = [{"slug": S["slug"], "title": S["row"]["title"]} for S in Ranked]
    if not Ranked:
        Out = {"story": None, "choices": []}
        Still = _Still(Ctx, Visible)
        if Still:
            Out["still"] = Still                       # the hero shows the still instead of an empty stage
        return Out
    Want = ShareSlug(Design or "")
    Best = next((S for S in Ranked if Want and Want in (S["slug"], ShareSlug(S["row"]["title"]))), Ranked[0])
    Url, Base = Ctx.AssetUrl, Ctx.Settings.BasePath
    Materials = {M["id"]: M for G in Ctx.Catalog.ToJson()["groups"] for M in G["materials"]}
    Movie = Url(Best["movie"])
    Still = Best["refined"][1]["asset_path"] if Best["refined"] else Best["options"][Best["picked"]]["asset_path"]
    return {
        "story": {
            "title": Best["row"]["title"],
            "slug": Best["slug"],
            "prompt": ShortPrompt(Best["prompt"]),
            "options": [Url(O["asset_path"]) for O in Best["options"]],
            "picked": Best["picked"],
            "refine": ({"text": ShortInstruction(Best["refined"][0]["user_text"]), "image_url": Url(Best["refined"][1]["asset_path"])}
                       if Best["refined"] else None),
            "movie_url": Movie,
            "poster_url": Movie.replace("/assets/", "/poster/", 1),
            "clip_url": Movie.replace("/assets/", "/clip/", 1) + "?tail=2&w=720",   # its last 2 s, small (p3/media.py)
            "metals": [Materials[I] for I in Metals if I in Materials],
            "still_url": Url(Still),                  # the hero ring from the metal beat to the end: its own render
            "tone": Tone(Ctx, Still),                 # "gold" | "silver": the render's own metal, shown unfiltered
            "cta_url": f"{Base}/",
        },
        "choices": Choices,
    }
