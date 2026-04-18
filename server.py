"""MCP Nano Banana - Image generation MCP server for Google Gemini image models.

Single tool: generate_image. No other Gemini endpoints are exposed.
The Gemini API key is received per-request, either as:
  Authorization: Bearer <GEMINI_API_KEY>
  X-Gemini-Api-Key: <GEMINI_API_KEY>

Images are saved to disk and served over HTTPS at /images/<id>.<ext>.
The response contains both a download URL (text) and the image bytes (image content),
so the calling LLM can both hand the URL to the user and reuse the image for further work.
"""
from __future__ import annotations

import base64
import contextvars
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent, TextContent
from pydantic import Field
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from typing_extensions import Annotated

# ---------- Configuration ----------

LOG = logging.getLogger("mcp-nano-banana")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

IMAGES_DIR = Path(os.getenv("IMAGES_DIR", "/app/images"))
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://mcp-nano-banana.mobiweb.pt").rstrip("/")
IMAGE_TTL_HOURS = int(os.getenv("IMAGE_TTL_HOURS", "24"))
REQUEST_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "180"))

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Economic default. Only use premium on explicit user request.
ECONOMIC_MODEL = "gemini-2.5-flash-image"
PREMIUM_MODEL = "gemini-3-pro-image-preview"

VALID_ASPECT_RATIOS = [
    "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9",
]

# ContextVar populated by the Starlette middleware per request.
_api_key_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("gemini_api_key", default=None)


# ---------- Gemini client ----------

async def call_gemini(
    api_key: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    number_of_images: int,
    image_size: str | None,
) -> dict:
    """Call Gemini generateContent for an image model and return the parsed JSON."""
    url = f"{GEMINI_BASE}/{model}:generateContent?key={api_key}"

    image_config: dict = {"aspectRatio": aspect_ratio}
    if image_size and model == PREMIUM_MODEL:
        image_config["imageSize"] = image_size

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "candidateCount": number_of_images,
            "imageConfig": image_config,
        },
    }

    LOG.info("Calling Gemini model=%s ar=%s size=%s n=%s", model, aspect_ratio, image_size, number_of_images)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
    if r.status_code != 200:
        snippet = r.text[:800]
        raise RuntimeError(f"Gemini API HTTP {r.status_code}: {snippet}")
    return r.json()


# ---------- Cleanup of expired images ----------

def cleanup_expired_images() -> int:
    """Delete images older than IMAGE_TTL_HOURS. Returns count deleted."""
    now = time.time()
    cutoff = now - IMAGE_TTL_HOURS * 3600
    deleted = 0
    for p in IMAGES_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except FileNotFoundError:
            pass
    if deleted:
        LOG.info("Cleaned %s expired images", deleted)
    return deleted


# ---------- MCP server ----------

# DNS-rebinding protection: the MCP SDK rejects Host headers that are not in its
# allowlist. Nginx already enforces server_name, so we just need to whitelist
# our public hostname (comma-separated list via env var, defaults include
# mcp-nano-banana.mobiweb.pt and localhost for local testing).
_allowed_hosts_env = os.getenv(
    "ALLOWED_HOSTS",
    "mcp-nano-banana.mobiweb.pt,localhost,127.0.0.1",
)
_allowed_hosts = [h.strip() for h in _allowed_hosts_env.split(",") if h.strip()]

mcp = FastMCP(
    "nano-banana",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
        allowed_origins=["*"],
    ),
)


@mcp.tool(
    name="generate_image",
    description=(
        "Generate one or more images from a text prompt using Google's Gemini image models "
        "(also known as 'Nano Banana').\n\n"
        "MODEL SELECTION RULES (read carefully):\n"
        "  - By default, leave `model` unset or set it to 'gemini-2.5-flash-image' (Nano Banana 1). "
        "This is the economic default and should be used for normal requests.\n"
        "  - Only set `model='gemini-3-pro-image-preview'` (Nano Banana 2) when the user explicitly "
        "asks for the highest quality, premium output, 'Nano Banana 2', 'Gemini 3 Pro Image', "
        "2K/4K resolution, or equivalent wording. Do NOT upgrade silently - it costs ~3.4x more.\n\n"
        "REQUIRED PARAMETER: `aspect_ratio`. If the user did not specify an aspect ratio or intended "
        "use (square, portrait, landscape, wallpaper, story, etc.), ASK THE USER before calling this "
        "tool. Do not guess. Valid values: 1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 21:9.\n\n"
        "The tool returns: (1) a download URL valid for 24h and (2) the image bytes inline so they "
        "can be reused in subsequent tool calls (unless include_image_in_response=False).\n\n"
        "The Gemini API key must be supplied by the client via the Authorization: Bearer header or "
        "the X-Gemini-Api-Key header. In Claude connectors, paste the key as the OAuth Client Secret."
    ),
)
async def generate_image(
    prompt: Annotated[str, Field(description="Text description of the image to generate.", min_length=1)],
    aspect_ratio: Annotated[
        Literal["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"],
        Field(description="Aspect ratio of the output image. REQUIRED - ask the user if not specified."),
    ],
    model: Annotated[
        Literal["gemini-2.5-flash-image", "gemini-3-pro-image-preview"],
        Field(
            description=(
                "Image model. Default 'gemini-2.5-flash-image' (economic). "
                "Only use 'gemini-3-pro-image-preview' on explicit user request for premium quality."
            )
        ),
    ] = ECONOMIC_MODEL,
    image_size: Annotated[
        Literal["1K", "2K", "4K"] | None,
        Field(
            description=(
                "Output resolution. Only applies to gemini-3-pro-image-preview. "
                "Ignore for the economic model (always 1K). Default None -> 1K."
            )
        ),
    ] = None,
    number_of_images: Annotated[
        int, Field(ge=1, le=4, description="Number of image variants to generate (1-4).")
    ] = 1,
    include_image_in_response: Annotated[
        bool,
        Field(
            description=(
                "If True (default), return image bytes inline so the LLM can see and reuse them. "
                "If False, return only the download URL (saves context tokens, but the LLM cannot see the image)."
            )
        ),
    ] = True,
) -> list:
    api_key = _api_key_var.get()
    if not api_key:
        return [
            TextContent(
                type="text",
                text=(
                    "ERROR: No Gemini API key found in the request. "
                    "Send it as 'Authorization: Bearer <GEMINI_API_KEY>' or 'X-Gemini-Api-Key: <GEMINI_API_KEY>'. "
                    "In Claude connectors, paste the key as the OAuth Client Secret."
                ),
            )
        ]

    try:
        resp = await call_gemini(
            api_key=api_key,
            model=model,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            number_of_images=number_of_images,
            image_size=image_size,
        )
    except Exception as exc:
        LOG.exception("Gemini call failed")
        return [TextContent(type="text", text=f"Gemini API call failed: {exc}")]

    cleanup_expired_images()

    results: list = []
    urls: list[str] = []
    for cand in resp.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if not inline or "data" not in inline:
                continue
            b64 = inline["data"]
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/jpeg"
            ext = "png" if "png" in mime else ("jpg" if "jpeg" in mime else "bin")
            img_id = uuid.uuid4().hex
            fname = f"{img_id}.{ext}"
            out_path = IMAGES_DIR / fname
            out_path.write_bytes(base64.b64decode(b64))
            public_url = f"{PUBLIC_BASE_URL}/images/{fname}"
            urls.append(public_url)
            if include_image_in_response:
                results.append(ImageContent(type="image", data=b64, mimeType=mime))

    if not urls:
        raw = json.dumps(resp)[:700]
        return [TextContent(type="text", text=f"Gemini returned no image. Raw response: {raw}")]

    lines = [
        f"Generated {len(urls)} image(s) with model={model}, aspect_ratio={aspect_ratio}"
        + (f", image_size={image_size}" if image_size else "")
        + ".",
        "Download links (valid for {h}h):".format(h=IMAGE_TTL_HOURS),
    ]
    for i, u in enumerate(urls, 1):
        lines.append(f"  {i}. {u}")

    results.insert(0, TextContent(type="text", text="\n".join(lines)))
    return results


# ---------- Starlette glue (middleware + static images + health) ----------

class BearerExtractorMiddleware(BaseHTTPMiddleware):
    """Extract the Gemini API key from the incoming HTTP request and store it in a ContextVar."""

    async def dispatch(self, request: Request, call_next):
        token: str | None = None
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip() or None
        if not token:
            gk = request.headers.get("x-gemini-api-key")
            if gk:
                token = gk.strip() or None
        # Allow non-MCP routes (health, images) to pass through without a key.
        if token:
            _api_key_var.set(token)
        return await call_next(request)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mcp-nano-banana"})


async def image_handler(request: Request) -> Response:
    fname = request.path_params["fname"]
    # Path-traversal guard.
    if "/" in fname or ".." in fname or not fname:
        return Response(status_code=400)
    p = IMAGES_DIR / fname
    if not p.exists() or not p.is_file():
        return Response(status_code=404)
    age = datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)
    if age > timedelta(hours=IMAGE_TTL_HOURS):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return Response(status_code=410, content=b"Image expired")
    media_type = "image/png" if p.suffix == ".png" else "image/jpeg"
    return FileResponse(p, media_type=media_type, filename=fname)


# The FastMCP streamable-http app exposes /mcp for JSON-RPC + SSE.
mcp_app = mcp.streamable_http_app()

app = Starlette(
    debug=False,
    routes=[
        Route("/health", health),
        Route("/images/{fname}", image_handler),
        Mount("/", app=mcp_app),
    ],
    middleware=[Middleware(BearerExtractorMiddleware)],
    # Propagate FastMCP lifespan so the session manager's task group is initialized.
    lifespan=mcp_app.router.lifespan_context,
)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "3000"))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("LOG_LEVEL", "info").lower())
