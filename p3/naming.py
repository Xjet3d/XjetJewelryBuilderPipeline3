"""Local, deterministic ring names — one or two short premium words from the design's own prompt:
"Aurora Twist", "Fil Wave", "Halo Bloom", "Amour Curve", "Luna Weave".

No external call of any kind (no LLM, no vision, no provider request, nothing paid): keyword rules
and a curated vocabulary only. Rules:
  * at most two words, no "The", no "Ring";
  * no material, karat or size words, no brand names;
  * unique across every design — when a name is taken, another local word combination is tried
    before any number is ever appended;
  * a refinement keeps its lineage: the family word of its master plus a new descriptor
    ("Fil Twist" → "Fil Lattice"); the Admin's manual Rename stays the final override.

Charms use the same rules and vocabulary (Product="charm"; rings are the default and are named exactly
as before), plus their own: a charm's name never says what it is or how it hangs — no "Charm",
"Pendant", "Chain", "Necklace", "Bail" or "Loop" — and the descriptors about how a ring sits on a
finger ("Open", "Cuff", "Midi" …) are never offered for a charm.
"""

import re

# ── vocabulary ───────────────────────────────────────────────────────────────
# Family word (first word): figurative motifs first, then style words; the first match wins.
Families = [
    (r"serpent|snake|python|anaconda|cobra|viper|ophid", "Serpent"),
    (r"dragon|drake|wyvern", "Drake"),
    (r"phoenix|firebird", "Phoenix"),
    (r"flame|\bfire\b|ember|blaz", "Ember"),
    (r"\bwaves?\b|ocean|\bsea\b|\btides?\b|aqua|marine|nautic", "Marée"),
    (r"\brose|floral|flower|petal|bloom|blossom|botanic", "Rose"),
    (r"\bleaf|leaves|\bvine|\bivy\b|laurel|olive|branch", "Laurel"),
    (r"\bstars?\b|stellar|celestial|cosmos|galaxy", "Nova"),
    (r"\bmoon|lunar|crescent", "Luna"),
    (r"\bsun\b|solar|sunburst|radian", "Solène"),
    (r"heart|amour", "Amour"),
    (r"skull|gothic|raven|\bnoir\b", "Noir"),
    (r"crown|regal|royal|\bking\b|\bqueen\b", "Régent"),
    (r"\bknot|infinity|eternal|forever", "Éternité"),
    (r"feather|\bwings?\b|angel|seraph", "Séraphine"),
    (r"butterfly|papillon", "Papillon"),
    (r"tiger|\blion\b|panther|leopard|jaguar", "Fauve"),
    (r"\bbees?\b|honey|\bhive\b", "Abeille"),
    (r"\bsignet\b", "Signet"),
    (r"\bsolitaire\b", "Solitaire"),
    (r"\bhalo\b", "Halo"),
    (r"\bwrap|\bcoil|articulat", "Coil"),
    (r"allong|elongat|lengthen|\blong\b|tapered", "Élan"),
    (r"minimal|delicate|dainty|\bthin|\bfine\b|\bslim|slender|simple|plain", "Fil"),
    (r"\bbold|chunky|statement|\bthick|heavy|massive", "Bold"),
    (r"diamond|brilliant|\bgems?\b|sapphire|emerald|\bruby\b|pav[eé]", "Éclat"),
]
# Style words describe a form, not a figure: they never stand alone and take shape descriptors only.
StyleFamilies = {"Signet", "Solitaire", "Halo", "Coil", "Élan", "Fil", "Bold", "Éclat"}
# When the prompt names no motif or style: a family chosen deterministically from the prompt.
FallbackFamilies = ["Lumière", "Solène", "Aurora", "Luna", "Solstice", "Aria", "Céleste", "Ondine", "Éclat", "Vesper",
                    "Orion", "Nova", "Maison", "Velvet", "Opale", "Astra"]

# Descriptor (second word): groups in priority order (distinctive forms first, generic adjectives last);
# each group lists alternatives, so a taken name gets another word before any number.
Descriptors = [
    (r"crossover|criss|\bcross(?:ed|ing)?\b|\bx\b", ["Cross", "Crossover", "Cruz"]),
    (r"lattice|\bmesh|\bgrid|\bnet\b|filigree|openwork|pierced|perforat", ["Lattice", "Mesh", "Filigree", "Grid"]),
    (r"weave|woven|braid|plait|interlac|intertwin|twine|entwin", ["Weave", "Braid", "Plait", "Twine"]),
    (r"twist|spiral|helix|\brope|torsad|\bcoil|\bwrap", ["Twist", "Spiral", "Helix", "Rope", "Coil"]),
    (r"\bknot|infinity|eternal|forever|\bloop", ["Knot", "Loop", "Infinity"]),
    (r"heart|amour|\blove\b", ["Heart", "Amour", "Love"]),
    (r"\bwaves?\b|\bwavy\b|ripple|undulat|ocean|\bsea\b|\btides?\b|swell", ["Wave", "Ripple", "Tide", "Swell"]),
    (r"floral|flower|petal|bloom|blossom|botanic|\brose|lotus|\blily|peony", ["Bloom", "Petal", "Flora", "Blossom"]),
    (r"\bleaf|leaves|\bvine|\bivy\b|laurel|olive|branch|\bfern", ["Leaf", "Vine", "Laurel", "Fern"]),
    (r"geometric|angular|hexagon|octagon|facet|polygon|prism|triang", ["Facet", "Prism", "Angle", "Edge"]),
    (r"\bscales?\b|scaly|\bskin\b", ["Scale", "Fang"]),
    (r"\bopen\b|\bgap\b|split|\bcuff|adjustable|bypass", ["Open", "Split", "Bypass", "Cuff"]),
    (r"\bchain|\blinks?\b", ["Link", "Chain", "Mesh"]),
    (r"\barch|\barcs?\b|bridge|\bspan", ["Arc", "Arch", "Span"]),
    (r"crown|royal|regal|tiara", ["Crown", "Regal", "Royal"]),
    (r"\bstars?\b|stellar|celestial|cosmos|galaxy|constellation", ["Star", "Astra", "Stellar"]),
    (r"\bmoon|lunar|crescent", ["Crescent", "Moon"]),
    (r"\bsun\b|solar|sunburst|radian|\brays?\b", ["Sol", "Ray", "Sunburst"]),
    (r"\bdrops?\b|\btear|\bpear\b", ["Drop", "Pear", "Tear"]),
    (r"square|cushion|rectang|\bbox\b", ["Square", "Cushion", "Block"]),
    (r"signet|\bseal\b|shield|crest|monogram", ["Signet", "Seal", "Crest"]),
    (r"stack|layer", ["Stack", "Layer", "Tier"]),
    (r"feather|\bwings?\b|angel|seraph|plume", ["Wing", "Plume", "Feather"]),
    (r"butterfly|papillon|\bmoth\b", ["Papillon", "Flutter"]),
    (r"flame|\bfire\b|ember|blaz|torch", ["Ember", "Flame", "Blaze"]),
    (r"\bhalo\b|\baura\b|\bglow|lumin|radiant|shine|sparkl", ["Halo", "Glow", "Aura", "Lumen"]),
    (r"\bdome|cabochon|bubble|bomb[eé]", ["Dome", "Bubble", "Orb"]),
    (r"textur|hammer|\bgrain|matte|brushed|etched|engrav|pattern|milgrain", ["Texture", "Grain", "Etch", "Relief"]),
    (r"\bstones?\b|\bgems?\b|diamond|sapphire|emerald|\bruby\b|crystal|pearl|brilliant|pav[eé]", ["Gem", "Stone", "Pavé", "Spark"]),
    (r"hollow|\bvoid\b|negative space", ["Hollow", "Void"]),
    (r"vintage|antique|art deco|\bdeco\b|retro|heirloom", ["Deco", "Heritage", "Vintage"]),
    (r"\btube|cylind|\bhoop", ["Hoop", "Round", "Orb"]),
    (r"\bwood|\bbark\b|coral|\bshell|\breef", ["Coral", "Bark", "Shell", "Reef"]),
    (r"\bmidi\b|knuckle|pinky", ["Midi", "Pinky"]),
    (r"curve|curvy|smooth|\bflow|organic|\bsoft\b|rounded|contour", ["Curve", "Flow", "Contour", "Arc"]),
    (r"modern|contemporary|futur|sleek|clean", ["Modern", "Sleek", "Edge", "Form"]),
    (r"classic|timeless|elegant|sophisticat|refined", ["Classic", "Grace", "Poise", "Noble"]),
    (r"minimal|delicate|dainty|\bthin|\bfine|\bslim|slender|simple|plain|subtle|\blight|narrow",
     ["Line", "Fine", "Trace", "Pure", "Whisper"]),
    (r"\bwide|\bwider|broad", ["Wide", "Broad"]),
    (r"\bbold|chunky|statement|\bthick|heav|\bbig|larg|massive|solid", ["Bold", "Mass", "Block", "Major"]),
]
ShapeFallbacks = ["Line", "Arc", "Form", "Curve", "Edge", "Flow", "Trace"]
MoodFallbacks = ["Aura", "Glow", "Muse", "Pure", "Calm", "Dawn", "Mist", "Note", "Echo", "Dusk"]
Roman = ["II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX"]

# Words a name must never contain: materials, karats, sizes, brands, and the banned framing words.
Forbidden = set((
    "the ring rings gold silver steel stainless platinum vermeil titanium brass copper bronze palladium rhodium "
    "karat carat 10k 14k 18k 24k 9k size sizes mm us uk eu variation variations variant version copy option design "
    "tiffany cartier bulgari bvlgari pandora swarovski chanel dior gucci hermes hermès cleef arpels graff winston "
    "boucheron chopard piaget mikimoto yurman messika rolex prada vuitton buccellati pomellato damiani chaumet "
    "repossi garrard asprey tacori verragio mejuri catbird vinader sabo zales jared kay nile xjet atelier").split())
_Stop = set((
    "the a an and or of for with to in on at by from into onto que la le "
    "create process design designed preserve keep maintain possible introduce make making add use give show render "
    "generate include avoid remove change turn refine using made ideal "
    "featuring inspired attached reference ref image images photo picture model models figure figures "
    "ring rings metal metallic luxury premium elegant style styled final copy edit new printable "
    "please your this that these those world cup detailed suitable precious original concept collection "
    "signet solitaire halo eternity band bands wrap coil tapered articulated sleek minimal delicate "
    "dainty bold chunky statement diamond brilliant gem sapphire emerald ruby "
    "front back top side center centre shape shaped motif head surface texture pattern pose "
    "modern classic contemporary high jewelry jewellery perfect timeless sophisticated intricate striking "
    "wearable comfortable unisex men women mens womens male female wedding engagement bridal anniversary gift "
    "thinner thicker smaller bigger larger wider").split()) | Forbidden

# Charms: their framing words are never design words. In a charm prompt they describe what the piece is and
# how it hangs ("a moon charm on a chain", "with a loop at the top"), so they are left out before matching.
CharmForbidden = set("charm charms pendant pendants chain chains necklace necklaces bail bails jump loop loops "
                     "bracelet bracelets".split())
_CharmFraming = re.compile(r"\b(?:charms?|pendants?|chains?|necklaces?|bails?|jump\s*rings?|loops?|bracelets?)\b", re.I)
# Descriptor groups about how a ring sits on the finger (named by their first word): never for a charm
RingOnlyDescriptors = {"Open", "Midi"}


def _Banned(Product: str) -> set[str]:
    return Forbidden | CharmForbidden if Product == "charm" else Forbidden


def _Framing(Text: str | None, Product: str) -> str | None:
    """A charm text without its framing words ("a moon charm on a chain" → "a moon"); a ring text as it is."""
    return _CharmFraming.sub(" ", Text) if Text and Product == "charm" else Text


def _Hash(Text: str) -> int:
    H = 0
    for Ch in (Text or "").strip().lower():
        H = (H * 31 + ord(Ch)) % 1_000_003
    return H


def _Rotate(Words: list[str], Seed: str) -> list[str]:
    if not Words:
        return []
    N = _Hash(Seed) % len(Words)
    return Words[N:] + Words[:N]


def _Clean(Word: str) -> str:
    return Word[0].upper() + Word[1:].lower() if Word else ""


def _Entity(Prompt: str) -> str:
    """A proper noun the prompt is built around ("inspired by Haaland"): a capitalised word that does not
    open a sentence — never a brand, a material or a jewellery term."""
    Hero = ""
    for M in re.finditer(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ']{2,}", Prompt or ""):
        Before = (Prompt or "")[:M.start()].rstrip()
        Tok = M.group(0)
        if not Before or Before[-1] in ".!?;:\n(\"'«" or not re.match(r"[A-ZÀ-Þ]", Tok) or Tok.lower() in _Stop:
            continue
        Hero = Tok
    return _Clean(Hero) if Hero else ""


def MatchedFamilies(Prompt: str) -> list[str]:
    """The families the prompt itself names: motif/style matches, then a proper noun."""
    Lower = (Prompt or "").lower()
    Out = [Word for Pattern, Word in Families if re.search(Pattern, Lower)]
    Entity = _Entity(Prompt)
    if Entity and Entity not in Out:
        Out.insert(1 if Out else 0, Entity)
    return Out


def FamilyWords(Prompt: str) -> list[str]:
    """Family candidates in priority order: the matched families, then the fallbacks."""
    Out = MatchedFamilies(Prompt)
    return Out + [F for F in _Rotate(FallbackFamilies, Prompt) if F not in Out]


def DescriptorWords(Prompt: str, Exclude=frozenset(), Product: str = "ring") -> list[str]:
    """Descriptor candidates from the prompt's keywords, best group first, alternatives after. Every
    group that contains an excluded word (the family word itself, or the master's own descriptor) is left
    out: no "Coil Twist", no "Bold Mass", and a variation of "Fil Twist" is never "Fil Spiral"."""
    Lower = (Prompt or "").lower()
    Ex = {E.lower() for E in Exclude}
    Out = []
    for Pattern, Words in Descriptors:
        if not re.search(Pattern, Lower) or any(W.lower() in Ex for W in Words):
            continue
        if Product == "charm" and Words[0] in RingOnlyDescriptors:
            continue
        Out += [W for W in Words if W not in Out]
    return Out


def _Ok(Name: str, Product: str = "ring") -> bool:
    Words, Banned = Name.split(), _Banned(Product)
    return 1 <= len(Words) <= 2 and not any(W.lower() in Banned for W in Words) and len({W.lower() for W in Words}) == len(Words)


def _Fallbacks(Family: str, Seed: str) -> list[str]:
    """Neutral descriptors when the prompt offers none: style words take shapes in a fixed order
    ("Bold Line", "Fil Arc"); figurative words vary with the prompt ("Luna Dawn", "Serpent Mist")."""
    if Family in StyleFamilies:
        return list(ShapeFallbacks)
    return _Rotate(ShapeFallbacks + MoodFallbacks, Seed)


def Candidates(Prompt: str, Lineage: str | None = None, Instruction: str | None = None, Product: str = "ring"):
    """Every name to try, best first. Lineage = the master's name for a refinement: its family word is
    kept and a descriptor comes from the refinement instruction, then from the master's prompt."""
    Prompt = _Framing(Prompt or "", Product)
    Instruction = _Framing(Instruction, Product)
    Seen = set()

    def Emit(Name):
        if Name.lower() not in Seen and _Ok(Name, Product):
            Seen.add(Name.lower())
            yield Name

    if Lineage:
        Words = [W for W in Lineage.split() if W.lower() not in _Banned(Product)]
        Family = _Clean(Words[0]) if Words else FamilyWords(Prompt)[0]
        Own = {Family} | set(Words[1:])
        Descs = DescriptorWords(Instruction or "", Own, Product)
        Descs += [D for D in DescriptorWords(Prompt, Own, Product) if D not in Descs]
        for D in Descs:
            yield from Emit(f"{Family} {D}")
        for D in _Fallbacks(Family, Instruction or Prompt):
            yield from Emit(f"{Family} {D}")
        for N in Roman:                                  # the very last resort: a number
            yield from Emit(f"{Family} {N}")
        return

    Fams = FamilyWords(Prompt)
    Strong = [F for F in MatchedFamilies(Prompt) if F not in StyleFamilies]   # figures that stand alone
    Descs = DescriptorWords(Prompt, {Fams[0]}, Product)
    Primary = Fams[:3]
    for F in Primary:                                    # keyword names: "Fil Twist", "Fil Spiral" …
        for D in Descs:
            yield from Emit(f"{F} {D}")
    for F in Primary:                                    # a single strong word: "Serpent", "Haaland"
        if F in Strong:
            yield from Emit(F)
    for F in Primary:                                    # neutral descriptors: "Bold Line", "Luna Dawn"
        for D in _Fallbacks(F, Prompt):
            yield from Emit(f"{F} {D}")
    for F in Fams[3:]:                                   # other families, same order
        for D in Descs:
            yield from Emit(f"{F} {D}")
        if F in Strong:
            yield from Emit(F)
        for D in _Fallbacks(F, Prompt):
            yield from Emit(f"{F} {D}")
    for N in Roman:                                      # the very last resort: a number
        yield from Emit(f"{Fams[0]} {N}")


def RingName(Prompt: str, Taken=frozenset(), Lineage: str | None = None, Instruction: str | None = None,
             Product: str = "ring") -> str:
    """The first candidate no other design carries (case-insensitive) — of a ring, or of a charm."""
    TakenLower = {T.lower() for T in Taken}
    for Name in Candidates(Prompt, Lineage, Instruction, Product):
        if Name.lower() not in TakenLower:
            return Name
    return f"{FamilyWords(_Framing(Prompt, Product))[0]} {len(TakenLower) + 1}"


def Suggestions(Prompt: str, Taken=frozenset(), Lineage: str | None = None, Instruction: str | None = None, N: int = 6,
                Product: str = "ring") -> list[str]:
    """A handful of free names for the Admin to pick from (the Rename form); never a numbered one."""
    TakenLower = {T.lower() for T in Taken}
    Out = []
    for Name in Candidates(Prompt, Lineage, Instruction, Product):
        if Name.lower() not in TakenLower and Name.split()[-1] not in Roman:
            Out.append(Name)
        if len(Out) >= N:
            break
    return Out


def FamilyOf(Title: str, Product: str = "ring") -> str:
    Words = [W for W in (Title or "").split() if W.lower() not in _Banned(Product)]
    return Words[0] if Words else ""


# ── database-aware helpers ──────────────────────────────────────────────────
def TakenTitles(Db, Except: str | None = None) -> set[str]:
    return {R["title"].lower() for R in Db.All("SELECT title FROM designs WHERE id IS NOT ?", (Except,))}


def NameForPrompt(Db, Prompt: str, Product: str = "ring") -> str:
    return RingName(Prompt, TakenTitles(Db), Product=Product)


def NameForVariation(Db, MasterTitle: str, Instruction: str, MasterPrompt: str, Product: str = "ring") -> str:
    return RingName(MasterPrompt, TakenTitles(Db), Lineage=MasterTitle, Instruction=Instruction, Product=Product)


def FollowRename(Db, VariationId: str, VariationTitle_: str, NewMasterTitle: str, Instruction: str, MasterPrompt: str,
                 Product: str = "ring") -> str:
    """A variation follows its renamed master: "Fil Lattice" under a master renamed "Aurora Twist"
    becomes "Aurora Lattice" when that is free, otherwise the next lineage name."""
    Taken = TakenTitles(Db, Except=VariationId)
    Family = FamilyOf(NewMasterTitle, Product)
    Rest = [W for W in VariationTitle_.split() if W.lower() not in _Banned(Product)][1:]
    Kept = " ".join([Family] + Rest[:1])
    if Rest and Rest[0] not in Roman and _Ok(Kept, Product) and Kept.lower() not in Taken:
        return Kept
    return RingName(MasterPrompt, Taken, Lineage=NewMasterTitle, Instruction=Instruction, Product=Product)


def UniqueTitle(Db, Base: str) -> str:
    """A given name made unique with a number — only for explicit names that clash (never automatic ones)."""
    Base = " ".join((Base or "Aurora").split())
    Taken = TakenTitles(Db)
    if Base.lower() not in Taken:
        return Base
    for N in Roman:
        if f"{Base} {N}".lower() not in Taken:
            return f"{Base} {N}"
    return f"{Base} {len(Taken) + 1}"


def ProductName(Prompt: str, Product: str = "ring") -> str:
    """The first-choice name for a prompt (uniqueness is applied by NameForPrompt when a design is saved)."""
    return RingName(Prompt, Product=Product)
