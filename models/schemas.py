"""Pydantic schemas for the Contentr API.

All request/response models used by the FastAPI gateway and dispatch layer.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────────────


class Platform(str, Enum):
    """Supported publishing platforms."""

    tiktok = "tiktok"
    instagram = "instagram"
    youtube = "youtube"
    facebook = "facebook"


class ContentFormat(str, Enum):
    """Content format determines orientation and duration target."""

    short = "short"  # vertical, under 60s — TikTok, Reels, Shorts
    long = "long"  # horizontal, 5–20 min — YouTube videos


class ContentGoal(str, Enum):
    """Creative intent for the generated content."""

    inform = "inform"
    entertain = "entertain"
    opinion = "opinion"
    breakdown = "breakdown"


class ContentNiche(str, Enum):
    """Topic niche used by the discovery agent to filter relevant stories."""

    general = "general"
    finance = "finance"
    tech = "tech"
    fashion = "fashion"
    sports = "sports"
    health = "health"
    politics = "politics"
    entertainment = "entertainment"
    business = "business"
    crypto = "crypto"


class VisualMode(str, Enum):
    """Controls the image generation style and prompt anchor phrase.

    thumbnail:  YouTube thumbnail — bold stylized 3D character, high
                contrast, space reserved for title text. Best for YouTube.
    social:     Social media graphic — flat/graphic design, no characters,
                bold colors. Best for TikTok, Reels, Instagram.
    marketing:  Clean product/brand asset — studio lighting, minimal
                background. Best for marketers and product shots.
    """

    thumbnail = "thumbnail"
    social = "social"
    marketing = "marketing"


class AspectRatio(str, Enum):
    """Supported output aspect ratios.

    Imagen 4 natively supports: 9:16, 16:9, 4:3, 3:4, 1:1.
    3:2 is mapped to 16:9 (nearest supported landscape ratio).

    Usage guide:
        9:16  — TikTok, Instagram Reels, YouTube Shorts, Stories
        16:9  — YouTube thumbnails and videos, widescreen
        4:3   — Facebook posts, presentations
        3:2   — Twitter cards, Instagram landscape
               (rendered at 16:9 by Imagen, crop client-side if needed)
    """

    vertical = "9:16"
    widescreen = "16:9"
    standard = "4:3"
    landscape = "3:2"


# Maps our AspectRatio enum to the nearest Imagen-supported string.
IMAGEN_ASPECT_RATIO_MAP: dict[AspectRatio, str] = {
    AspectRatio.vertical: "9:16",
    AspectRatio.widescreen: "16:9",
    AspectRatio.standard: "4:3",
    AspectRatio.landscape: "16:9",  # 3:2 not natively supported
}


def resolve_aspect_ratio(ratio: AspectRatio) -> str:
    """Return the Imagen-compatible ratio string for a given AspectRatio value.

    Args:
        ratio: The AspectRatio enum value requested by the user.

    Returns:
        A ratio string accepted by Imagen 4.
    """
    return IMAGEN_ASPECT_RATIO_MAP.get(ratio, "9:16")


# ── Request models ────────────────────────────────────────────────────────────


class StyleMemory(BaseModel):
    """Creator's personal style profile. Saved once, applied to every job."""

    handle: Optional[str] = None
    tone: str
    pace: str
    visual_style: str
    avoid: str
    creator_type: str
    hook_style: str
    platform_primary: Platform


class DiscoverBrief(BaseModel):
    """Sent to /ws/discover when the user has no topic yet."""

    user_id: str
    platform: Platform
    goal: ContentGoal
    niche: ContentNiche = ContentNiche.general


class GenerateBrief(BaseModel):
    """Sent to /ws/generate to produce intel + captions.

    verify_with_intel: Use Case 2 can set this to False to skip web
    research and write captions directly from the user's raw idea.
    """

    user_id: str
    topic: str
    platform: Platform
    goal: ContentGoal
    chosen_angle: Optional[dict] = None
    verify_with_intel: bool = True


class RenderEnhanceBrief(BaseModel):
    """Sent as the first message to /ws/render (Phase 1).

    Use Case 1 and 2: provide script (from /ws/generate output).
    Use Case 3: provide user_description and product_image_b64 instead.
    """

    user_id: str
    platform: Platform
    content_format: ContentFormat = ContentFormat.short
    visual_mode: VisualMode = VisualMode.social
    aspect_ratio: AspectRatio = AspectRatio.vertical

    # Use Case 1 and 2
    script: Optional[dict] = None

    # Use Case 3
    user_description: Optional[str] = None
    product_image_b64: Optional[str] = None


class ConfirmedScene(BaseModel):
    """A single scene prompt after user review.

    The user may have edited enhanced_prompt — that is the value
    sent to Imagen. original_prompt is preserved for reference only.
    """

    scene_number: int
    scene_label: str
    enhanced_prompt: str
    negative_prompt: str = ""
    is_product_background: bool = False


class ConfirmedRenderBrief(BaseModel):
    """Sent as the second message to /ws/render (Phase 2).

    Contains the user's confirmed (possibly edited) scene prompts.
    product_image_b64 only required for Use Case 3.
    """

    confirmed_scenes: list[ConfirmedScene]
    aspect_ratio: str = "9:16"
    visual_mode: VisualMode = VisualMode.social
    product_image_b64: Optional[str] = None
