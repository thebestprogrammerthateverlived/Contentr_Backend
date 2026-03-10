"""Creator personality presets for Contentr.

Instead of a real user system, the app ships with six pre-built
creator archetypes. The user picks one at the entry screen.
Each maps to a full StyleMemory profile stored in Redis on startup.

These are creative starting points, not locked profiles. The frontend
can let users fine-tune individual fields after selecting a preset.
"""

from services.redis_service import save_style, get_style

# ── Preset definitions ────────────────────────────────────────────────────────

PERSONALITIES: dict[str, dict] = {
    "tech_visionary": {
        "display_name": "Tech Visionary",
        "tagline": "First to explain what everyone else is still confused about.",
        "emoji": "⚡",
        "tone": "direct, confident, slightly contrarian",
        "pace": "fast — short punchy sentences, no filler words",
        "visual_style": "dark background, electric blue accents, bold sans-serif text, 3D render aesthetic",
        "avoid": "corporate jargon, wishy-washy language, stock photo aesthetic, excessive hype",
        "creator_type": "tech",
        "hook_style": "controversial statement first, then explain why",
        "platform_primary": "youtube",
    },
    "finance_educator": {
        "display_name": "Finance Educator",
        "tagline": "Making money moves make sense for normal people.",
        "emoji": "📈",
        "tone": "clear, authoritative, accessible — no condescension",
        "pace": "moderate — builds the argument methodically, payoff at the end",
        "visual_style": "clean minimal dark, gold and white accents, data-forward graphics",
        "avoid": "get-rich-quick framing, vague advice, sensationalism, complex jargon without explanation",
        "creator_type": "finance",
        "hook_style": "stat shock — open with a surprising number",
        "platform_primary": "youtube",
    },
    "lifestyle_creator": {
        "display_name": "Lifestyle Creator",
        "tagline": "Real talk about the stuff that actually affects your life.",
        "emoji": "✨",
        "tone": "warm, conversational, like talking to a trusted friend",
        "pace": "relaxed but never slow — flows naturally with energy highs and lows",
        "visual_style": "warm tones, soft gradients, aesthetic and clean, minimal clutter",
        "avoid": "cold professional tone, fear-based framing, anything that feels inauthentic or forced",
        "creator_type": "lifestyle",
        "hook_style": "relatable scenario — place the viewer in a moment they recognise",
        "platform_primary": "instagram",
    },
    "hype_machine": {
        "display_name": "Hype Machine",
        "tagline": "If it's trending, you're already on it.",
        "emoji": "🔥",
        "tone": "high energy, enthusiastic, infectious — maximum excitement without being annoying",
        "pace": "very fast — almost stream of consciousness, quick cuts, no breathing room",
        "visual_style": "vivid neon, bold gradients, maximum contrast, chaotic energy that still works",
        "avoid": "slow builds, corporate tone, anything understated, long explanations",
        "creator_type": "entertainment",
        "hook_style": "bold promise — open with the most exciting claim immediately",
        "platform_primary": "tiktok",
    },
    "news_anchor": {
        "display_name": "News Anchor",
        "tagline": "The story, the context, no noise.",
        "emoji": "📰",
        "tone": "authoritative, neutral, precise — opinion only when clearly labelled",
        "pace": "steady and measured — deliberate word choice, no rushing",
        "visual_style": "professional dark theme, clean typography, journalism aesthetic",
        "avoid": "clickbait, emotional manipulation, unverified claims, excessive editorialising",
        "creator_type": "news",
        "hook_style": "the hook fact — open with the single most important fact of the story",
        "platform_primary": "youtube",
    },
    "contrarian": {
        "display_name": "The Contrarian",
        "tagline": "Everyone agrees on this. They're all wrong.",
        "emoji": "🎯",
        "tone": "sharp, provocative, intellectually confident — challenges consensus",
        "pace": "moderate to fast — builds tension then delivers the argument",
        "visual_style": "high contrast black and red, minimal, text-heavy, aggressive negative space",
        "avoid": "playing it safe, agreeing with mainstream takes, corporate-friendly language",
        "creator_type": "opinion",
        "hook_style": "myth buster — open by stating the popular belief, then immediately challenge it",
        "platform_primary": "youtube",
    },
}


def load_all_personalities() -> None:
    """Seed all personality presets into Redis.

    Called on app startup. Only writes if not already present so
    restarts don't overwrite user customisations.
    """
    for preset_id, style in PERSONALITIES.items():
        existing = get_style(preset_id)
        if not existing:
            # Strip display-only fields before saving as StyleMemory
            style_memory = {
                k: v for k, v in style.items()
                if k not in ("display_name", "tagline", "emoji")
            }
            save_style(preset_id, style_memory)


def get_personality_list() -> list[dict]:
    """Return the preset list for the frontend picker UI."""
    return [
        {
            "id": preset_id,
            "display_name": data["display_name"],
            "tagline": data["tagline"],
            "emoji": data["emoji"],
            "platform_primary": data["platform_primary"],
            "creator_type": data["creator_type"],
        }
        for preset_id, data in PERSONALITIES.items()
    ]