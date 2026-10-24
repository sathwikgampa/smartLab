// REST client for the SmartLab AI Flask API. Every call rejects with an Error whose
// message is the server's JSON "error" field when available, and whose `status`
// is the HTTP status (0 when the server could not be reached).

const BASE = import.meta.env.VITE_API_BASE ?? "";
export const UNREACHABLE = "Couldn't reach the server. Please try again.";

// Network-level failures are broadcast so the shell can update its connection indicator.
const broadcast = (reachable) => window.dispatchEvent(new CustomEvent("smartlab:connection", { detail: { reachable } }));
// A 401 from a protected endpoint means the session ended; the app sends the user to log in.
const signedOut = (path) => { if (!path.startsWith("/api/auth/")) window.dispatchEvent(new Event("smartlab:unauthorized")); };

function apiError(message, status, data) {
  const error = new Error(message);
  error.status = status;
  error.data = data;
  return error;
}

async function request(path, { method = "GET", body, signal, quiet = false } = {}) {
  const init = { method, signal, headers: {}, credentials: "include" };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(`${BASE}${path}`, init);
  } catch (err) {
    if (err.name === "AbortError") throw err;
    if (!quiet) broadcast(false);
    throw apiError(UNREACHABLE, 0);
  }
  if (res.status === 204) return null;
  const isJson = res.headers.get("content-type")?.includes("application/json");
  const data = isJson ? await res.json() : await res.text();
  // A gateway/proxy error without the API's JSON body means the backend itself is down.
  if (!res.ok && !isJson && res.status >= 500) { if (!quiet) broadcast(false); throw apiError(UNREACHABLE, 0); }
  if (res.status === 401) signedOut(path);
  if (!res.ok) throw apiError((isJson && data?.error) || `Request failed (${res.status})`, res.status, data);
  return data;
}

/**
 * Multipart upload with progress. `onProgress(fraction)` reports the *transfer*;
 * the promise only resolves once the server has finished processing the file.
 */
function upload(path, fields, { onProgress, signal } = {}) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    Object.entries(fields).forEach(([k, v]) => v !== undefined && v !== null && v !== "" && form.append(k, v));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}${path}`);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* non-JSON body */ }
      if (xhr.status === 401) signedOut(path);
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else if (xhr.status >= 500 && data === null) { broadcast(false); reject(apiError(UNREACHABLE, 0)); }
      else reject(apiError(data?.error || `Upload failed (${xhr.status})`, xhr.status, data));
    };
    xhr.onerror = () => { broadcast(false); reject(apiError(UNREACHABLE, 0)); };
    xhr.onabort = () => reject(Object.assign(new Error("Upload cancelled."), { name: "AbortError" }));
    signal?.addEventListener("abort", () => xhr.abort());
    xhr.send(form);
  });
}

/**
 * POST a JSON body to a Server-Sent Events endpoint. Each `trace` event is passed to
 * `onEvent(name, payload)` as it arrives; the promise resolves with the final `result`
 * payload and rejects on an `error` event (the server sends a generic message only).
 */
async function stream(path, body, { onEvent, signal } = {}) {
  let res;
  try {
    res = await fetch(`${BASE}${path}`, {
      method: "POST", signal, body: JSON.stringify(body),
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    broadcast(false);
    throw apiError(UNREACHABLE, 0);
  }
  if (!res.ok) {
    const isJson = res.headers.get("content-type")?.includes("application/json");
    const data = isJson ? await res.json() : null;
    if (!isJson && res.status >= 500) { broadcast(false); throw apiError(UNREACHABLE, 0); }
    throw apiError(data?.error || `Request failed (${res.status})`, res.status, data);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let cut;
    while ((cut = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message";
      const lines = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) lines.push(line.slice(5).trimStart());
      }
      if (!lines.length) continue; // comment or keep-alive
      const payload = JSON.parse(lines.join("\n"));
      if (event === "error") throw apiError(payload.error || "The co-pilot could not complete this request.", 500, payload);
      if (event === "result") result = payload;
      else onEvent?.(event, payload);
    }
  }
  if (!result) throw apiError("The response ended before the answer arrived. Please try again.", 0);
  return result;
}

export const qs = (params) => {
  const s = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (Array.isArray(v)) v.forEach((x) => s.append(k, x));
    else if (v !== undefined && v !== null && v !== "") s.append(k, v);
  });
  const str = s.toString();
  return str ? `?${str}` : "";
};

export const api = {
  // Background status check: failures update the indicator only, never a toast.
  health: () => request("/api/health", { quiet: true }),

  // Authentication (session cookie set by the server; credentials never stored client-side)
  me: () => request("/api/auth/me", { quiet: true }),
  login: (email, password) => request("/api/auth/login", { method: "POST", body: { email, password } }),
  signup: (payload) => request("/api/auth/signup", { method: "POST", body: payload }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  forgotPassword: (email) => request("/api/auth/forgot-password", { method: "POST", body: { email } }),
  resetPassword: (token, password) => request("/api/auth/reset-password", { method: "POST", body: { token, password } }),

  // Documents
  documents: (params = {}) => request(`/api/documents${qs(params)}`),
  documentFacets: () => request("/api/documents/facets"),
  document: (id) => request(`/api/documents/${id}`),
  uploadDocument: (file, meta = {}, opts) => upload("/api/documents", { file, ...meta }, opts),
  updateDocument: (id, payload) => request(`/api/documents/${id}`, { method: "PATCH", body: payload }),
  reindexDocument: (id) => request(`/api/documents/${id}/reindex`, { method: "POST" }),
  deleteDocument: (id) => request(`/api/documents/${id}`, { method: "DELETE" }),
  pageText: (id, page) => request(`/api/documents/${id}/pages/${page}`),
  pagePreviewUrl: (id, page, highlights = [], zoom = 150) =>
    `${BASE}/api/documents/${id}/pages/${page}/preview${qs({ highlight: highlights, zoom })}`,
  documentFileUrl: (id) => `${BASE}/api/documents/${id}/file`,

  // Conversations
  chat: (question, sessionId, documentIds, extra = {}) =>
    request("/api/chat", { method: "POST", body: { question, session_id: sessionId, document_ids: documentIds, ...extra } }),
  // Lab co-pilot: the same run as chat(), streamed. extra = { upload_id, error_code }.
  chatStream: (question, sessionId, documentIds, extra = {}, opts) =>
    stream("/api/chat/stream", { question, session_id: sessionId, document_ids: documentIds, ...extra }, opts),
  agentUpload: (image, opts) => upload("/api/agent/uploads", { image }, opts),
  agentInfo: () => request("/api/agent/info"),
  agentRuns: (limit = 50) => request(`/api/agent/runs${qs({ limit })}`),
  sessions: (limit = 20) => request(`/api/chat/sessions${qs({ limit })}`),
  session: (id) => request(`/api/chat/${encodeURIComponent(id)}`),
  deleteSession: (id) => request(`/api/chat/${encodeURIComponent(id)}`, { method: "DELETE" }),
  recordExchange: (sessionId, payload) =>
    request(`/api/chat/${encodeURIComponent(sessionId)}/messages`, { method: "POST", body: payload }),
  feedback: (messageId, rating, comment) =>
    request(`/api/messages/${messageId}/feedback`, { method: "POST", body: { rating, comment } }),
  feedbackQueue: (status = "open") => request(`/api/feedback${qs({ status })}`),
  resolveFeedback: (id, resolution) => request(`/api/feedback/${id}/resolve`, { method: "POST", body: { resolution } }),

  // Search & vision
  unifiedSearch: (q) => request(`/api/search/unified${qs({ q })}`),
  analyzeImage: (image, note, opts) => upload("/api/vision/analyze", { image, note }, opts),

  // Reagents & troubleshooting
  reagents: (params = {}) => request(`/api/reagents${qs(params)}`),
  reagent: (id) => request(`/api/reagents/${id}`),
  reagentCategories: () => request("/api/reagents/categories"),
  reagentSummary: () => request("/api/reagents/summary"),
  reagentQuery: (question) => request("/api/reagents/query", { method: "POST", body: { question } }),
  createReagent: (payload) => request("/api/reagents", { method: "POST", body: payload }),
  updateReagent: (id, payload) => request(`/api/reagents/${id}`, { method: "PATCH", body: payload }),
  adjustReagent: (id, delta) => request(`/api/reagents/${id}/adjust`, { method: "POST", body: { delta } }),
  deleteReagent: (id) => request(`/api/reagents/${id}`, { method: "DELETE" }),
  troubleshooting: () => request("/api/troubleshooting"),
  matchTroubleshooting: (query) => request("/api/troubleshooting/match", { method: "POST", body: { query } }),

  // Safety, settings, activity
  safety: () => request("/api/safety"),
  safetySearch: (q) => request(`/api/safety/search${qs({ q })}`),
  settings: () => request("/api/settings"),
  updateSettings: (payload) => request("/api/settings", { method: "PUT", body: payload }),
  // mine=false (the whole lab's activity) is allowed for administrators only.
  activity: (since, { mine = true } = {}) => request(`/api/activity${qs({ since, mine })}`),

  // Literature
  literature: (q, params = {}) => request(`/api/literature/search${qs({ q, ...params })}`),

  // Experiments
  experiments: () => request("/api/experiments"),
  experiment: (id) => request(`/api/experiments/${id}`),
  createExperiment: (payload) => request("/api/experiments", { method: "POST", body: payload }),
  updateExperiment: (id, payload) => request(`/api/experiments/${id}`, { method: "PATCH", body: payload }),
  addStep: (id, text) => request(`/api/experiments/${id}/steps`, { method: "POST", body: { text } }),
  updateStep: (id, stepId, payload) => request(`/api/experiments/${id}/steps/${stepId}`, { method: "PATCH", body: payload }),
  deleteStep: (id, stepId) => request(`/api/experiments/${id}/steps/${stepId}`, { method: "DELETE" }),
  reorderSteps: (id, stepIds) => request(`/api/experiments/${id}/steps/order`, { method: "PUT", body: { step_ids: stepIds } }),
  logVariable: (id, payload) => request(`/api/experiments/${id}/variables`, { method: "POST", body: payload }),
  requestReview: (id, payload) => request(`/api/experiments/${id}/reviews`, { method: "POST", body: payload }),
  decideReview: (id, reviewId, payload) =>
    request(`/api/experiments/${id}/reviews/${reviewId}/decision`, { method: "POST", body: payload }),

  // Data
  datasets: () => request("/api/datasets"),
  dataset: (id, params = {}) => request(`/api/datasets/${id}${qs(params)}`),
  uploadDataset: (file, name) => upload("/api/datasets", { file, name }),
  deleteDataset: (id) => request(`/api/datasets/${id}`, { method: "DELETE" }),

  // Collaboration
  projects: () => request("/api/projects"),
  createProject: (payload) => request("/api/projects", { method: "POST", body: payload }),
  comments: (type, id) => request(`/api/comments/${type}/${id}`),
  addComment: (type, id, payload) => request(`/api/comments/${type}/${id}`, { method: "POST", body: payload }),
  projectFiles: (id) => request(`/api/projects/${id}/files`),
  uploadProjectFile: (id, file, uploadedBy) => upload(`/api/projects/${id}/files`, { file, uploaded_by: uploadedBy }),
  fileUrl: (id) => `${BASE}/api/files/${id}`,

  // SmartLab Visualizer (scientific image generation)
  visualizerInfo: () => request("/api/visualizer/info"),
  generateImage: (payload, opts = {}) => request("/api/visualizer/generate", { method: "POST", body: payload, ...opts }),
  generatedImages: (params = {}) => request(`/api/visualizer/images${qs(params)}`),
  generatedImageUrl: (id, { download = false } = {}) => `${BASE}/api/visualizer/images/${id}/file${download ? "?download=1" : ""}`,
  saveImageToProject: (id, projectId) =>
    request(`/api/visualizer/images/${id}/save-to-project`, { method: "POST", body: { project_id: projectId } }),
  attachImageToExperiment: (id, experimentId) =>
    request(`/api/visualizer/images/${id}/attach-to-experiment`, { method: "POST", body: { experiment_id: experimentId } }),

  // Simulation, profiles, training
  simulations: () => request("/api/simulations"),
  runSimulation: (model, params) => request(`/api/simulations/${model}`, { method: "POST", body: { params } }),
  profiles: () => request("/api/profiles"),
  profile: (id) => request(`/api/profiles/${id}`),
  equipment: () => request("/api/equipment"),
  equipmentItem: (id) => request(`/api/equipment/${id}`),
};
