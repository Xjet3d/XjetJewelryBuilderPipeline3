"""AI model prompts & parameters — the single source of truth for every provider request.

Each model has a parameter spec (from the fal.ai OpenAPI schema, verified 2026-10-01) and an
immutable version history in pipeline3.db (model_config_versions); exactly one version per model is
active (model_config_active). Every new pipeline request is built from the active version, and the
version id is recorded on the batch / movie / mesh, so requests already created keep the settings
they were created with — even if they are submitted after a newer version is activated.

A stored configuration contains only parameters that were explicitly configured. Anything omitted
is not sent and the provider default applies. Runtime inputs (the customer's prompt or refinement
instruction, the selected image, per-image seeds) are never stored in a configuration: the pipeline
supplies them when it builds each request, through the documented placeholders below.

On first start (no saved versions), version 1 of each model is seeded from the files the pipeline
used before this existed (config/generation.json + config/prompts/*), so current prompts and
settings are preserved exactly. After that those files are not read for model parameters.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

from p3.config import _ReadText
from p3.db import Database, Dumps, Now
from p3.providers import endpoints
from p3.settings import ConfigDir

Placeholder = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

# ── runtime inputs the pipeline supplies ─────────────────────────────────────
RuntimeInputs = {
    "user_text": "The customer's text for this request: the New Design prompt, or the refinement instruction.",
    "design_prompt": "The original New Design prompt of the session (also available during refinements).",
    "user_prompt": "The customer's prompt (any-llm).",
}

CharmSampleText = "A small crescent moon charm with a tiny star in its curve"
SampleRuntime = {
    "user_text": "[SAMPLE customer text] A slim rose-gold band with a small leaf motif",
    "design_prompt": "[SAMPLE design prompt] A slim rose-gold band with a small leaf motif",
    "user_prompt": "[SAMPLE customer prompt] A slim rose-gold band with a small leaf motif",
    "image_url": "https://example.invalid/SAMPLE-selected-image.png",
    "seed": 123456789,
    "slot": 0,                     # the preview shows image A of a refinement (its variation directive, if configured)
}


@dataclass(frozen=True)
class Param:
    Name: str
    Kind: str                      # text | template | enum | int | number | bool | keyframes
    Description: str
    Default: object = None         # provider default (None = provider decides / no default)
    Enum: tuple = ()
    Min: float | None = None
    Max: float | None = None
    Required: bool = False         # the provider requires it (must be configured)
    Placeholders: tuple = ()       # template: allowed placeholders
    RequiredPlaceholders: tuple = ()
    MaxLength: int | None = None
    Allowed: tuple = ()            # subset of Enum the pipeline supports (empty = all)
    AllowedReason: str = ""
    Internal: bool = False         # used by the pipeline when it builds the request; never sent as a provider field
    Group: str = ""                # heading shown above this parameter in the Admin

    def ToJson(self) -> dict:
        return {"name": self.Name, "kind": self.Kind, "description": self.Description, "default": self.Default,
                "enum": list(self.Enum), "allowed": list(self.Allowed or self.Enum), "allowed_reason": self.AllowedReason,
                "min": self.Min, "max": self.Max, "required": self.Required,
                "placeholders": list(self.Placeholders), "required_placeholders": list(self.RequiredPlaceholders),
                "max_length": self.MaxLength, "internal": self.Internal, "group": self.Group}


@dataclass(frozen=True)
class Fixed:
    """A request field the pipeline controls (runtime input or a fixed technical value)."""
    Name: str
    Description: str
    Value: object = None           # fixed value sent; None = supplied at runtime
    Runtime: str | None = None     # runtime input key, when supplied per request


@dataclass(frozen=True)
class ModelSpec:
    Id: str
    Label: str
    Endpoint: str
    UsedFor: str
    Connected: bool
    Params: tuple
    Fixed: tuple = ()
    Notes: tuple = ()
    Product: str = "ring"          # the product this configuration is for: 'ring' or 'charm' (never both)

    def Param(self, Name: str) -> Param | None:
        return next((P for P in self.Params if P.Name == Name), None)

    def ToJson(self) -> dict:
        return {"id": self.Id, "label": self.Label, "endpoint": self.Endpoint, "used_for": self.UsedFor,
                "connected": self.Connected, "params": [P.ToJson() for P in self.Params],
                "fixed": [{"name": F.Name, "description": F.Description, "value": F.Value, "runtime": F.Runtime}
                          for F in self.Fixed], "notes": list(self.Notes), "product": self.Product}


AspectRatios = ("auto", "21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16")


def _ImageParams(AspectDefault: str) -> tuple:
    return (
        Param("prompt", "template", "Prompt template sent for every image. {{user_text}} is replaced by the customer's "
              "prompt or refinement instruction.", Required=True, MaxLength=50000,
              Placeholders=("user_text", "design_prompt"), RequiredPlaceholders=("user_text",)),
        Param("system_prompt", "text", "System instruction that steers the model's style for the whole request. "
              "Omit (or leave blank) to send none.", Default="", MaxLength=50000),
        Param("aspect_ratio", "enum", "Aspect ratio of the generated image.", Default=AspectDefault, Enum=AspectRatios),
        Param("resolution", "enum", "Resolution of the generated image.", Default="1K", Enum=("1K", "2K", "4K")),
        Param("output_format", "enum", "File format of the generated image.", Default="png", Enum=("jpeg", "png", "webp")),
        Param("safety_tolerance", "enum", "Content moderation level: 1 is the most strict, 6 the least strict.",
              Default="4", Enum=("1", "2", "3", "4", "5", "6")),
        Param("enable_web_search", "bool", "Let the model use web search for the latest information.", Default=False),
        Param("limit_generations", "bool", "Limit each round of prompting to one generation and ignore image-count "
              "instructions in the prompt (experimental).", Default=True),
    )


# ── refinement variations: the same instruction for all four images, plus one directive per image ──
# A refinement sends four requests with the same instruction and reference image; the edit model
# honours seeds only weakly and is told to preserve everything else, so the four results often come
# back as the same picture. Each image therefore gets its own directive, appended to the end of its
# prompt (A–D by slot). A blank directive = no addition for that image, exactly as before. Pipeline-
# only text: never a provider field, no extra request, no extra cost.
VariationGroup = ("Refinement variations — the same instruction for all four images, plus one directive per image "
                  "(added to the end of that image's prompt; refinements only, not New Designs from an uploaded photo). "
                  "Blank = that image gets the plain prompt, as before.")
# The four are deliberately far apart (no gentle gradient): A stays faithful, B pushes the change to an
# extreme, C rebuilds the proportions and placement, D is free to be a different ring in the same family.
# B–D say explicitly that they override the "preserve everything" instruction above where the two conflict.
_Override = ("Where this conflicts with any instruction above to preserve the design, this directive wins. ")
VariationDefaults = {
    "a": "Image A of four — the faithful version. Apply the requested change exactly and nothing else: every other "
         "detail, proportion, finish and the metal stay identical to the reference image.",
    "b": "Image B of four — the extreme version. " + _Override +
         "Push the requested change as far as it can go: at least three times stronger than a minimal edit, so that it "
         "dominates the design and is obvious at first glance. Other parts of the ring may change where the extreme "
         "version demands it; the ring stays wearable and the metal stays the same.",
    "c": "Image C of four — the reinterpretation. " + _Override +
         "Rebuild the ring around the requested change with clearly different proportions and placement: move it, "
         "scale it up or down dramatically, repeat it, wrap it around the band or let it take over the whole shank, so "
         "that the silhouette itself changes. Keep only the metal and the fact that it is the same type of ring.",
    "d": "Image D of four — the free variant. " + _Override +
         "Treat the requested change as the brief for a new design in the same family: choose a different structure, "
         "finish, texture and detailing that express the change boldly. It may look completely different from the "
         "reference image; only the metal must stay the same.",
}
# Earlier default texts: an installation that still runs one of these sets, unedited, is moved to the current
# defaults on start (a new visible version); edited texts are never touched.
PreviousVariationDefaults = [{
    "a": "This is image A of four: the most faithful, conservative version. Apply exactly the requested change and "
         "nothing more; keep every other detail, proportion, finish and the metal identical to the reference image.",
    "b": "This is image B of four: a bolder version of the requested change. Make the change clearly more pronounced "
         "than a minimal edit, while every other feature, proportion and the metal of the design stay exactly as in "
         "the reference image.",
    "c": "This is image C of four: an alternative interpretation of the requested change in proportions or placement. "
         "Realise the same change with different proportions or at a different position on the ring, keeping "
         "everything else identical to the reference image.",
    "d": "This is image D of four: the requested change with a different finish or detail treatment (surface texture, "
         "polish, edge or ornament detail), keeping the form and every other feature of the design identical to the "
         "reference image.",
}]
VariationLabels = {"a": "Image A — the faithful version: exactly the requested change, nothing else.",
                   "b": "Image B — the extreme version: the change pushed as far as it can go.",
                   "c": "Image C — the reinterpretation: different proportions and placement, the silhouette may change.",
                   "d": "Image D — the free variant: a different ring in the same family, may be completely different."}
_VariationParams = tuple(
    Param(f"variation_{K}", "text", VariationLabels[K] + " Appended to the end of this image's prompt in a refinement. "
          "Blank = no directive for this image (same prompt as before).", Default=VariationDefaults[K], MaxLength=2000,
          Internal=True, Group=VariationGroup)
    for K in "abcd")

# The same four directives for a charm, in charm words (the plain loop at the top always stays).
CharmVariationDefaults = {
    "a": "Image A of four — the faithful version. Apply the requested change exactly and nothing else: every other "
         "detail, proportion, finish, the metal and the plain loop at the top stay identical to the reference image.",
    "b": "Image B of four — the extreme version. " + _Override +
         "Push the requested change as far as it can go: at least three times stronger than a minimal edit, so that it "
         "dominates the design and is obvious at first glance. Other parts of the charm may change where the extreme "
         "version demands it; the charm stays wearable, the metal stays the same and the plain loop stays at the top center.",
    "c": "Image C of four — the reinterpretation. " + _Override +
         "Rebuild the charm around the requested change with clearly different proportions and placement: move it, "
         "scale it up or down dramatically, repeat it, or let it take over the whole charm body, so that the silhouette "
         "itself changes. Keep only the metal, the plain loop at the top center and the fact that it is a charm.",
    "d": "Image D of four — the free variant. " + _Override +
         "Treat the requested change as the brief for a new charm in the same family: choose a different shape, "
         "finish, texture and detailing that express the change boldly. It may look completely different from the "
         "reference image; only the metal and the plain loop at the top center must stay the same.",
}
CharmVariationLabels = {**VariationLabels, "d": "Image D — the free variant: a different charm in the same family, may be completely different."}
_CharmVariationParams = tuple(
    Param(f"variation_{K}", "text", CharmVariationLabels[K] + " Appended to the end of this image's prompt in a refinement. "
          "Blank = no directive for this image (same prompt as before).", Default=CharmVariationDefaults[K], MaxLength=2000,
          Internal=True, Group=VariationGroup)
    for K in "abcd")


def SlotDirective(ModelId: str, Params: dict, Slot) -> str | None:
    """The configured variation directive for image `Slot` (0–3 → A–D) of this model, or None."""
    Spec = Models.get(ModelId)
    if Spec is None or Slot is None or not 0 <= int(Slot) <= 3:
        return None
    P = Spec.Param(f"variation_{'abcd'[int(Slot)]}")
    Value = Params.get(P.Name) if P and P.Internal else None
    return Value.strip() if isinstance(Value, str) and Value.strip() else None


def WithDirective(Prompt: str, Directive: str | None) -> str:
    return (Prompt or "").rstrip() + "\n\n" + Directive.strip() if Directive else Prompt


_ImageFixed = (
    Fixed("num_images", "Always 1: the pipeline sends four separate requests per batch, one image each.", 1),
    Fixed("seed", "A different random seed for each of the four images, chosen when the batch is created.",
          Runtime="seed"),
    Fixed("sync_mode", "Not sent (provider default false): the pipeline downloads the result from its URL."),
)

# Movie and 3D parameters: one definition, shared by the ring and the charm model of each
_MovieParams = (
    Param("prompt", "text", "Video prompt. When omitted or blank the provider uses its frozen-scene, "
          "camera-only default prompt.", Default="The same elements in the reference are rigid. Preserve every "
          "element exactly. The entire scene is frozen. Only the camera moves. no scene motion only camera motion",
          MaxLength=50000),
    Param("prompt_expansion_mode", "enum", "How much the provider rewrites the prompt first: 'disabled' skips it, "
          "'balanced' takes about a second, 'quality' up to ~30 s.", Default="balanced",
          Enum=("disabled", "balanced", "quality"), Required=True),
    Param("duration", "int", "Video length in seconds.", Default=5, Min=3, Max=15),
    Param("resolution", "enum", "Native generation resolution, or 1080P refinement from a native 768P source.",
          Default="480P", Enum=("480P", "768P", "1080P")),
    Param("camera_trajectory", "keyframes", "Ordered camera keyframes (2–12). time 0–1 is the normalized "
          "point in the clip, azimuth the horizontal angle (degrees, may exceed 360 for full turns), elevation "
          "−90…90°, distance > 0 in scene units. The first pose is held before its time and the last pose to "
          "the end; at most 32 turns of total azimuth travel.", Min=2, Max=12),
    Param("seed", "int", "Random seed. Omit for a random seed per movie.", Min=0),
    Param("enable_safety_checker", "bool", "Run the provider's safety checker.", Default=True),
)
_MovieFixed = (Fixed("image_url", "The customer's selected final image (first frame).", Runtime="image_url"),
               Fixed("sync_mode", "Not sent (provider default false): the pipeline downloads the video from its URL."))
_MeshParams = (
    Param("resolution", "enum", "2048quality is faster; 2048master gives the highest quality.",
          Default="2048quality", Enum=("2048quality", "2048master")),
    Param("face_count", "int", "Target face count (partner recommends 2,000,000 for 2048quality and "
          "5,000,000 for 2048master). Omit to let the provider decide.", Min=100_000, Max=5_000_000),
    Param("export_format", "enum", "File format of the model.", Default="glb",
          Enum=("glb", "obj", "stl", "fbx", "usdz"), Allowed=("glb", "obj", "stl"),
          AllowedReason="The geometry measurement (volume, inner diameter) can read GLB, OBJ and STL only."),
    Param("enable_texture", "bool", "Generate textures as well as geometry (billed).", Default=True),
    Param("enable_pbr", "bool", "Generate PBR material maps with the texture (billed).", Default=True),
    Param("shading", "number", "De-shading strength.", Default=0.5, Min=0, Max=1),
    Param("enable_safety_checker", "bool", "Check the input image for safety first.", Default=True),
)
_MeshFixed = (Fixed("image_url", "The session's selected design image.", Runtime="image_url"),
              Fixed("model", "Fixed by the endpoint (Hi3D v3.0); not sent.", None))

Models: dict[str, ModelSpec] = {S.Id: S for S in (
    ModelSpec(
        "any-llm", "any-llm", "fal-ai/any-llm",
        "Not used by the Pipeline 3 flow yet (Pipeline 2 uses it as its prompt gate). Settings can be prepared and "
        "previewed here; no P3 request uses them until a feature is connected.", False,
        (
            Param("model", "enum", "Language model to use. Premium models are charged at 10× the standard rate.",
                  Default="google/gemini-2.5-flash-lite", Enum=(
                      "deepseek/deepseek-r1", "deepseek/deepseek-v3.1-terminus", "anthropic/claude-sonnet-4.5",
                      "anthropic/claude-haiku-4.5", "anthropic/claude-3.7-sonnet", "anthropic/claude-3.5-sonnet",
                      "anthropic/claude-3-5-haiku", "anthropic/claude-3-haiku", "google/gemini-pro-1.5",
                      "google/gemini-flash-1.5", "google/gemini-flash-1.5-8b", "google/gemini-2.0-flash-001",
                      "google/gemini-2.5-flash", "google/gemini-2.5-flash-lite", "google/gemini-2.5-pro",
                      "meta-llama/llama-3.2-1b-instruct", "meta-llama/llama-3.2-3b-instruct",
                      "meta-llama/llama-3.1-8b-instruct", "meta-llama/llama-3.1-70b-instruct", "openai/gpt-oss-120b",
                      "openai/gpt-4o-mini", "openai/gpt-4o", "openai/gpt-4.1", "openai/o3", "openai/gpt-5-chat",
                      "openai/gpt-5-mini", "openai/gpt-5-nano", "meta-llama/llama-4-maverick",
                      "meta-llama/llama-4-scout", "moonshotai/kimi-k2.5")),
            Param("prompt", "template", "Prompt template. {{user_prompt}} is replaced by the customer's prompt.",
                  Required=True, Placeholders=("user_prompt",), RequiredPlaceholders=("user_prompt",)),
            Param("system_prompt", "text", "System prompt giving the model context or instructions."),
            Param("temperature", "number", "Variety of the responses: 0 is fully predictable, higher is more diverse.",
                  Min=0, Max=2),
            Param("max_tokens", "int", "Upper limit for the number of tokens generated.", Min=1),
            Param("priority", "enum", "Throughput (recommended for most uses) or low latency.", Default="latency",
                  Enum=("throughput", "latency")),
            Param("reasoning", "bool", "Include the model's reasoning in the final answer.", Default=False),
        )),
    ModelSpec(
        "nano-banana-pro", "fal-ai/nano-banana-pro", endpoints.ImageGenerate,
        "Design images for a New Design without a reference image (four separate requests, one image each).", True,
        _ImageParams("1:1"), _ImageFixed),
    ModelSpec(
        "nano-banana-pro-edit", "fal-ai/nano-banana-pro/edit", endpoints.ImageEdit,
        "Refinements (from the selected image) and New Designs with an uploaded reference image (four separate "
        "requests, one image each).", True,
        _ImageParams("auto") + _VariationParams,
        _ImageFixed + (Fixed("image_urls", "The image being edited: the selected design image for a refinement, or the "
                                           "customer's uploaded reference image.", Runtime="image_url"),)),
    ModelSpec(
        "minimax-camera", "minimax/h3-max/camera-controls", endpoints.Movie,
        "The 360° movie in Customize, made from the customer's selected final image.", True,
        _MovieParams,
        _MovieFixed),
    ModelSpec(
        "hi3d", "hitem3d/hi3d/v3.0/image-to-3d", endpoints.Mesh,
        "3D model generation — only when an admin starts Generate 3D (Sessions) or uses the developer mesh tool.", True,
        _MeshParams,
        _MeshFixed),
    # ── Charms: their own models, versions and history — the same providers, never the ring configuration ──
    ModelSpec(
        "nano-banana-pro-charm", "fal-ai/nano-banana-pro · Charm", endpoints.ImageGenerate,
        "Charm design images for a New Design without a reference image (four separate requests, one image each).", True,
        _ImageParams("1:1"), _ImageFixed, Product="charm"),
    ModelSpec(
        "nano-banana-pro-edit-charm", "fal-ai/nano-banana-pro/edit · Charm", endpoints.ImageEdit,
        "Charm refinements (from the selected image) and new Charm designs from an uploaded reference image (four "
        "separate requests, one image each).", True,
        _ImageParams("auto") + _CharmVariationParams,
        _ImageFixed + (Fixed("image_urls", "The image being edited: the selected charm image for a refinement, or the "
                                           "customer's uploaded reference image.", Runtime="image_url"),), Product="charm"),
    ModelSpec(
        "minimax-camera-charm", "minimax/h3-max/camera-controls · Charm", endpoints.Movie,
        "The 360° movie of a charm in Customize, made from the customer's selected final image.", True,
        _MovieParams, _MovieFixed, Product="charm"),
    ModelSpec(
        "hi3d-charm", "hitem3d/hi3d/v3.0/image-to-3d · Charm", endpoints.Mesh,
        "3D model generation for a charm — only when an admin starts Generate 3D (Sessions).", True,
        _MeshParams, _MeshFixed, Product="charm"),
)}

# Ring models by endpoint — exactly the mapping of before (callers that know no product are ring callers)
ByEndpoint = {S.Endpoint: S.Id for S in Models.values() if S.Product == "ring"}
# Every model by (endpoint, product)
ModelFor = {(S.Endpoint, S.Product): S.Id for S in Models.values()}


def ModelIdFor(Endpoint: str, Product: str = "ring") -> str:
    return ModelFor[(Endpoint, Product or "ring")]


def ProductOf(ModelId: str) -> str:
    return Models[ModelId].Product


class ConfigError(ValueError):
    def __init__(self, Problems: list[str]):
        super().__init__("; ".join(Problems))
        self.Problems = Problems


# ── validation ───────────────────────────────────────────────────────────────
def _IsNumber(V) -> bool:
    return isinstance(V, (int, float)) and not isinstance(V, bool) and math.isfinite(V)


def Validate(ModelId: str, Params: dict) -> dict:
    """Return the cleaned configuration or raise ConfigError with every problem found."""
    Spec = Models.get(ModelId)
    if Spec is None:
        raise ConfigError([f"Unknown model: {ModelId}"])
    if not isinstance(Params, dict):
        raise ConfigError(["Parameters must be an object."])
    Problems, Clean = [], {}
    FixedNames = {F.Name for F in Spec.Fixed}
    for Name, Value in Params.items():
        if Name in FixedNames:
            Problems.append(f"'{Name}' is set by the pipeline and cannot be configured.")
            continue
        P = Spec.Param(Name)
        if P is None:
            Problems.append(f"'{Name}' is not a supported parameter of {Spec.Label}.")
            continue
        if Value is None:
            continue                                   # explicit "omit"
        Label = f"'{Name}'"
        if P.Kind in ("text", "template"):
            if not isinstance(Value, str):
                Problems.append(f"{Label} must be text.")
                continue
            if P.MaxLength and len(Value) > P.MaxLength:
                Problems.append(f"{Label} is longer than {P.MaxLength} characters.")
            if P.Kind == "template" or Placeholder.search(Value):
                Used = set(Placeholder.findall(Value))
                Unknown = Used - set(P.Placeholders)
                if Unknown:
                    Problems.append(f"{Label} uses unknown placeholder(s): " + ", ".join("{{" + U + "}}" for U in sorted(Unknown))
                                    + (". Allowed: " + ", ".join("{{" + A + "}}" for A in P.Placeholders) if P.Placeholders
                                       else ". This field has no placeholders."))
                Missing = set(P.RequiredPlaceholders) - Used
                if Missing:
                    Problems.append(f"{Label} must contain " + ", ".join("{{" + M + "}}" for M in sorted(Missing))
                                    + " so the customer's input is sent.")
            if P.Kind == "text" and not Value.strip():
                continue                               # blank text = omit
            Clean[Name] = Value
        elif P.Kind == "enum":
            if Value not in P.Enum:
                Problems.append(f"{Label} must be one of: {', '.join(map(str, P.Enum))}.")
            elif P.Allowed and Value not in P.Allowed:
                Problems.append(f"{Label} '{Value}' is not supported by the pipeline. {P.AllowedReason}")
            else:
                Clean[Name] = Value
        elif P.Kind == "bool":
            if not isinstance(Value, bool):
                Problems.append(f"{Label} must be true or false.")
            else:
                Clean[Name] = Value
        elif P.Kind in ("int", "number"):
            if not _IsNumber(Value) or (P.Kind == "int" and float(Value) != int(Value)):
                Problems.append(f"{Label} must be {'a whole number' if P.Kind == 'int' else 'a number'}.")
                continue
            if P.Min is not None and Value < P.Min or P.Max is not None and Value > P.Max:
                Problems.append(f"{Label} must be between {P.Min if P.Min is not None else '−∞'} and "
                                f"{P.Max if P.Max is not None else '∞'}.")
                continue
            Clean[Name] = int(Value) if P.Kind == "int" else float(Value)
        elif P.Kind == "keyframes":
            Clean[Name] = _ValidateKeyframes(Value, Problems)
    for P in Spec.Params:
        if P.Required and P.Name not in Clean:
            Problems.append(f"'{P.Name}' is required by {Spec.Label}.")
    if Problems:
        raise ConfigError(Problems)
    return Clean


def _ValidateKeyframes(Value, Problems: list) -> list:
    if not isinstance(Value, list) or not 2 <= len(Value) <= 12:
        Problems.append("'camera_trajectory' needs between 2 and 12 keyframes.")
        return []
    Out, Prev = [], -1.0
    for I, K in enumerate(Value, 1):
        if not isinstance(K, dict) or set(K) - {"time", "azimuth", "elevation", "distance"}:
            Problems.append(f"Keyframe {I}: only time, azimuth, elevation and distance are allowed.")
            continue
        Missing = [F for F in ("time", "azimuth", "elevation", "distance") if not _IsNumber(K.get(F))]
        if Missing:
            Problems.append(f"Keyframe {I}: {', '.join(Missing)} must be a number.")
            continue
        if not 0 <= K["time"] <= 1:
            Problems.append(f"Keyframe {I}: time must be between 0 and 1.")
        if K["time"] < Prev:
            Problems.append(f"Keyframe {I}: keyframes must be in time order.")
        if not -90 <= K["elevation"] <= 90:
            Problems.append(f"Keyframe {I}: elevation must be between −90 and 90 degrees.")
        if K["distance"] <= 0:
            Problems.append(f"Keyframe {I}: distance must be greater than 0.")
        Prev = K["time"]
        Out.append({F: float(K[F]) for F in ("time", "azimuth", "elevation", "distance")})
    Travel = sum(abs(B["azimuth"] - A["azimuth"]) for A, B in zip(Out, Out[1:]))
    if Travel > 32 * 360:
        Problems.append("'camera_trajectory' exceeds 32 turns of total azimuth travel.")
    return Out


def Render(Template: str, Runtime: dict) -> str:
    return Placeholder.sub(lambda M: str(Runtime.get(M.group(1), "")), Template)


# ── request building (used by the pipeline and by the preview) ──────────────────
def BuildRequest(ModelId: str, Params: dict, Runtime: dict) -> dict:
    """The exact provider arguments for one request: configured params (templates rendered with
    the runtime inputs), plus the fields the pipeline controls."""
    Spec = Models[ModelId]
    Args = {}
    for Name, Value in Params.items():
        P = Spec.Param(Name)
        if P and P.Internal:
            continue                                   # pipeline-only (e.g. a variation directive), never a provider field
        Args[Name] = Render(Value, Runtime) if P and P.Kind in ("template", "text") and isinstance(Value, str) else Value
    Directive = SlotDirective(ModelId, Params, Runtime.get("slot"))
    if Directive and isinstance(Args.get("prompt"), str):
        Args["prompt"] = WithDirective(Args["prompt"], Directive)
    for F in Spec.Fixed:
        if F.Runtime == "image_url":
            Url = Runtime.get("image_url")
            if Url:
                Args[F.Name] = [Url] if F.Name == "image_urls" else Url
        elif F.Runtime == "seed":
            if Runtime.get("seed") is not None:
                Args["seed"] = Runtime["seed"]
        elif F.Value is not None:
            Args[F.Name] = F.Value
    return Args


# ── seed from the pre-existing files ───────────────────────────────────────────
def LegacyAliases() -> dict[str, str]:
    """The content-hash config versions recorded on batches/movies/meshes before versioned configs
    (computed exactly as p3/config.py did), so existing movies stay reusable under version 1."""
    from p3.config import ContentVersion
    Raw = json.loads((ConfigDir / "generation.json").read_text(encoding="utf-8"))
    Img = Raw["images"]
    Gen, Edit_, Suffix = (_ReadText(Img[K]) for K in ("generate_system_prompt_file", "edit_system_prompt_file",
                                                      "prompt_suffix_file"))
    ImgV = ContentVersion("img", {"params": Img["params"], "gen": Gen, "edit": Edit_, "suffix": Suffix})
    return {"nano-banana-pro": ImgV, "nano-banana-pro-edit": ImgV,
            "minimax-camera": ContentVersion("mov", Raw["movie"]["params"]),
            "hi3d": ContentVersion("mesh", Raw["mesh"]["params"])}


def SeedConfigs() -> dict[str, dict]:
    """Version 1 of each model = exactly what the pipeline sent before this module existed."""
    Raw = json.loads((ConfigDir / "generation.json").read_text(encoding="utf-8"))
    Img = Raw["images"]
    Suffix = _ReadText(Img["prompt_suffix_file"])
    ImgParams = {K: V for K, V in Img.get("params", {}).items() if K not in ("num_images", "seed", "sync_mode")}
    Template = "{{user_text}}\n\n" + Suffix                     # the former hardcoded "<text>\n\n<suffix>"
    Gen = {"prompt": Template, "system_prompt": _ReadText(Img["generate_system_prompt_file"]), **ImgParams}
    Edit = {"prompt": Template, "system_prompt": _ReadText(Img["edit_system_prompt_file"]), **ImgParams}
    Movie = {K: V for K, V in Raw["movie"].get("params", {}).items() if K not in ("image_url", "sync_mode")}
    Mesh = {K: V for K, V in Raw["mesh"].get("params", {}).items() if K not in ("image_url", "model")}
    return {"any-llm": {"prompt": "{{user_prompt}}"}, "nano-banana-pro": Gen, "nano-banana-pro-edit": Edit,
            "minimax-camera": Movie, "hi3d": Mesh}


PromptKeys = ("prompt", "system_prompt")


def CharmSeed(ModelId: str, RingParams: dict) -> dict:
    """Version 1 of a charm model: the provider settings of the ring model's active version (so the charm starts from
    what is tuned and running for rings) with the charm's own prompts — never a reference to a ring version."""
    Raw = json.loads((ConfigDir / "generation.json").read_text(encoding="utf-8"))["charm"]
    Keep = {K: V for K, V in RingParams.items() if K not in PromptKeys and not K.startswith("variation_")}
    if ModelId in ("nano-banana-pro-charm", "nano-banana-pro-edit-charm"):
        Template = "{{user_text}}\n\n" + _ReadText(Raw["prompt_suffix_file"])
        System = _ReadText(Raw["generate_system_prompt_file" if ModelId == "nano-banana-pro-charm" else "edit_system_prompt_file"])
        Out = {**Keep, "prompt": Template, "system_prompt": System}
        if ModelId == "nano-banana-pro-edit-charm":
            Out.update({f"variation_{K}": V for K, V in CharmVariationDefaults.items()})
        return Out
    if ModelId == "minimax-camera-charm":
        return {**Keep, "prompt": _ReadText(Raw["movie_prompt_file"])}
    return dict(RingParams)                                    # hi3d-charm: the same 3D settings as rings


RingCounterpart = {"nano-banana-pro-charm": "nano-banana-pro", "nano-banana-pro-edit-charm": "nano-banana-pro-edit",
                   "minimax-camera-charm": "minimax-camera", "hi3d-charm": "hi3d"}


Schema = """
CREATE TABLE IF NOT EXISTS model_config_versions (
    id           TEXT PRIMARY KEY,          -- "<model>@v<n>-<hash8>"
    model        TEXT NOT NULL,
    number       INTEGER NOT NULL,
    config_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    legacy_alias TEXT,                      -- v1 only: the content-hash version requests carried before
    UNIQUE (model, number)
);
CREATE TABLE IF NOT EXISTS model_config_active (
    model         TEXT PRIMARY KEY,
    version_id    TEXT NOT NULL REFERENCES model_config_versions(id),
    activated_at  TEXT NOT NULL,
    activated_by  TEXT NOT NULL
);
"""


@dataclass
class Version:
    Id: str
    Model: str
    Number: int
    Params: dict
    CreatedAt: str
    CreatedBy: str
    Note: str = ""
    Extra: dict = field(default_factory=dict)

    def ToJson(self) -> dict:
        return {"id": self.Id, "model": self.Model, "number": self.Number, "params": self.Params,
                "created_at": self.CreatedAt, "created_by": self.CreatedBy, "note": self.Note, **self.Extra}


def _Hash(Params: dict) -> str:
    return hashlib.sha256(json.dumps(Params, sort_keys=True).encode("utf-8")).hexdigest()[:8]


class ModelConfigStore:
    def __init__(self, Db: Database):
        self.Db = Db
        with Db.Connect() as Conn:
            Conn.executescript(Schema)
        self._Cache: dict[str, Version] = {}
        self._SeedMissing()

    def _SeedMissing(self) -> None:
        Seeds = Aliases = None
        for ModelId in Models:
            if self.Db.One("SELECT 1 AS x FROM model_config_active WHERE model = ?", (ModelId,)):
                continue
            if Models[ModelId].Product != "ring":
                continue                                       # charms are seeded after the rings (from their active versions)
            Seeds, Aliases = Seeds or SeedConfigs(), Aliases or LegacyAliases()
            Clean = Validate(ModelId, Seeds[ModelId])
            V = self._Insert(ModelId, Clean, "seed", "Initial version from config/generation.json and config/prompts")
            self.Db.Execute("UPDATE model_config_versions SET legacy_alias = ? WHERE id = ?", (Aliases.get(ModelId), V.Id))
            self._Activate(ModelId, V.Id, "seed")
        self._SeedInternalDefaults()
        self._SeedCharms()

    def _SeedCharms(self) -> None:
        """Charm models without a version get version 1: the ring model's active provider settings + the charm prompts.
        Nothing about the ring models changes."""
        for ModelId, Spec in Models.items():
            if Spec.Product != "charm" or self.Db.One("SELECT 1 AS x FROM model_config_active WHERE model = ?", (ModelId,)):
                continue
            Ring = self.Active(RingCounterpart[ModelId])
            V = self._Insert(ModelId, Validate(ModelId, CharmSeed(ModelId, Ring.Params)), "seed",
                             f"Initial Charm version: provider settings from the Ring configuration v{Ring.Number}, "
                             "Charm prompts from config/prompts/charm_*")
            self._Activate(ModelId, V.Id, "seed")

    def _SeedInternalDefaults(self) -> None:
        """Pipeline-only parameters that arrived after a model was first configured (the refinement variation
        directives) are added once, as a new visible version with their default texts, so the Admin can see,
        edit or blank them. Never repeated: once any version of the model carries them, nothing is added."""
        for ModelId, Spec in Models.items():
            if Spec.Product != "ring":
                continue                    # a charm model is seeded with its directives (_SeedCharms)
            Internal = [P for P in Spec.Params if P.Internal and P.Default is not None]
            if not Internal:
                continue
            Names = {P.Name for P in Internal}
            if any(Names & set(json.loads(R["config_json"])) for R in
                   self.Db.All("SELECT config_json FROM model_config_versions WHERE model = ?", (ModelId,))):
                continue
            Params = {**self.Active(ModelId).Params, **{P.Name: P.Default for P in Internal}}
            V = self._Insert(ModelId, Validate(ModelId, Params), "update",
                             "Refinement variations A–D added with their default directives (edit or blank them here)")
            self._Activate(ModelId, V.Id, "update")
        self._UpgradeUneditedDefaults()

    def _UpgradeUneditedDefaults(self) -> None:
        """An installation still running an earlier set of default directives, unedited, gets the current
        defaults as a new visible version. Texts the admin changed (or blanked) are never touched."""
        Spec = Models.get("nano-banana-pro-edit")
        if Spec is None:
            return
        Active = self.Active(Spec.Id)
        Current = {K: Active.Params.get(f"variation_{K}") for K in "abcd"}
        if Current == VariationDefaults or Current not in PreviousVariationDefaults:
            return
        Params = {**Active.Params, **{f"variation_{K}": V for K, V in VariationDefaults.items()}}
        V = self._Insert(Spec.Id, Validate(Spec.Id, Params), "update",
                         "Refinement variations A–D: stronger default directives (the earlier defaults were in use, unedited)")
        self._Activate(Spec.Id, V.Id, "update")

    def _Row(self, R: dict) -> Version:
        return Version(R["id"], R["model"], R["number"], json.loads(R["config_json"]), R["created_at"],
                       R["created_by"], R["note"])

    def _Insert(self, ModelId: str, Params: dict, By: str, Note: str) -> Version:
        with self.Db.Transaction() as Conn:
            N = (Conn.execute("SELECT MAX(number) FROM model_config_versions WHERE model = ?", (ModelId,)).fetchone()[0] or 0) + 1
            Vid = f"{ModelId}@v{N}-{_Hash(Params)}"
            Conn.execute("INSERT INTO model_config_versions (id, model, number, config_json, created_at, created_by, note) "
                         "VALUES (?,?,?,?,?,?,?)", (Vid, ModelId, N, Dumps(Params), Now(), By, Note))
        return self.Get(Vid)

    def _Activate(self, ModelId: str, Vid: str, By: str) -> None:
        self.Db.Execute("INSERT INTO model_config_active (model, version_id, activated_at, activated_by) VALUES (?,?,?,?) "
                        "ON CONFLICT(model) DO UPDATE SET version_id = excluded.version_id, "
                        "activated_at = excluded.activated_at, activated_by = excluded.activated_by",
                        (ModelId, Vid, Now(), By))

    # ── reads ────────────────────────────────────────────────────────────
    def Get(self, Vid: str) -> Version | None:
        if Vid in self._Cache:
            return self._Cache[Vid]
        R = self.Db.One("SELECT * FROM model_config_versions WHERE id = ?", (Vid,))
        if R is None:
            return None
        self._Cache[Vid] = V = self._Row(R)                     # versions are immutable
        return V

    def Active(self, ModelId: str) -> Version:
        R = self.Db.One("SELECT version_id FROM model_config_active WHERE model = ?", (ModelId,))
        return self.Get(R["version_id"])

    def Equivalent(self, ModelId: str) -> list[str]:
        """Version ids (and legacy aliases) whose parameters equal the active version's — results made
        under any of them are the same as under the active one (e.g. movie reuse)."""
        Active = self.Active(ModelId)
        Out = []
        for R in self.Db.All("SELECT id, config_json, legacy_alias FROM model_config_versions WHERE model = ?", (ModelId,)):
            if json.loads(R["config_json"]) == Active.Params:
                Out += [R["id"]] + ([R["legacy_alias"]] if R["legacy_alias"] else [])
        return Out

    def ActiveFor(self, Endpoint: str, Product: str = "ring") -> Version:
        """The active configuration of this endpoint for this product (rings by default, as before)."""
        return self.Active(ModelIdFor(Endpoint, Product))

    def Supports(self, Product: str) -> bool:
        """Every pipeline endpoint (design images, refinement, movie, 3D) has a model for this product."""
        if Product == "ring":
            return True
        return all(any(getattr(S, "Product", "ring") == Product and S.Endpoint == E for S in Models.values())
                   for E in (endpoints.ImageGenerate, endpoints.ImageEdit, endpoints.Movie, endpoints.Mesh))

    def Resolve(self, Vid: str | None, Endpoint: str, Product: str = "ring") -> Version:
        """The version a request was created with. Requests created before versioned configs carry an
        old content hash; they get version 1 — exactly the settings the pipeline sent at the time."""
        V = self.Get(Vid) if Vid else None
        if V is not None:
            return V
        Model = ModelIdFor(Endpoint, Product)
        R = self.Db.One("SELECT id FROM model_config_versions WHERE model = ? AND legacy_alias = ?", (Model, Vid or ""))
        if R:
            return self.Get(R["id"])
        R = self.Db.One("SELECT id FROM model_config_versions WHERE model = ? ORDER BY number LIMIT 1", (Model,))
        return self.Get(R["id"])

    def History(self, ModelId: str) -> list[dict]:
        Active = self.Db.One("SELECT * FROM model_config_active WHERE model = ?", (ModelId,))
        Out = []
        for R in self.Db.All("SELECT * FROM model_config_versions WHERE model = ? ORDER BY number DESC", (ModelId,)):
            V = self._Row(R).ToJson()
            V["active"] = bool(Active and Active["version_id"] == R["id"])
            Out.append(V)
        return Out

    def State(self, ModelId: str) -> dict:
        A = self.Db.One("SELECT * FROM model_config_active WHERE model = ?", (ModelId,))
        return {"model": Models[ModelId].ToJson(), "active": self.Get(A["version_id"]).ToJson(),
                "activated_at": A["activated_at"], "activated_by": A["activated_by"], "history": self.History(ModelId)}

    # ── writes ───────────────────────────────────────────────────────────
    def SaveAndActivate(self, ModelId: str, Params: dict, By: str, Note: str = "") -> dict:
        Clean = Validate(ModelId, Params)
        Current = self.Active(ModelId)
        if Current.Params == Clean:
            return {"changed": False, **self.State(ModelId)}
        V = self._Insert(ModelId, Clean, By, Note)
        self._Activate(ModelId, V.Id, By)
        return {"changed": True, **self.State(ModelId)}

    def Restore(self, ModelId: str, Vid: str, By: str) -> dict:
        Old = self.Get(Vid)
        if Old is None or Old.Model != ModelId:
            raise ConfigError([f"Unknown version {Vid} for {ModelId}."])
        return self.SaveAndActivate(ModelId, Old.Params, By, f"Restored from v{Old.Number}")

    # ── preview / export ─────────────────────────────────────────────────
    def Preview(self, ModelId: str, Params: dict | None = None) -> dict:
        """The final request payload with labeled sample runtime inputs. Never calls the provider."""
        Spec = Models[ModelId]
        Clean = Validate(ModelId, Params) if Params is not None else self.Active(ModelId).Params
        Runtime = dict(SampleRuntime)
        if Spec.Product == "charm":
            Runtime.update({K: f"[SAMPLE {L}] {CharmSampleText}" for K, L in (("user_text", "customer text"),
                            ("design_prompt", "design prompt"), ("user_prompt", "customer prompt"))})
        Letters = {K: SlotDirective(ModelId, Clean, I) for I, K in enumerate("ABCD")} if any(P.Internal for P in Spec.Params) else None
        return {"endpoint": Spec.Endpoint, "payload": BuildRequest(ModelId, Clean, Runtime),
                "slot_directives": Letters,
                "sample_runtime": {K: V for K, V in Runtime.items()
                                   if K in {"seed", "image_url"} | set(P for Prm in Spec.Params for P in Prm.Placeholders)},
                "omitted": [P.Name for P in Spec.Params if P.Name not in Clean],
                "submitted": False}

    def Export(self, ModelIds: list[str]) -> dict:
        Out = []
        for M in ModelIds:
            S = self.State(M)
            Out.append({"model": M, "label": Models[M].Label, "endpoint": Models[M].Endpoint, "product": Models[M].Product,
                        "connected_to_pipeline": Models[M].Connected, "version": S["active"]["id"],
                        "version_number": S["active"]["number"], "activated_at": S["activated_at"],
                        "activated_by": S["activated_by"], "parameters": S["active"]["params"],
                        "omitted_parameters_use_provider_default": [P.Name for P in Models[M].Params
                                                                    if P.Name not in S["active"]["params"]],
                        "pipeline_controlled": [{"name": F.Name, "value": F.Value, "runtime": F.Runtime,
                                                 "description": F.Description} for F in Models[M].Fixed]})
        return {"exported_at": Now(), "runtime_placeholders": RuntimeInputs, "models": Out}


def ExportText(Data: dict) -> str:
    Lines = [f"XJet Atelier — Pipeline 3 AI prompts & parameters", f"Exported {Data['exported_at']}", ""]
    Lines += ["Runtime placeholders (filled by the pipeline for every request):"]
    Lines += [f"  {{{{{K}}}}} — {V}" for K, V in Data["runtime_placeholders"].items()] + [""]
    for M in Data["models"]:
        Lines += ["=" * 78, f"{M['label']}   ({M['endpoint']}) — {(M.get('product') or 'ring').title()} configuration",
                  f"Version {M['version']} (v{M['version_number']}) — activated {M['activated_at']} by {M['activated_by']}",
                  "Connected to the P3 pipeline: " + ("yes" if M["connected_to_pipeline"] else "no"), "=" * 78]
        for K, V in M["parameters"].items():
            if isinstance(V, str) and ("\n" in V or len(V) > 70):
                Lines += [f"{K}:", "-" * 40, V, "-" * 40]
            elif isinstance(V, list):
                Lines += [f"{K}:"] + [f"  {json.dumps(X)}" for X in V]
            else:
                Lines.append(f"{K}: {json.dumps(V)}")
        if M["omitted_parameters_use_provider_default"]:
            Lines.append("Not set (provider default): " + ", ".join(M["omitted_parameters_use_provider_default"]))
        Lines.append("Set by the pipeline: " + ", ".join(
            f"{F['name']}=" + ("<runtime>" if F["runtime"] else "not sent" if F["value"] is None else json.dumps(F["value"]))
            for F in M["pipeline_controlled"]))
        Lines.append("")
    return "\n".join(Lines)
