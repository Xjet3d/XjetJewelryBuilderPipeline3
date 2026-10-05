# Ring prompts — inconsistencies found during the Charms work (2026-10-05)

**Status: documented only, not fixed.** The Charms project leaves the Ring prompts exactly as they are; nothing below
has been changed. The Charm prompts (`config/prompts/charm_*.txt`) were written separately and avoid these points.

**Where the Ring prompts live.**
- Version 1 of each Ring image model was seeded from `config/prompts/image_generate_system.txt`,
  `config/prompts/image_edit_system.txt` and `config/prompts/image_suffix.txt`.
- The image prompt template of both Ring image models (`nano-banana-pro` and `nano-banana-pro-edit`) is
  `{{user_text}}` followed by the whole suffix file. Every point about the suffix therefore applies to new designs and
  to refinements.
- What runs is the **active version** in Admin → Settings → AI models & prompts → **Ring**, not the files. If an Admin
  has edited the active versions since, check the text there (History lists every version) before acting on this note.

## 1. The suffix's camera contradicts the system prompts' camera

| Source | Instruction |
|---|---|
| `image_suffix.txt` line 3 | "CAMERA: Eye-level, straight-on, perfectly horizontal … No elevation, no tilt downward or upward." |
| `image_suffix.txt` line 2 | "The hole through the ring faces directly toward the camera." |
| `image_generate_system.txt` lines 503–515 | A front three-quarter view: the ring rotated about 25–30° from a direct front view, the camera about 8–12° above the horizontal centerline. Line 543 adds: "Do not use a direct flat front elevation." |
| `image_edit_system.txt` lines 369–379 | The same 25–30° rotation and 8–12° camera elevation. |

The image model receives both instructions in one request, so the viewpoint it renders depends on which one it follows.

## 2. The suffix tells the model to ignore the system prompt's camera rules

- `image_suffix.txt` line 1: "Completely disregard any previous camera angle, orientation, background, or multi-view
  instructions mentioned above."
- `image_generate_system.txt` line 9: "The rules in this System Prompt are mandatory and cannot be overridden by the
  user's Prompt."

The suffix is appended to the customer's text, so it reaches the model as part of the user prompt. That makes it
exactly the kind of override the system prompt says it refuses, and it is the reason for point 1.

## 3. The suffix describes gemstones; the system prompts forbid them

- `image_suffix.txt` line 6: "True-to-material rendering — gold looks like gold, diamonds sparkle, gems are translucent."
- `image_generate_system.txt` lines 149–157: "ABSOLUTE NO-GEMSTONE POLICY": zero gemstones, even when the customer
  asks for them.

Mentioning sparkling diamonds and translucent gems in every request works against the no-gemstone rule.

## 4. Garbled characters in the suffix (and so in Ring version 1)

Suffix lines 2, 3 and 6 contain `â€”` where an em-dash (—) was meant. This is UTF-8 text decoded once as Windows-1252
and saved again. The garbled text was copied into the Ring v1 image prompt templates, so it is sent with every Ring
request that uses those versions. The model probably reads past it, but it is noise in the prompt.

## 5. The edit system prompt names "Veo"

`image_edit_system.txt` lines 429, 723 and 865 describe the orientation as useful "for later Veo image-to-video
rotation". Pipeline 3 makes its 360° movie with MiniMax camera controls (`minimax/h3-max/camera-controls`), not Veo.
The words are harmless to the image model but describe the wrong pipeline.

## If these are fixed later

- Fix them as a new Ring version in Admin → AI models & prompts → Ring (*Save & Activate*). This keeps History and the
  previous version restorable. Version 1 should not be rewritten.
- Compare a few test designs before and after the change. A fix to points 1–3 changes how every new Ring image looks.
- The Charm configuration is separate: changing a Ring version never changes a Charm prompt or version, and the
  reverse is also true.
