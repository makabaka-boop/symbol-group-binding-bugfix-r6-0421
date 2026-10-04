// 后端 API 封装。所有写操作都携带/返回 revision。
const BASE = "";

async function jsonOrThrow(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || `请求失败 ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

export const api = {
  listDocuments: () => fetch(`${BASE}/api/documents`).then(jsonOrThrow),
  createDocument: (title, content = "") =>
    fetch(`${BASE}/api/documents`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, content }),
    }).then(jsonOrThrow),
  getDocument: (id) => fetch(`${BASE}/api/documents/${id}`).then(jsonOrThrow),
  replaceDocument: (id, content, expectedRevision) =>
    fetch(`${BASE}/api/documents/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, expectedRevision }),
    }).then(jsonOrThrow),
  analyze: (id, content, revision) =>
    fetch(`${BASE}/api/documents/${id}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, revision }),
    }).then(jsonOrThrow),
  diagnostics: (id) =>
    fetch(`${BASE}/api/documents/${id}/diagnostics`).then(jsonOrThrow),
  renamePreview: (oldName, newName) =>
    fetch(`${BASE}/api/rename/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ oldName, newName }),
    }).then(jsonOrThrow),
  renameCommit: (planId) =>
    fetch(`${BASE}/api/rename/commit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ planId }),
    }).then(jsonOrThrow),
};

// 打开 WebSocket，自动重连。返回一个带 close 的句柄。
export function connectWorkspace(onMessage) {
  let ws;
  let closed = false;
  let retryTimer = null;

  const url = (() => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws`;
  })();

  const open = () => {
    ws = new WebSocket(url);
    ws.onmessage = (ev) => {
      try {
        onMessage(JSON.parse(ev.data));
      } catch {
        /* 忽略无法解析的帧 */
      }
    };
    ws.onclose = () => {
      if (!closed) retryTimer = setTimeout(open, 1000);
    };
    ws.onerror = () => ws && ws.close();
  };
  open();

  return {
    close() {
      closed = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (ws) ws.close();
    },
  };
}
