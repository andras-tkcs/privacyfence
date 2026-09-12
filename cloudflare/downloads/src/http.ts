/** Small HTTP response helpers shared by index.ts's route handlers. */

export function jsonResponse(body: unknown, status = 200, headers?: HeadersInit): Response {
  const responseHeaders = new Headers(headers);
  responseHeaders.set("Content-Type", "application/json");
  return new Response(JSON.stringify(body), { status, headers: responseHeaders });
}

export function notFound(message: string): Response {
  return jsonResponse({ error: message }, 404);
}

export function methodNotAllowed(allowed: readonly string[]): Response {
  return jsonResponse({ error: "method not allowed" }, 405, { Allow: allowed.join(", ") });
}

// Only /api/* routes ever need browser CORS -- download routes are hit by <a>/<img>-style
// navigation, never fetch()'d cross-origin (docs/release-publishing-kpi-plan.md Phase 1).
const ALLOWED_ORIGINS = new Set(["https://privacyfence.eu", "https://www.privacyfence.eu"]);

function allowedOrigin(request: Request): string | null {
  const origin = request.headers.get("Origin");
  return origin && ALLOWED_ORIGINS.has(origin) ? origin : null;
}

/** Wraps an /api/* response with CORS headers when the request's Origin is allow-listed. */
export function withCors(request: Request, response: Response): Response {
  const origin = allowedOrigin(request);
  if (!origin) return response;
  const headers = new Headers(response.headers);
  headers.set("Access-Control-Allow-Origin", origin);
  headers.append("Vary", "Origin");
  return new Response(response.body, { status: response.status, headers });
}

/** Handles a CORS preflight `OPTIONS` request against an /api/* route. */
export function corsPreflight(request: Request): Response {
  const origin = allowedOrigin(request);
  const headers = new Headers();
  if (origin) {
    headers.set("Access-Control-Allow-Origin", origin);
    headers.append("Vary", "Origin");
    headers.set("Access-Control-Allow-Methods", "GET, OPTIONS");
    headers.set("Access-Control-Allow-Headers", "Content-Type");
  }
  return new Response(null, { status: 204, headers });
}
