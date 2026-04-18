# mcp-nano-banana

Model Context Protocol (MCP) server that exposes a single tool, `generate_image`, backed by Google Gemini image models (also known as "Nano Banana"). The server is deliberately minimal. It does not expose any other Gemini endpoint. Only image generation.

Production endpoint: `https://mcp-nano-banana.mobiweb.pt/mcp`

## Design rules

1. Single purpose. one tool, `generate_image`. No text generation, no embedding, no chat.
2. Economic by default. when the calling LLM does not specify a model, it uses `gemini-2.5-flash-image` (Nano Banana 1, ~$0.039/image). The premium model `gemini-3-pro-image-preview` (Nano Banana 2, ~$0.134/image) is only used when explicitly requested.
3. Required aspect ratio. the tool description instructs the LLM to ask the user for an aspect ratio if none was given, rather than guessing.
4. Per-request API key. the server holds no Gemini credentials. Each request must carry a valid bearer. Two modes are supported: (a) OAuth 2.1 flow (RFC 8414 + RFC 7591 + PKCE S256) where the user pastes the Gemini key on the `/authorize` HTML page and the issued `access_token` is the key itself; (b) direct header `Authorization: Bearer <GEMINI_API_KEY>` or `X-Gemini-Api-Key: <GEMINI_API_KEY>` for CLI / curl usage.
5. Returns both URL and bytes. images are written to disk and served over HTTPS for 24h. The tool response contains a short markdown block with the download URL and (optionally) the image bytes inline so the LLM can reuse them in subsequent steps.

## API

### Tool: `generate_image`

Arguments:

| name | type | required | default | notes |
|---|---|---|---|---|
| `prompt` | string | yes |  | Text description of the image. |
| `aspect_ratio` | enum | yes |  | One of `1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 21:9`. |
| `model` | enum | no | `gemini-2.5-flash-image` | `gemini-2.5-flash-image` or `gemini-3-pro-image-preview`. |
| `image_size` | enum\|null | no | null | `1K`, `2K` or `4K`. Only applied when model is the premium one. |
| `number_of_images` | int | no | 1 | 1..4. |
| `include_image_in_response` | bool | no | true | If false, returns only the URL. |

Response format: an MCP tool result with a `text` part (human-readable summary plus one URL per image) and, by default, one `image` part per generated image (base64 inline).

### Auth

Two accepted modes, `Authorization` header takes precedence over `X-Gemini-Api-Key`:

```
Authorization: Bearer <GEMINI_API_KEY>
X-Gemini-Api-Key: <GEMINI_API_KEY>
```

For MCP clients that only speak OAuth 2.1 (Claude Desktop / Claude Web custom connectors), the server implements a minimal Authorization Server. Flow:

1. Client fetches `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource`.
2. Client performs Dynamic Client Registration on `POST /register` (RFC 7591).
3. Client sends the browser to `GET /authorize` with PKCE `S256`. The server renders an HTML form asking for the Gemini API key.
4. User pastes the key, `POST /authorize` redirects with `?code=...`.
5. Client exchanges the code on `POST /token`; the returned `access_token` IS the Gemini API key the user pasted. No server-side persistence beyond the in-memory auth-code table.

### Installing as a Claude Desktop custom connector

1. Claude → Settings → Connectors → Add custom connector.
2. URL: `https://mcp-nano-banana.mobiweb.pt/mcp`. Leave Client ID and Client Secret empty.
3. Claude opens the `/authorize` page. Paste the Gemini API key and submit.
4. Return to the chat and use `generate_image`.

## Endpoints

| path | description |
|---|---|
| `POST /mcp` | MCP Streamable HTTP JSON-RPC. |
| `GET /mcp` | MCP Streamable HTTP SSE stream. |
| `GET /health` | Liveness probe. returns `{"status":"ok"}`. |
| `GET /images/{id}.{ext}` | Static file server for generated images. 410 after TTL expires. |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 AS metadata. |
| `GET /.well-known/oauth-protected-resource` | RFC 9728 RS metadata. |
| `POST /register` | RFC 7591 Dynamic Client Registration. |
| `GET /authorize` | Renders the HTML page where the user pastes the Gemini key. |
| `POST /authorize` | Issues the authorization code and 302-redirects. |
| `POST /token` | Exchanges code + PKCE verifier for `access_token` = user-supplied Gemini key. |

## Running locally

```
pip install -r requirements.txt
IMAGES_DIR=./images PUBLIC_BASE_URL=http://localhost:3000 python server.py
```

## Running in Docker

```
docker compose up -d --build
```

The container publishes `127.0.0.1:3002` by default and is meant to be reverse-proxied by nginx on the host. See `nginx-site.conf.example`.

## Environment variables

| name | default | purpose |
|---|---|---|
| `PORT` | `3000` | HTTP port inside the container. |
| `HOST` | `0.0.0.0` | Bind address. |
| `IMAGES_DIR` | `/app/images` | Where generated images are written. |
| `PUBLIC_BASE_URL` | `https://mcp-nano-banana.mobiweb.pt` | Used to build the public URLs returned in the tool response. |
| `IMAGE_TTL_HOURS` | `24` | How long generated images remain downloadable. |
| `GEMINI_TIMEOUT` | `180` | httpx timeout for the Gemini call, in seconds. |
| `LOG_LEVEL` | `INFO` |  |

## Security notes

1. No Gemini key is stored server-side. If someone compromises the container they gain no API access.
2. The server does not authenticate MCP callers beyond the bearer being a valid Gemini key. Anyone with a working Gemini key can call the endpoint. Add a layer (nginx basic auth, IP allowlist, or a separate gateway token) if you want tenant isolation.
3. Images are served publicly for 24h by UUID. The UUIDs so guessing is impractical, but the URLs are not private. Do not send prompts whose output you cannot afford to leak.

## License

Proprietary. Internal tooling for Mobiweb.
