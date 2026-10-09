"""Provider endpoint ids. Model choices are code, not admin-editable config.

Verified to exist on fal.ai (OpenAPI schemas fetched 2026-09-29):
  * fal-ai/nano-banana-pro, fal-ai/nano-banana-pro/edit — num_images max 4, so a
    six-candidate batch is six single-image requests (one per slot).
  * minimax/h3-max/camera-controls — required by the product owner for Customize video.
  * hitem3d/hi3d/v3.0/image-to-3d — required single-image developer mesh endpoint;
    export_format supports "stl" directly.
"""

ImageGenerate = "fal-ai/nano-banana-pro"
ImageEdit     = "fal-ai/nano-banana-pro/edit"
Movie         = "minimax/h3-max/camera-controls"
Mesh          = "hitem3d/hi3d/v3.0/image-to-3d"
Llm           = "fal-ai/any-llm"                   # the prompt check (p3/promptcheck.py)
