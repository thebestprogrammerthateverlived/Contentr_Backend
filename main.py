import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from gateway import router as gateway_router
from services.personalities import load_all_personalities


# ── Startup / shutdown ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Seed personality presets into Redis on startup."""
    load_all_personalities()
    yield
    # shutdown logic here if needed


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Contentr",
    description="Real-time AI content generation platform",
    version="1.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# NOTE: CORSMiddleware covers HTTP only (POST /style, GET /health, GET /personalities).
# WebSocket origin checking is handled separately in gateway.py because
# FastAPI's CORS middleware does NOT apply to WebSocket connections.
# The browser still sends an Origin header on WS upgrades — we check it manually.

ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",          # Vite default dev port
    "https://contentr.vercel.app",    # ← replace with your actual Vercel URL
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(gateway_router)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)