"""Gateway — FastAPI routes and WebSocket handlers for Contentr.

Three WebSocket endpoints:
  /ws/discover  — topic discovery (single message in, stream out)
  /ws/generate  — intel + captions (single message in, stream out)
  /ws/render    — two-phase: enhance prompts then generate images
                  (two messages in on the same connection, stream out)

One HTTP endpoint:
  POST /style/{user_id}       — save style memory
  GET  /personalities         — list all preset personalities

WebSocket CORS:
  FastAPI's CORSMiddleware does NOT apply to WebSocket upgrades.
  We check the Origin header manually in _check_ws_origin().
  Connections from disallowed origins are closed immediately with
  code 1008 (Policy Violation) before any data is exchanged.

Cloud Run keepalive:
  Cloud Run's WebSocket idle timeout is ~60s by default. We send a
  JSON ping frame every 30 seconds so long-running pipelines
  (intel search + 4 Imagen calls can take 90–120s) don't get cut.
  The frontend ignores chunks with type "ping".
"""

import asyncio
import json
import traceback
import uuid
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from agents.dispatch import run_discover, run_generate, run_render_enhance, run_render_generate
from services.redis_service import save_style
from services.personalities import get_personality_list

router = APIRouter()

# Origins that are allowed to open WebSocket connections.
# Must match ALLOWED_ORIGINS in main.py — kept in sync manually for now.
_ALLOWED_WS_ORIGINS = {
    "http://localhost:3000",
    "http://localhost:5173",
    "https://contentr.vercel.app",    # ← replace with your actual Vercel URL
}

_PING_INTERVAL = 30  # seconds between keepalive pings


# ── Origin guard ──────────────────────────────────────────────────────────────

async def _check_ws_origin(websocket: WebSocket) -> bool:
    """Return True if the connection origin is allowed, False otherwise.

    The browser always sends an Origin header on WebSocket upgrades.
    Non-browser clients (Postman, curl) send no Origin, which we allow
    so local testing and server-to-server calls still work.
    """
    origin = websocket.headers.get("origin")
    if origin is None:
        # Non-browser client — allow (Postman, server-to-server)
        return True
    return origin in _ALLOWED_WS_ORIGINS


# ── Keepalive ping ────────────────────────────────────────────────────────────

async def _keepalive(websocket: WebSocket, stop_event: asyncio.Event) -> None:
    """Send a ping frame every _PING_INTERVAL seconds until stop_event is set.

    Run this as a concurrent task alongside the pipeline so Cloud Run
    doesn't close the connection during long agent runs.
    """
    while not stop_event.is_set():
        try:
            await asyncio.sleep(_PING_INTERVAL)
            if not stop_event.is_set():
                await websocket.send_json({"type": "ping"})
        except Exception:
            break


# ── HTTP endpoints ────────────────────────────────────────────────────────────

@router.post("/style/{user_id}")
async def set_style(user_id: str, style: dict):
    """Save or update a creator style profile in Redis."""
    save_style(user_id, style)
    return {"status": "saved", "user_id": user_id}


@router.get("/personalities")
async def list_personalities():
    """Return all preset personality profiles for the frontend picker."""
    return {"personalities": get_personality_list()}


# ── /ws/discover ─────────────────────────────────────────────────────────────

@router.websocket("/ws/discover")
async def discover_socket(websocket: WebSocket):
    """Find 5 viral-worthy stories for a niche and platform.

    Client sends one DiscoverBrief JSON message.
    Server streams: status → topics
    """
    if not await _check_ws_origin(websocket):
        await websocket.close(code=1008, reason="Origin not allowed")
        return

    await websocket.accept()
    job_id = str(uuid.uuid4())
    stop_ping = asyncio.Event()

    try:
        raw = await websocket.receive_text()
        brief = json.loads(raw)

        ping_task = asyncio.create_task(_keepalive(websocket, stop_ping))

        async for chunk in run_discover(brief, job_id):
            await websocket.send_json(chunk)

    except WebSocketDisconnect:
        print(f"[discover] client disconnected — job {job_id}")
    except json.JSONDecodeError as exc:
        _log_error("discover", job_id, exc)
        await _try_send_error(websocket, job_id, "Invalid JSON in request body")
    except ValueError as exc:
        # Agent returned unparseable output
        _log_error("discover", job_id, exc)
        await _try_send_error(websocket, job_id, str(exc))
    except Exception as exc:
        _log_error("discover", job_id, exc)
        await _try_send_error(websocket, job_id, f"Unexpected error: {type(exc).__name__}")
    finally:
        stop_ping.set()
        ping_task.cancel()


# ── /ws/generate ─────────────────────────────────────────────────────────────

@router.websocket("/ws/generate")
async def generate_socket(websocket: WebSocket):
    """Run intel research and caption generation.

    Client sends one GenerateBrief JSON message.
    Server streams: status → intel → status → captions
    """
    if not await _check_ws_origin(websocket):
        await websocket.close(code=1008, reason="Origin not allowed")
        return

    await websocket.accept()
    job_id = str(uuid.uuid4())
    stop_ping = asyncio.Event()
    ping_task = None

    try:
        raw = await websocket.receive_text()
        brief = json.loads(raw)

        ping_task = asyncio.create_task(_keepalive(websocket, stop_ping))

        async for chunk in run_generate(brief, job_id):
            await websocket.send_json(chunk)

    except WebSocketDisconnect:
        print(f"[generate] client disconnected — job {job_id}")
    except json.JSONDecodeError as exc:
        _log_error("generate", job_id, exc)
        await _try_send_error(websocket, job_id, "Invalid JSON in request body")
    except ValueError as exc:
        _log_error("generate", job_id, exc)
        await _try_send_error(websocket, job_id, str(exc))
    except Exception as exc:
        _log_error("generate", job_id, exc)
        await _try_send_error(websocket, job_id, f"Unexpected error: {type(exc).__name__}")
    finally:
        stop_ping.set()
        if ping_task:
            ping_task.cancel()


# ── /ws/render ───────────────────────────────────────────────────────────────

@router.websocket("/ws/render")
async def render_socket(websocket: WebSocket):
    """Two-phase render: enhance prompts then generate images.

    PHASE 1 — client sends RenderEnhanceBrief:
      Server streams: status → status → enhanced_prompts
      Server then WAITS (blocking receive) — do NOT close connection.

    PHASE 2 — client sends ConfirmedRenderBrief on the SAME connection:
      Server streams: status → images → complete

    If the client closes the connection between phases, the server
    catches WebSocketDisconnect and exits cleanly without crashing.
    """
    if not await _check_ws_origin(websocket):
        await websocket.close(code=1008, reason="Origin not allowed")
        return

    await websocket.accept()
    job_id = str(uuid.uuid4())
    stop_ping = asyncio.Event()
    ping_task = None

    try:
        # ── Phase 1: enhance prompts ──────────────────────────────────────────
        raw = await websocket.receive_text()
        enhance_brief = json.loads(raw)

        ping_task = asyncio.create_task(_keepalive(websocket, stop_ping))

        async for chunk in run_render_enhance(enhance_brief, job_id):
            await websocket.send_json(chunk)

        # Pipeline paused — waiting for user to confirm/edit prompts.
        # Keepalive ping continues ticking so Cloud Run doesn't close the socket.

        # ── Phase 2: generate images ──────────────────────────────────────────
        raw2 = await websocket.receive_text()
        confirmed_brief = json.loads(raw2)

        async for chunk in run_render_generate(confirmed_brief, job_id):
            await websocket.send_json(chunk)

    except WebSocketDisconnect:
        # Client closed between phase 1 and phase 2 (e.g. navigated away).
        # This is normal — just log it and exit cleanly.
        print(f"[render] client disconnected between phases — job {job_id}")
    except json.JSONDecodeError as exc:
        _log_error("render", job_id, exc)
        await _try_send_error(websocket, job_id, "Invalid JSON in request body")
    except ValueError as exc:
        _log_error("render", job_id, exc)
        await _try_send_error(websocket, job_id, str(exc))
    except Exception as exc:
        _log_error("render", job_id, exc)
        await _try_send_error(websocket, job_id, f"Unexpected error: {type(exc).__name__}")
    finally:
        stop_ping.set()
        if ping_task:
            ping_task.cancel()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log_error(endpoint: str, job_id: str, exc: Exception) -> None:
    print(f"[{endpoint}] ERROR — job {job_id}: {exc}")
    traceback.print_exc()


async def _try_send_error(
    websocket: WebSocket, job_id: str, message: str
) -> None:
    """Best-effort error chunk send. Swallows send failures gracefully."""
    try:
        await websocket.send_json({
            "type": "error",
            "job_id": job_id,
            "data": {"message": message},
        })
    except Exception:
        pass