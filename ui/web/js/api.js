// Controller API (docs/08). Same origin; the session cookie is sent automatically.
export class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}
let onAuth = () => {};
export function onUnauthorized(f) { onAuth = f; }

export async function api(method, path, body) {
  const r = await fetch(path, {
    method, credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) {
    if (r.status === 401 && path !== "/api/login") onAuth();
    const d = data && data.detail;
    throw new ApiError(r.status, typeof d === "string" ? d : JSON.stringify(d || r.statusText));
  }
  return data;
}
export const get = (p) => api("GET", p);
export const post = (p, b) => api("POST", p, b === undefined ? {} : b);
