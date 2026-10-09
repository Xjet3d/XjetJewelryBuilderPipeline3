"""The customer site's public pages, for people and for search engines.

Each public page has a real URL under the base path (/gallery, /faq, /materials, /technology, /about and the legal
pages), its own <title>, meta description, canonical link and Open Graph tags, one H1 and a sitemap entry. The page
asked for is in the HTML as real markup: a copy of its Alpine <template> that removes itself once the visitor moves to
another view (the template takes over from there), so a crawler reads the page without running scripts and a visitor
sees it before the scripts start. The FAQ is written here — the one source for the page and its FAQPage data.

Structured data says only what is true: who runs the site, the site itself, the FAQ, a gallery design's name, image and
description — no reviews, ratings, prices or availability. Search engines may index the site only where the Admin turned
it on (Settings → System → Search engines; off by default, so proto and any other copy stay out) or where the
configuration is production. The Admin, the API, the tools, sign-in links, quote links and My Account are never pages."""

from __future__ import annotations

import html
import json
import re

from p3 import products as Products

IndexKey = "site_indexing"                    # Admin → Settings → System → Search engines (product_settings)
VerificationKey = "google_site_verification"   # the content of Search Console's <meta name="google-site-verification">
VerificationPattern = re.compile(r"[A-Za-z0-9_-]{20,100}")

# The customer app's view → its path under the base path ("" is the home page). The Design screens, My Account
# (#account), checkout and confirmation are not pages: they live under the home URL and are never indexed.
Paths = {"home": "", "inspiration": "gallery", "faq": "faq", "materials": "materials", "technology": "technology",
         "designers": "about", "shipping-returns": "shipping-returns", "terms": "terms", "privacy": "privacy",
         "contact": "contact"}
Views = {P: V for V, P in Paths.items() if P}
Legal = ("terms", "privacy", "shipping-returns", "contact")
_LegalOpen = """<template x-if="['terms','privacy','shipping-returns','contact'].includes(view)">"""
_TemplateTag = re.compile(r"<template\b|</template>")
_OpenTag = re.compile(r"""<[a-zA-Z][\w:-]*(?:\s+[^\s=>/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*\s*/?>""")


def E(Text: str) -> str:
    return html.escape(str(Text), quote=True)


# ── the words that follow the products on offer (the same as web/app.js `wording`) ─────────────────────────────────────
def Words(Visible: tuple) -> dict:
    Sole = Visible[0] if len(Visible) == 1 else None
    Plurals = [Products.Plurals[P] for P in Visible]
    Lower = [P.lower() for P in Plurals]
    Noun = Products.Labels[Sole].lower() if Sole else "piece"
    Nouns = Products.Plurals[Sole].lower() if Sole else "pieces"
    return {"noun": Noun, "nouns": Nouns, "Nouns": Nouns[:1].upper() + Nouns[1:], "list": " and ".join(Plurals),
            "ring": Products.Ring in Visible, "charm": Products.Charm in Visible, "none": not Visible, "many": len(Visible) > 1,
            "sole": Sole,
            # "rings", "rings and charms" (more: "rings, charms and pendants"); "Rings & Charms" in a title
            "text": (", ".join(Lower[:-1]) + " and " + Lower[-1]) if len(Lower) > 1 else (Lower[0] if Lower else "jewelry"),
            "title": (", ".join(Plurals[:-1]) + " & " + Plurals[-1]) if len(Plurals) > 1 else (Plurals[0] if Plurals else "Jewelry")}


def _Labels(Ctx, Group: str) -> list[str]:
    return [M["label"] for G in Ctx.Catalog.ToJson()["groups"] if G["id"] == Group for M in G["materials"]]


# ── the FAQ: the page, its accordion and its FAQPage data come from here ─────────────────────────────────────────────
def Faq(Ctx, Visible: tuple) -> list[tuple[str, str]]:
    W = Words(Visible)
    Tail = " Additional jewelry categories will be introduced in future updates."
    What = ("No product is available at the moment." + Tail if W["none"] else
            W["list"] + ": choose one when you start a design." + Tail if W["many"] else
            "Currently, XJet Atelier supports " + W["Nouns"] + " only." + Tail)
    Fashion = ", ".join(_Labels(Ctx, "fashion"))
    Gold = ", ".join(L.replace(" Gold", "") for L in _Labels(Ctx, "luxury"))
    Metals = (f"Fashion jewelry: {Fashion}. Luxury: solid gold in {Gold} — luxury pieces can be previewed but are not yet "
              "available to order.")
    R, C = W["ring"], W["charm"]
    Price = ("Fashion jewelry pieces are priced per piece. A ring is priced by material — its size does not change the "
             "price. A charm is priced by material and size." if R and C else
             "Fashion jewelry pieces are priced per piece by material and size." if C else
             "Fashion jewelry pieces are priced per piece by material. Your ring size does not change the price.")
    Ring = "Ring sizes are US sizes (see the size guide on the Customize screen)"
    Charm = "charm size is its total height in millimetres, including the loop at the top"
    Usd = "Yes — all prices are in US dollars." + (f" {Ring}; a {Charm}." if R and C else f" {Ring}." if R else
                                                  f" A {Charm}." if C else "")
    return [("What jewelry can I create?", What),
            ("What metals are available?", Metals),
            ("Do I need design experience?", "None at all. Just describe what you want in plain language or upload a "
             "reference photo. Our AI creates four designs for you to choose from."),
            ("Can I see the design before I buy?", "Yes. You choose from four designs, refine your favourite as many "
             f"times as you like, and preview the selected {W['noun']} in a 360° movie before adding it to your bag."),
            ("How is the price calculated?", Price),
            ("How long does production take?", "Design happens the same day. Production takes 7–10 business days. "
             "Shipping adds about 5 business days with Express or up to 14 with free Standard delivery."),
            ("Are prices in USD?", Usd),
            ("What is NanoParticle Jetting™?", "It is XJet's proprietary metal printing technology. It jets ultra-fine "
             "metal nanoparticles at 8-micron layer precision — enabling finer detail and more complex shapes than "
             "traditional casting."),
            ("Can I make changes?", "Yes. Select any of your four designs and describe what should change — you get four "
             "new variations of that design each time."),
            ("Who can I contact for help?", "If you need assistance at any stage, reach out and a specialist will respond "
             "within one business day.")]


def FaqHtml(Items: list[tuple[str, str]]) -> str:
    """The FAQ accordion as markup (one answer open at a time; the answers are in the page, collapsed)."""
    Out = []
    for I, (Q, A) in enumerate(Items):
        Out.append(
            '<div class="border border-zinc-100 rounded-xl overflow-hidden">'
            f'<button @click="open === {I} ? open = null : open = {I}" :aria-expanded="open === {I}" '
            'class="w-full flex justify-between items-center p-6 text-left hover:bg-zinc-50 transition">'
            f'<span class="text-sm font-semibold text-zinc-800">{E(Q)}</span>'
            f'<svg class="w-4 h-4 text-zinc-400 shrink-0 transition-transform" :class="open === {I} ? \'rotate-45\' : \'\'" '
            'fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" '
            'stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg></button>'
            f'<div x-show="open === {I}" x-cloak class="px-6 pb-6"><p class="text-sm text-zinc-500 leading-relaxed">{E(A)}</p></div>'
            '</div>')
    return "\n                    ".join(Out)


# ── each page's title and description (unique, true, following the products on offer) ───────────────────────────────
def Meta(View: str, Ctx, Visible: tuple, HomeDescription: str) -> tuple[str, str]:
    W = Words(Visible)
    Fashion = _Labels(Ctx, "fashion")
    Metals = (", ".join(Fashion[:-1]) + " and " + Fashion[-1]) if len(Fashion) > 1 else (Fashion[0] if Fashion else "metal")
    return {
        "home": ("XJet Atelier — Custom AI Jewelry, Designed by You", HomeDescription),
        "inspiration": (f"Inspiration Gallery — {W['title']} Designed with AI · XJet Atelier",
                        f"Real XJet {W['text']} designs, made with AI. Open one to see it large, preview it in another "
                        "metal and make it yours — or use it as the spark for your own."),
        "faq": ("FAQ — Designing Custom Jewelry with AI · XJet Atelier",
                f"Answers about designing {W['text']} with AI at XJet Atelier: what you can create, the metals, how "
                "prices work, production in 7–10 business days and delivery."),
        "materials": ("Materials & Pricing — Real Metal, Fixed Prices · XJet Atelier",
                      f"The metals XJet Atelier prints in: {Metals} at a fixed price per {W['noun']}, and solid gold "
                      "quoted individually — appearance, finish, durability and care for each."),
        "technology": ("NanoParticle Jetting™ — How XJet Prints in Metal · XJet Atelier",
                       "XJet's NanoParticle Jetting™ prints metal in 8-micron layers at 99.8% density, with soluble "
                       f"support for fine detail and complex shapes — the technology behind every XJet Atelier {W['noun']}."),
        "designers": ("About XJet — The Inkjet Revolution · XJet Atelier",
                      "XJet pioneered 3D inkjet manufacturing in metal and ceramics with NanoParticle Jetting™: 80+ "
                      "patents, ISO 9001 and ISO 13485 certified. XJet Atelier is its custom jewelry service."),
        "shipping-returns": ("Shipping & Returns · XJet Atelier",
                             "Production takes 7–10 business days. Standard delivery is free (up to 14 business days), "
                             "Express is $45 (about 5). Prices in US dollars; returns and the two-year warranty."),
        "terms": ("Terms of Service · XJet Atelier",
                  "The terms for designing and ordering custom jewelry at XJet Atelier, a service of XJet Ltd.: the "
                  "service, your access, your content, orders, preview accuracy and liability."),
        "privacy": ("Privacy · XJet Atelier",
                    "What XJet Atelier collects, how it is used, the AI providers that process your designs, cookies "
                    "and storage, and your rights."),
        "contact": ("Contact & Support · XJet Atelier",
                    "Questions about a design, sizing, an order or something that is not working — the XJet Atelier "
                    "design team answers within one business day."),
    }[View]


# ── a crawler's reading of computed text: the same sentences web/app.js shows (the page replaces them on start) ──────
def Fallbacks(View: str, Ctx, Visible: tuple, HeroCollection: dict) -> dict[str, str]:
    """x-text expression → its text on this site, for the server copy of a page (only where the page computes it)."""
    W = Words(Visible)
    R, C = W["ring"], W["charm"]
    Tail = " — real XJet designs you can make your own, or the spark for one of yours."
    HomeGallery = ("Real XJet designs you can make your own, or the spark for one of yours." if W["none"] else
                   "Bands, signets, statement and stackable rings" + Tail if W["sole"] == Products.Ring else
                   W["text"][:1].upper() + W["text"][1:] + Tail)
    if View == "home":
        return {"'Your idea. Your style. A ' + wording.noun + ' that’s uniquely yours.'": f"Your idea. Your style. A {W['noun']} that’s uniquely yours.",
                "heroCollection.label": HeroCollection.get("label", ""), "heroCollection.text": HeroCollection.get("text", ""),
                "homeGalleryIntro": HomeGallery,
                "footerLine": f"© 2026 XJet Ltd. Custom {W['text']} designed with AI, produced with NanoParticle Jetting™."}
    if View == "inspiration":
        Intro = ("Real XJet designs. Open one to see it large." if W["none"] else
                 (W["list"] if W["many"] else "Charms" if W["sole"] == Products.Charm else "Bands, signets, statement and stackable rings")
                 + " — real XJet designs. Open one to see it large; make it yours to start from its four options.")
        return {"galleryIntro": Intro}
    if View == "materials":
        How = ("Fashion metals have a fixed price per piece — a ring’s price does not depend on its size; a charm is priced by material and size."
               if R and C else "Fashion metals have a fixed price per charm, set by material and size — any design."
               if C else "Fashion metals have a fixed price per ring — any size, any design.")
        Fixed = ("Prices in US dollars, fixed for the material — a ring’s size does not change its price; a charm is priced by size."
                 if R and C else "Prices in US dollars, per charm, fixed for the material and size."
                 if C else "Prices in US dollars, per ring, fixed for the material — the ring size does not change the price.")
        return {"materialsTitle": "Real metal, one fixed price per " + W["noun"],
                "materialsIntro": f"Every {W['noun']} is printed to order in the metal you choose. {How} Gold is quoted individually.",
                "materialsNote": f"{Fixed} Each {W['noun']} is made to order; the final weight is measured at production. "
                                 "Vermeil is sterling silver with a thick 14K gold plating.",
                "goldIntro": f"Solid gold {W['nouns']} are priced per piece. Design your {W['noun']}, choose a gold option in "
                             "Customize and send a quote request — a specialist replies within one business day with a price "
                             "and the next steps. Nothing is ordered or charged until you confirm."}
    if View == "technology":
        return {"'Fine detail at this scale is what the technology is made for — the same printers make every XJet Atelier ' + wording.noun + '.'":
                f"Fine detail at this scale is what the technology is made for — the same printers make every XJet Atelier {W['noun']}."}
    if View == "shipping-returns":
        return {"'Each ' + wording.noun + ' is printed to order in real metal.'": f"Each {W['noun']} is printed to order in real metal.",
                "'Every piece is made to your design and size, so it cannot be resold and is not returnable for change of mind. If your ' + wording.noun + ' arrives damaged, or does not match the design or the size you selected, contact us within 14 days of delivery and we will remake it or refund you.'":
                "Every piece is made to your design and size, so it cannot be resold and is not returnable for change of mind. "
                f"If your {W['noun']} arrives damaged, or does not match the design or the size you selected, contact us within "
                "14 days of delivery and we will remake it or refund you."}
    if View == "terms":
        Names = [Products.Labels[P].lower() for P in Visible]
        Idea = " or ".join("a " + N for N in Names) if Names else "a piece of jewelry"
        Supports = (" and ".join(Products.Plurals[P].lower() for P in Visible) if len(Visible) > 1 else
                    Products.Plurals[Visible[0]].lower() + " only" if Visible else "no products at the moment")
        Fixed = ("fixed per piece and material (a charm’s price also depends on its size)" if R and C else
                 "fixed per charm, material and size" if C else "fixed per ring and material")
        return {"termsService": f"XJet Atelier, operated by XJet Ltd., lets you describe or upload an idea for {Idea}, generates "
                                "four design options and a 360° preview with AI, and lets you configure the metal and size of the "
                                f"design you choose. The current release supports {Supports}.",
                "termsOrders": f"Prices are in US dollars and are {Fixed}. An order is a reservation until our team has reviewed "
                               "the design for production feasibility and confirmed it to you; payment is arranged after you "
                               f"place the order. Each {W['noun']} is made to order (design and size) and is not returnable for "
                               f"change of mind; your design is not exclusive. Gold {W['nouns']} are quoted individually."}
    if View == "contact":
        Ring = ("Not sure about your ring size? Use the size guide on the Customize screen (tap any row to pick a size) or "
                "send us your finger circumference in mm.")
        Charm = "A charm size is its total height in millimetres, including the loop — ask us if you are unsure which size suits you."
        return {"sizingHelp": " ".join(X for X in (Ring if R else "", Charm if C else "") if X)
                or "Ask us anything about sizes and a specialist will help."}
    return {}


def HeroCollection(Visible: tuple, Previewed: tuple = ()) -> dict:
    """The line under Start Designing (web/app.js heroCollection): the collections customers can design now."""
    On = [Products.Plurals[P] for P in Visible if P not in Previewed]
    if not On:
        return {"label": "", "text": ""}
    if len(On) == 1:
        return {"label": On[0], "text": " — the first XJet Atelier collection, made to order in real metal."}
    return {"label": ", ".join(On[:-1]) + " & " + On[-1], "text": " — XJet Atelier collections, made to order in real metal."}


# ── the server copy of the requested page ─────────────────────────────────────────────────────────────────────────────
def _Span(Html: str, Open: str, Start: int = 0) -> tuple[int, int]:
    """Where the template that opens with `Open` starts and where its own </template> ends."""
    I = Html.index(Open, Start)
    Depth = 0
    for M in _TemplateTag.finditer(Html, I):
        Depth += -1 if M.group(0) == "</template>" else 1
        if Depth == 0:
            return I, M.end()
    raise ValueError(f"unclosed template: {Open}")


def _Cloak(Html: str) -> str:
    """Before the scripts start, a part shown or hidden by state (x-show) stays hidden: it never flashes in its wrong state."""
    def One(M):
        Tag = M.group(0)
        if ' x-show="' not in Tag or " x-cloak" in Tag:
            return Tag
        Name = re.match(r"<[a-zA-Z][\w:-]*", Tag).group(0)
        return Name + " x-cloak" + Tag[len(Name):]
    return _OpenTag.sub(One, Html)


def _Fill(Html: str, Expr: str, Text: str) -> str:
    """The text an x-text expression shows, written into its element for readers that run no scripts."""
    return re.sub(r'(x-text="' + re.escape(Expr) + r'"[^<>]*>)[^<]*(</)',
                  lambda M: M.group(1) + html.escape(Text, quote=False) + M.group(2), Html)


def ServerView(Html: str, View: str, Fill: dict | None = None, Before: dict | None = None) -> str:
    """index.html with the page `View` as real markup: a live copy of its template (filled with this site's text, the
    x-show parts cloaked until the scripts start, crawler-only blocks inserted before an anchor) that removes itself when
    the visitor leaves the view; the template stays for later visits, kept from rendering twice (ssrView)."""
    Open = _LegalOpen if View in Legal else f"""<template x-if="view === '{View}'">"""
    S, End = _Span(Html, Open)
    Inner = Html[S + len(Open):End - len("</template>")]
    Copy = Inner
    if View in Legal:                     # the legal pages share one frame: this page's part is real markup, the others' go
        for Other in Legal:               # (the copy leaves as soon as the view changes, so they could never show in it)
            Open_ = f"""<template x-if="view === '{Other}'">"""
            IS, IE = _Span(Copy, Open_)
            Copy = Copy[:IS] + (Copy[IS + len(Open_):IE - len("</template>")] if Other == View else "") + Copy[IE:]
    Copy = _Cloak(Copy)
    for Expr, Text in (Fill or {}).items():
        Copy = _Fill(Copy, Expr, Text)
    for Anchor, Block in (Before or {}).items():
        if Anchor in Copy:
            Copy = Copy.replace(Anchor, Block + Anchor, 1)
    Live = (f"""<div class="ssr-view" data-ssr-view="{View}" x-effect="if (view !== '{View}') {{ ssrView = ''; $el.remove() }}">"""
            + Copy + "</div>")
    Kept = Open[:-2] + f""" && ssrView !== '{View}'">"""
    return Html[:S] + Live + Kept + Inner + "</template>" + Html[End:]


def DesignHeading(Html: str) -> str:
    """A gallery design's own page: the site's tagline becomes a second-level heading and the design's name in its dialog
    the H1 (the same classes, so nothing looks different)."""
    Hero, Out, I = '<h1 class="text-5xl md:text-6xl', [], 0
    while (J := Html.find(Hero, I)) != -1:
        K = Html.index("</h1>", J)
        Out.append(Html[I:J] + "<h2" + Html[J + 3:K] + "</h2>")
        I = K + len("</h1>")
    Html = "".join(Out) + Html[I:]
    return Html.replace('<h3 class="brand-font text-2xl text-zinc-900" x-text="galleryItem.title"></h3>',
                        '<h1 class="brand-font text-2xl text-zinc-900" x-text="galleryItem.title"></h1>')


def CrawlerBlock(Inner: str) -> str:
    """Markup for readers that run no scripts (the same content the page shows once its scripts start): hidden from the
    first paint and removed as the page starts."""
    return f'<div data-ssr-fallback x-cloak x-init="$el.remove()">{Inner}</div>'


def DesignLinks(Base: str, Items: list[dict]) -> str:
    """The gallery as plain links to each design's own page (crawlers find every design from the page)."""
    if not Items:
        return ""
    Li = "".join(f'<li><a href="{E(Base)}/design/{E(I["slug"])}">{E(I["title"])}</a> — '
                 f'{E(Products.Labels.get(I.get("product_type") or Products.Ring, "Ring"))}</li>' for I in Items)
    return CrawlerBlock(f"<ul>{Li}</ul>")


def MaterialList(Ctx) -> str:
    Fashion = "".join(f"<li>{E(L)} — available to order</li>" for L in _Labels(Ctx, "fashion"))
    Gold = "".join(f"<li>{E(L)} — by request (quoted individually)</li>" for L in _Labels(Ctx, "luxury"))
    return CrawlerBlock(f"<ul>{Fashion}{Gold}</ul>")


# ── structured data (JSON-LD): only what is true ─────────────────────────────────────────────────────────────────────
def JsonLd(*Objects: dict) -> str:
    return "".join('<script type="application/ld+json">'
                   + json.dumps(O, ensure_ascii=False).replace("</", "<\\/") + "</script>" for O in Objects)


def Organization(Home: str, SupportEmail: str = "") -> dict:
    Out = {"@context": "https://schema.org", "@type": "Organization", "@id": Home + "#organization", "name": "XJet Ltd.",
           "url": "https://www.xjet3d.com/", "brand": {"@type": "Brand", "name": "XJet Atelier"}}
    if SupportEmail:
        Out["contactPoint"] = {"@type": "ContactPoint", "contactType": "customer support", "email": SupportEmail}
    return Out


def WebSite(Home: str) -> dict:
    return {"@context": "https://schema.org", "@type": "WebSite", "@id": Home + "#website", "name": "XJet Atelier",
            "url": Home, "inLanguage": "en", "publisher": {"@id": Home + "#organization"}}


def FaqPage(Items: list[tuple[str, str]]) -> dict:
    return {"@context": "https://schema.org", "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": Q, "acceptedAnswer": {"@type": "Answer", "text": A}} for Q, A in Items]}


def DesignProduct(Title: str, Url: str, Image: str, Description: str, Product: str) -> dict:
    """A gallery design: its name, image, description and kind — no price, availability, rating or review."""
    return {"@context": "https://schema.org", "@type": "Product", "name": Title, "url": Url, "image": [Image],
            "description": Description, "category": Products.Plurals.get(Product, "Rings"),
            "brand": {"@type": "Brand", "name": "XJet Atelier"}}
