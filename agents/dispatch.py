"""Dispatch orchestrator for Contentr.

Four async generators, each callable from the gateway:

  run_discover        — finds trending stories for a given niche/platform
  run_generate        — intel + captions (no images)
  run_render_enhance  — image director + prompt enhancer → yields enhanced
                        prompts for user approval, no Imagen calls
  run_render_generate — takes confirmed prompts → Imagen → images

Error handling philosophy:
  Each pipeline stage is wrapped in its own try/except.
  On failure, a structured error chunk is yielded and the generator
  exits cleanly. The gateway sends the error chunk to the client and
  the frontend shows an inline error with a retry option.
  We never let an exception from one stage silently kill the pipeline.
"""

import asyncio
import base64
import json
import uuid
from typing import AsyncGenerator

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from agents.caption import caption_agent
from agents.image_director import image_director_agent
from agents.intel import intel_agent
from agents.prompt_enhancer import prompt_enhancer_agent
from agents.topic_discovery import topic_discovery_agent
from models.schemas import AspectRatio, ContentFormat, Platform, resolve_aspect_ratio
from services.imagen_service import generate_scene_images
from services.redis_service import (
    cache_intel,
    create_job,
    get_cached_intel,
    get_style,
    make_topic_key,
    update_job,
)

_session_service = InMemorySessionService()


# ── ADK runner ────────────────────────────────────────────────────────────────

async def _run_agent(agent, prompt: str) -> str:
    """Run a single ADK agent with a one-shot prompt and return its text response."""
    app_name = f"contentr_{agent.name}"
    session_id = str(uuid.uuid4())

    await _session_service.create_session(
        app_name=app_name,
        user_id="contentr_system",
        session_id=session_id,
    )

    runner = Runner(
        agent=agent,
        app_name=app_name,
        session_service=_session_service,
    )

    final_text = ""
    async for event in runner.run_async(
        user_id="contentr_system",
        session_id=session_id,
        new_message=Content(parts=[Part(text=prompt)], role="user"),
    ):
        if event.is_final_response():
            if event.content and event.content.parts:
                final_text = event.content.parts[0].text
            break

    return final_text


# ── JSON parsing ──────────────────────────────────────────────────────────────

def _safe_parse(raw: str, stage: str) -> dict:
    """Parse JSON from agent output, stripping markdown fences if present."""
    cleaned = raw.strip()

    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        cleaned = cleaned.rsplit("```", 1)[0].strip()

    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        if start != -1:
            cleaned = cleaned[start:]

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"[{stage}] Agent returned invalid JSON: {exc}\n\n"
            f"Raw output (first 500 chars):\n{raw[:500]}"
        ) from exc


# ── Error chunk builder ───────────────────────────────────────────────────────

def _error_chunk(job_id: str, stage: str, message: str) -> dict:
    return {
        "type": "error",
        "stage": stage,
        "job_id": job_id,
        "data": {"message": message},
    }


# ── Aspect ratio helpers ──────────────────────────────────────────────────────

def _resolve_ratio(raw: str | AspectRatio) -> str:
    if isinstance(raw, AspectRatio):
        return resolve_aspect_ratio(raw)
    _supported = {"9:16", "16:9", "4:3", "3:4", "1:1"}
    _fallback = {"3:2": "16:9", "2:3": "3:4"}
    ratio_str = str(raw).strip()
    if ratio_str in _supported:
        return ratio_str
    return _fallback.get(ratio_str, "9:16")


def _derive_ratio(platform: str, content_format: str) -> str:
    if platform == Platform.youtube and content_format == ContentFormat.long:
        return "16:9"
    return "9:16"


# ── Discovery pipeline ────────────────────────────────────────────────────────

async def run_discover(brief: dict, job_id: str) -> AsyncGenerator[dict, None]:
    """Find 5 viral-worthy stories for a given niche and platform.

    Yields: status → topics | error
    """
    platform: str = brief.get("platform", "tiktok")
    goal: str = brief.get("goal", "inform")
    niche: str = brief.get("niche", "general")

    yield {
        "type": "status",
        "stage": "discover",
        "job_id": job_id,
        "data": {"message": f"Scanning {niche} news for viral stories on {platform}..."},
    }

    try:
        topic_key = f"discover_{niche}_{platform}"
        cached = get_cached_intel(topic_key)

        if cached:
            topics_result = cached
        else:
            raw = await _run_agent(
                topic_discovery_agent,
                f"Niche: {niche}. Platform: {platform}. Goal: {goal}. "
                f"Find the 5 most viral-worthy stories right now.",
            )
            topics_result = _safe_parse(raw, "topic_discovery")
            cache_intel(topic_key, topics_result, ttl=1800)

    except ValueError as exc:
        # Agent returned bad JSON — not retryable without model change
        yield _error_chunk(job_id, "discover", str(exc))
        return
    except Exception as exc:
        yield _error_chunk(
            job_id, "discover",
            f"Topic discovery failed — please try again. ({type(exc).__name__})"
        )
        return

    yield {
        "type": "topics",
        "stage": "discover",
        "job_id": job_id,
        "data": topics_result,
    }


# ── Generate pipeline: intel + captions ──────────────────────────────────────

async def run_generate(brief: dict, job_id: str) -> AsyncGenerator[dict, None]:
    """Run intel research and caption generation.

    Yields: status → intel (if ran) → status → captions | error
    Error on any stage yields an error chunk and stops the pipeline.
    """
    topic: str = brief["topic"]
    platform: str = brief["platform"]
    goal: str = brief["goal"]
    user_id: str = brief["user_id"]
    chosen_angle: dict | None = brief.get("chosen_angle")
    verify_with_intel: bool = brief.get("verify_with_intel", True)

    style = get_style(user_id) or {}
    creator_type = style.get("creator_type", "general")

    try:
        create_job(job_id, brief)
    except Exception as exc:
        yield _error_chunk(job_id, "setup", f"Could not initialise job: {exc}")
        return

    intel_result: dict | None = None

    # ── Stage 1: Intel (optional) ─────────────────────────────────────────────
    if verify_with_intel:
        yield {
            "type": "status",
            "stage": "intel",
            "job_id": job_id,
            "data": {"message": f"Searching for credible sources on: {topic}"},
        }

        try:
            topic_key = make_topic_key(topic)
            intel_result = get_cached_intel(topic_key)

            if intel_result:
                yield {
                    "type": "status",
                    "stage": "intel",
                    "job_id": job_id,
                    "data": {"message": "Using cached intel (same topic searched recently)"},
                }
            else:
                raw = await _run_agent(
                    intel_agent,
                    f"Topic: {topic}\nCreator type: {creator_type}",
                )
                intel_result = _safe_parse(raw, "intel")
                cache_intel(topic_key, intel_result)

            update_job(job_id, "intel", intel_result)

        except ValueError as exc:
            # Bad JSON from agent — skip intel, continue to captions without it
            print(f"[generate] intel parse error — continuing without intel: {exc}")
            intel_result = None
            yield {
                "type": "status",
                "stage": "intel",
                "job_id": job_id,
                "data": {"message": "Web research unavailable — writing from topic directly"},
            }
        except Exception as exc:
            # Network/API failure — same recovery: continue without intel
            print(f"[generate] intel error — continuing without intel: {exc}")
            intel_result = None
            yield {
                "type": "status",
                "stage": "intel",
                "job_id": job_id,
                "data": {"message": "Web research unavailable — writing from topic directly"},
            }
        else:
            # Only yield intel chunk if we have a valid result
            yield {
                "type": "intel",
                "stage": "intel",
                "job_id": job_id,
                "data": intel_result,
            }

    # ── Stage 2: Captions ─────────────────────────────────────────────────────
    yield {
        "type": "status",
        "stage": "captions",
        "job_id": job_id,
        "data": {"message": "Writing your captions..."},
    }

    try:
        if intel_result:
            angle_to_use = chosen_angle or (
                intel_result["content_angles"][0]
                if intel_result.get("content_angles")
                else {}
            )
            caption_prompt = (
                f"Intel report:\n{json.dumps(intel_result)}\n\n"
                f"Chosen angle:\n{json.dumps(angle_to_use)}\n\n"
                f"Platform: {platform}\n"
                f"Goal: {goal}\n\n"
                f"Style Memory (follow this exactly):\n{json.dumps(style)}"
            )
        else:
            caption_prompt = (
                f"User idea: {topic}\n\n"
                f"Platform: {platform}\n"
                f"Goal: {goal}\n\n"
                f"Style Memory (follow this exactly):\n{json.dumps(style)}"
            )

        raw = await _run_agent(caption_agent, caption_prompt)
        caption_result = _safe_parse(raw, "caption")
        update_job(job_id, "captions", caption_result)

    except ValueError as exc:
        yield _error_chunk(job_id, "captions", str(exc))
        return
    except Exception as exc:
        yield _error_chunk(
            job_id, "captions",
            f"Caption generation failed — please try again. ({type(exc).__name__})"
        )
        return

    yield {
        "type": "captions",
        "stage": "captions",
        "job_id": job_id,
        "data": caption_result,
    }


# ── Render phase 1: image direction + prompt enhancement ─────────────────────

async def run_render_enhance(brief: dict, job_id: str) -> AsyncGenerator[dict, None]:
    """Run image direction and prompt enhancement.

    Yields: status → status → enhanced_prompts | error
    No Imagen calls here — this is the review step before credits are spent.
    """
    platform: str = brief.get("platform", "tiktok")
    content_format: str = brief.get("content_format", "short")
    user_id: str | None = brief.get("user_id")
    visual_mode: str = brief.get("visual_mode", "social")
    script_result: dict | None = brief.get("script")
    user_description: str = brief.get("user_description", "")

    raw_ratio = brief.get("aspect_ratio")
    aspect_ratio = (
        _resolve_ratio(raw_ratio)
        if raw_ratio
        else _derive_ratio(platform, content_format)
    )

    style = get_style(user_id) or {} if user_id else {}

    yield {
        "type": "status",
        "stage": "enhance",
        "job_id": job_id,
        "data": {"message": "Directing your visuals..."},
    }

    # ── Step 1: Image director ─────────────────────────────────────────────
    try:
        if script_result:
            director_prompt = (
                f"Script:\n{json.dumps(script_result)}\n\n"
                f"Visual mode: {visual_mode}\n"
                f"Aspect ratio: {aspect_ratio}\n"
                f"Creator visual style: {style.get('visual_style', 'clean and bold')}\n"
                f"Avoid: {style.get('avoid', 'photorealism, stock photo aesthetic')}\n"
                f"Platform: {platform}\n\n"
                f"Use the {visual_mode.upper()} mode style anchor from your instructions.\n"
                f"STRICT RULE: Return EXACTLY 4 scenes — hook, scene_1, scene_2, scene_3. "
                f"Never return a CTA scene. Never return more than 4 scenes."
            )
        else:
            director_prompt = (
                f"The user wants 4 product/marketing images.\n"
                f"Their description: {user_description}\n\n"
                f"Visual mode: {visual_mode}\n"
                f"Aspect ratio: {aspect_ratio}\n"
                f"Creator visual style: "
                f"{style.get('visual_style', 'clean, minimal, commercial')}\n"
                f"Avoid: {style.get('avoid', 'photorealism, stock photo aesthetic')}\n"
                f"Platform: {platform}\n\n"
                f"Generate exactly 4 distinct scene prompts with different environments.\n"
                f"Use the MARKETING mode style anchor from your instructions.\n"
                f"STRICT RULE: Return EXACTLY 4 scenes. Never return more than 4 scenes."
            )

        raw = await _run_agent(image_director_agent, director_prompt)
        director_result = _safe_parse(raw, "image_director")
        # Hard cap — safety net regardless of what the agent returns
        director_result["scenes"] = director_result.get("scenes", [])[:4]

    except ValueError as exc:
        yield _error_chunk(job_id, "image_director", str(exc))
        return
    except Exception as exc:
        yield _error_chunk(
            job_id, "image_director",
            f"Visual direction failed — please try again. ({type(exc).__name__})"
        )
        return

    # ── Step 2: Prompt enhancer ────────────────────────────────────────────
    yield {
        "type": "status",
        "stage": "enhance",
        "job_id": job_id,
        "data": {"message": "Enhancing your image prompts..."},
    }

    try:
        enhancer_prompt = (
            f"Visual mode: {visual_mode}\n"
            f"Aspect ratio: {aspect_ratio}\n"
            f"Visual style preference: {style.get('visual_style', 'clean and bold')}\n"
            f"Avoid: {style.get('avoid', 'photorealism, stock photo aesthetic')}\n\n"
            f"Raw scene prompts to enhance:\n"
            f"{json.dumps(director_result.get('scenes', []))}"
        )

        raw = await _run_agent(prompt_enhancer_agent, enhancer_prompt)
        enhanced_result = _safe_parse(raw, "prompt_enhancer")
        # Hard cap applied here too
        enhanced_result["scenes"] = enhanced_result.get("scenes", [])[:4]

        update_job(job_id, "enhanced_prompts", enhanced_result)

    except ValueError as exc:
        yield _error_chunk(job_id, "prompt_enhancer", str(exc))
        return
    except Exception as exc:
        yield _error_chunk(
            job_id, "prompt_enhancer",
            f"Prompt enhancement failed — please try again. ({type(exc).__name__})"
        )
        return

    yield {
        "type": "enhanced_prompts",
        "stage": "enhance",
        "job_id": job_id,
        "data": {
            "scenes": enhanced_result.get("scenes", []),
            "style_base": enhanced_result.get("style_base", ""),
            "visual_mode": visual_mode,
            "aspect_ratio": aspect_ratio,
            "message": "Review and edit your prompts before generating images.",
        },
    }


# ── Render phase 2: Imagen generation ────────────────────────────────────────

async def run_render_generate(
    confirmed: dict, job_id: str
) -> AsyncGenerator[dict, None]:
    """Generate images from user-confirmed prompts.

    Yields: status → images → complete | error (per-scene errors are
    included in the images array, not as top-level error chunks,
    so partial success is surfaced rather than failing everything).
    """
    confirmed_scenes: list[dict] = confirmed.get("confirmed_scenes", [])
    aspect_ratio: str = confirmed.get("aspect_ratio", "9:16")
    visual_mode: str = confirmed.get("visual_mode", "social")

    product_bytes: bytes | None = None
    if confirmed.get("product_image_b64"):
        try:
            product_bytes = base64.b64decode(confirmed["product_image_b64"])
        except Exception as exc:
            yield _error_chunk(
                job_id, "images",
                f"Could not decode product image: {exc}"
            )
            return

    scenes_for_imagen = [
        {
            "scene_number": s.get("scene_number"),
            "scene_label": s.get("scene_label", f"scene_{s.get('scene_number')}"),
            "imagen_prompt": s.get("enhanced_prompt") or s.get("imagen_prompt", ""),
            "negative_prompt": s.get("negative_prompt", ""),
            "is_product_background": s.get("is_product_background", False),
        }
        for s in confirmed_scenes
    ]

    yield {
        "type": "status",
        "stage": "images",
        "job_id": job_id,
        "data": {
            "message": f"Generating {len(scenes_for_imagen)} images ({aspect_ratio})...",
            "visual_mode": visual_mode,
            "aspect_ratio": aspect_ratio,
        },
    }

    try:
        images = await generate_scene_images(
            scenes=scenes_for_imagen,
            job_id=job_id,
            product_image_bytes=product_bytes,
            aspect_ratio=aspect_ratio,
        )
    except Exception as exc:
        # This only fires if generate_scene_images itself crashes entirely.
        # Per-scene errors are handled inside generate_scene_images and
        # returned as error fields in the scene dict — they don't raise.
        yield _error_chunk(
            job_id, "images",
            f"Image generation failed — please try again. ({type(exc).__name__})"
        )
        return

    try:
        update_job(job_id, "images", {"scenes": images})
        update_job(job_id, "status", "complete")
    except Exception:
        # Redis write failure — don't block the response, just log
        print(f"[render_generate] Warning: could not update job {job_id} in Redis")

    yield {
        "type": "images",
        "stage": "images",
        "job_id": job_id,
        "data": {
            "scenes": images,
            "visual_mode": visual_mode,
            "aspect_ratio": aspect_ratio,
        },
    }

    yield {
        "type": "complete",
        "stage": "done",
        "job_id": job_id,
        "data": {"message": "Your content package is ready."},
    }