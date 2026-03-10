"""Imagen generation service for Contentr.

Handles image generation via Imagen 4 and product compositing via Imagen 3
Capability. Resolves aspect ratios to Imagen-supported values before calling
the API.
"""

import os
import tempfile

from google import genai
from google.genai import types

from models.schemas import AspectRatio, resolve_aspect_ratio
from services.storage_service import upload_image_bytes


_client: genai.Client | None = None


def get_client() -> genai.Client:
    """Return a lazily initialised Vertex AI Genai client.

    Returns:
        A configured genai.Client instance using Vertex AI auth.
    """
    global _client
    if _client is None:
        _client = genai.Client(
            vertexai=True,
            project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            location=os.getenv("GOOGLE_CLOUD_REGION", "us-central1"),
        )
    return _client


async def generate_scene_images(
    scenes: list[dict],
    job_id: str,
    product_image_bytes: bytes | None = None,
    aspect_ratio: str = "9:16",
) -> list[dict]:
    """Generate one image per scene and upload each to GCS.

    For standard scenes, calls Imagen 4 generate. For product background
    scenes with a product image supplied, calls Imagen 3 Capability edit
    to composite the product into the generated scene.

    The aspect_ratio parameter accepts either a raw string (e.g. "9:16")
    or an AspectRatio enum value — both are resolved to the nearest Imagen-
    supported ratio before the API call.

    Args:
        scenes: List of scene dicts from the image_director agent, each
            containing at minimum: scene_number, scene_label, imagen_prompt,
            negative_prompt, is_product_background.
        job_id: Unique job identifier used to build GCS object names.
        product_image_bytes: Optional raw bytes of an uploaded product image.
            Only used when is_product_background is True on a scene.
        aspect_ratio: Desired output ratio. Resolved to the nearest ratio
            supported by Imagen before the API call.

    Returns:
        List of result dicts, one per scene, each containing:
            scene_number, scene_label, image_url, prompt_used, error.
    """
    # Resolve ratio — handles both raw strings and AspectRatio enum values.
    resolved_ratio = _resolve_ratio(aspect_ratio)

    client = get_client()
    results: list[dict] = []

    for scene in scenes:
        scene_number: int = scene.get("scene_number", 0)
        scene_label: str = scene.get("scene_label", f"scene_{scene_number}")
        prompt: str = scene.get("imagen_prompt", "")
        negative: str = scene.get("negative_prompt", "")
        is_product_bg: bool = scene.get("is_product_background", False)

        try:
            image_bytes = _generate_image(
                client=client,
                prompt=prompt,
                negative=negative,
                is_product_bg=is_product_bg,
                product_image_bytes=product_image_bytes,
                aspect_ratio=resolved_ratio,
            )

            image_id = f"{job_id}_{scene_label}"
            url = upload_image_bytes(image_bytes, image_id)

            results.append(
                {
                    "scene_number": scene_number,
                    "scene_label": scene_label,
                    "image_url": url,
                    "prompt_used": prompt,
                    "aspect_ratio_used": resolved_ratio,
                    "error": None,
                }
            )

        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "scene_number": scene_number,
                    "scene_label": scene_label,
                    "image_url": None,
                    "prompt_used": prompt,
                    "aspect_ratio_used": resolved_ratio,
                    "error": str(exc),
                }
            )

    return results


# ── Private helpers ───────────────────────────────────────────────────────────


def _resolve_ratio(aspect_ratio: str | AspectRatio) -> str:
    """Map an aspect ratio value to the nearest Imagen-supported string.

    Args:
        aspect_ratio: Either a raw ratio string (e.g. "9:16", "3:2") or
            an AspectRatio enum value.

    Returns:
        A ratio string that Imagen 4 accepts.
    """
    # If it's an AspectRatio enum, use the canonical mapping.
    if isinstance(aspect_ratio, AspectRatio):
        return resolve_aspect_ratio(aspect_ratio)

    # If it's a raw string, normalise and check against known values.
    _supported = {"9:16", "16:9", "4:3", "3:4", "1:1"}
    _fallback_map = {
        "3:2": "16:9",   # closest landscape ratio Imagen supports
        "2:3": "3:4",    # closest portrait ratio
    }

    ratio_str = str(aspect_ratio).strip()

    if ratio_str in _supported:
        return ratio_str
    if ratio_str in _fallback_map:
        return _fallback_map[ratio_str]

    # Unknown ratio — default to vertical for social content.
    return "9:16"


def _generate_image(
    client: genai.Client,
    prompt: str,
    negative: str,
    is_product_bg: bool,
    product_image_bytes: bytes | None,
    aspect_ratio: str,
) -> bytes:
    """Call the appropriate Imagen endpoint and return raw PNG bytes.

    Uses Imagen 4 generate for standard scenes. Uses Imagen 3 Capability
    edit (OUTPAINTING) for product background compositing.

    Args:
        client: Initialised genai.Client.
        prompt: The imagen_prompt string from the image director agent.
        negative: The negative_prompt string.
        is_product_bg: Whether this scene should use product compositing.
        product_image_bytes: Raw bytes of the user's product photo (or None).
        aspect_ratio: Resolved Imagen-compatible ratio string.

    Returns:
        Raw PNG bytes of the generated image.

    Raises:
        ValueError: If product mode is requested but no product bytes supplied.
        RuntimeError: If the Imagen API returns no generated images.
    """
    if is_product_bg and product_image_bytes:
        return _composite_product(
            client=client,
            background_prompt=prompt,
            product_bytes=product_image_bytes,
            aspect_ratio=aspect_ratio,
        )

    return _standard_generate(
        client=client,
        prompt=prompt,
        negative=negative,
        aspect_ratio=aspect_ratio,
    )


def _standard_generate(
    client: genai.Client,
    prompt: str,
    negative: str,
    aspect_ratio: str,
) -> bytes:
    """Generate an image with Imagen 4.

    Args:
        client: Initialised genai.Client.
        prompt: Generation prompt.
        negative: Negative prompt (optional — passed only when non-empty).
        aspect_ratio: Imagen-compatible aspect ratio string.

    Returns:
        Raw PNG bytes.

    Raises:
        RuntimeError: If the API returns no images.
    """
    config = types.GenerateImagesConfig(
        negative_prompt=negative if negative else None,
        number_of_images=1,
        aspect_ratio=aspect_ratio,
        output_mime_type="image/png",
    )

    response = client.models.generate_images(
        model="imagen-4.0-generate-001",
        prompt=prompt,
        config=config,
    )

    if not response.generated_images:
        raise RuntimeError("Imagen 4 returned no generated images.")

    return response.generated_images[0].image.image_bytes


def _composite_product(
    client: genai.Client,
    background_prompt: str,
    product_bytes: bytes,
    aspect_ratio: str,
) -> bytes:
    """Composite a product photo into a generated background scene.

    Uses Imagen 3 Capability OUTPAINTING to place the product into
    the described environment.

    Args:
        client: Initialised genai.Client.
        background_prompt: Scene description (background only, no product).
        product_bytes: Raw bytes of the user's product photo.
        aspect_ratio: Imagen-compatible aspect ratio string.

    Returns:
        Raw PNG bytes of the composited image.

    Raises:
        RuntimeError: If the API returns no images.
    """
    product_image = types.RawReferenceImage(
        reference_id=1,
        reference_image=types.Image(image_bytes=product_bytes),
    )

    composite_prompt = (
        f"Place this product naturally and cleanly into the following scene: "
        f"{background_prompt}. "
        f"The product should be the clear focal point. "
        f"Maintain clean studio lighting. "
        f"No distortion, no warping of the product."
    )

    response = client.models.edit_image(
        model="imagen-3.0-capability-001",
        prompt=composite_prompt,
        reference_images=[product_image],
        config=types.EditImageConfig(
            edit_mode="OUTPAINTING",
            number_of_images=1,
            aspect_ratio=aspect_ratio,
            output_mime_type="image/png",
        ),
    )

    if not response.generated_images:
        raise RuntimeError("Imagen 3 Capability returned no composited images.")

    return response.generated_images[0].image.image_bytes