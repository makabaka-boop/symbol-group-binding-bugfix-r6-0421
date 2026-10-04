import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, connectWorkspace } from "./api.js";
import DocEditor from "./DocEditor.jsx";
import RenameModal from "./RenameModal.jsx";
import "./styles.css";

// 每个打开文档的本地状态：
//   { id, title, serverText, serverRevision, localText, dirty,
//     conflict: null | { remoteRevision, reason }, diagnostics, diagRevision }
export default function App() {
  const [docs, setDocs] = useState([]); // 服务端文档清单（简要）
  const [openStates, setOpenStates] = useState({}); // id -> 本地编辑状态
  const [activeId, setActiveId] = useState(null);
  const [connected, setConnected] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameInitial, setRenameInitial] = useState("");
  const [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const editorsRef = useRef({}); // docId -> editor 实例
  const analyzeTimers = useRef({});

  // ---------- 初始加载 ----------
  useEffect(() => {
    api.listDocuments().then(async (res) => {
      setDocs(res.documents);
      if (res.documents[0]) {
        const full = await api.getDocument(res.documents[0].id);
        openDocFromServer(full, res.documents[0].id);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openDocFromServer = useCallback((full, selectId) => {
    const st = {
      id: full.id,
      title: full.title,
      serverText: full.content,
      serverRevision: full.revision,
      localText: full.content,
      dirty: false,
      conflict: null,
      diagnostics: [],
      diagRevision: full.revision,
    };
    setOpenStates((prev) => ({ ...prev, [full.id]: st }));
    if (selectId) setActiveId(selectId);
    // 拉取该修订的诊断
    api.diagnostics(full.id).then((d) => {
      if (d.revision === full.revision) {
        setOpenStates((prev) =>
          prev[full.id] && prev[full.id].serverRevision === d.revision
            ? {
                ...prev,
                [full.id]: {
                  ...prev[full.id],
                  diagnostics: d.diagnostics,
                  diagRevision: d.revision,
                },
              }
            : prev,
        );
      }
    });
  }, []);

  const openTab = async (id) => {
    setActiveId(id);
    if (!openStates[id]) {
      const full = await api.getDocument(id);
      openDocFromServer(full, null);
    }
  };

  // ---------- WebSocket：两个（多个）浏览器会话收通知 ----------
  useEffect(() => {
    const conn = connectWorkspace((msg) => {
      if (msg.type === "snapshot") {
        setDocs((prev) => mergeDocList(prev, msg.documents));
        setConnected(true);
      } else if (msg.type === "document_changed") {
        const brief = msg.document;
        setDocs((prev) => upsertBrief(prev, brief));
        handleRemoteChange(brief);
      } else if (msg.type === "diagnostics") {
        applyRemoteDiagnostics(msg.docId, msg.revision, msg.diagnostics);
      }
    });
    return () => conn.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleRemoteChange = useCallback((brief) => {
    setOpenStates((prev) => {
      const st = prev[brief.id];
      if (!st) return prev; // 未打开的文档只更新左侧清单即可
      if (brief.revision <= st.serverRevision) return prev;
      if (st.dirty) {
        // 本地有未提交文字：保留本地文字，标记冲突
        return {
          ...prev,
          [brief.id]: {
            ...st,
            conflict: {
              remoteRevision: brief.revision,
              reason: `另一会话已将文档保存为 r${brief.revision}，你本地有未提交修改`,
            },
          },
        };
      }
      // 本地干净：拉取新文本对齐
      api
        .getDocument(brief.id)
        .then((full) => {
          setOpenStates((p) => {
            const cur = p[brief.id];
            if (!cur || cur.dirty) return p; // 拉取期间用户开始输入则不覆盖
            return {
              ...p,
              [brief.id]: {
                ...cur,
                title: full.title,
                serverText: full.content,
                serverRevision: full.revision,
                localText: full.content,
                conflict: null,
              },
            };
          });
          return api.diagnostics(brief.id);
        })
        .then((d) => {
          if (!d) return;
          applyRemoteDiagnostics(d.docId, d.revision, d.diagnostics);
        });
      return prev;
    });
  }, []);

  const applyRemoteDiagnostics = useCallback((docId, revision, diagnostics) => {
    setOpenStates((prev) => {
      const st = prev[docId];
      if (!st) return prev;
      // 只接受不早于当前服务端修订的诊断；旧修订的异步结果不能覆盖新文本
      if (revision < st.serverRevision) return prev;
      if (st.dirty && revision === st.serverRevision) {
        // 本地正在编辑：诊断仍显示旧文本的（与 Monaco 所见不同），记录但不覆盖标记
        return {
          ...prev,
          [docId]: { ...st, diagnostics, diagRevision: revision },
        };
      }
      return {
        ...prev,
        [docId]: { ...st, diagnostics, diagRevision: revision },
      };
    });
  }, []);

  // ---------- 编辑：本地保存 + 触发绑定修订的异步分析 ----------
  const onLocalChange = (id, text) => {
    setOpenStates((prev) => {
      const st = prev[id];
      if (!st) return prev;
      const dirty = text !== st.serverText;
      return { ...prev, [id]: { ...st, localText: text, dirty } };
    });
    scheduleAnalyze(id, text);
  };

  const scheduleAnalyze = (id, text) => {
    if (analyzeTimers.current[id]) clearTimeout(analyzeTimers.current[id]);
    analyzeTimers.current[id] = setTimeout(async () => {
      const revision = lastStateRef.current[id]?.serverRevision;
      const res = await api.analyze(id, text, revision).catch(() => null);
      if (!res || res.stale) return; // 旧修订结果被后端丢弃
      setOpenStates((prev) => {
        const st = prev[id];
        if (!st || st.serverRevision !== res.revision) return prev;
        // 分析期间本地仍在改：过期草稿的诊断不上屏
        if (st.localText !== text) return prev;
        return {
          ...prev,
          [id]: {
            ...st,
            diagnostics: res.diagnostics,
            diagRevision: res.revision,
          },
        };
      });
    }, 350);
  };

  // 保留一份最新 state 镜像供定时器读取
  const lastStateRef = useRef({});
  useEffect(() => {
    lastStateRef.current = openStates;
  }, [openStates]);

  // ---------- 保存（乐观锁） ----------
  const save = async (id) => {
    const st = openStates[id];
    if (!st) return;
    try {
      const doc = await api.replaceDocument(
        id,
        st.localText,
        st.conflict ? null : st.serverRevision,
      );
      // 冲突状态下以服务器最新版本为准强推（用户显式“强制覆盖”）
      setDocs((prev) =>
        upsertBrief(prev, {
          id: doc.id,
          title: doc.title,
          revision: doc.revision,
          updatedAt: doc.updatedAt,
        }),
      );
      setOpenStates((prev) => ({
        ...prev,
        [id]: {
          ...prev[id],
          serverText: doc.content,
          serverRevision: doc.revision,
          localText: doc.content,
          dirty: false,
          conflict: null,
        },
      }));
      flash(`已保存为 r${doc.revision}`);
    } catch (e) {
      if (e.status === 409) {
        // 修订冲突：本地文字保留，进入冲突态，拉取远端信息
        const full = await api.getDocument(id);
        setDocs((prev) =>
          upsertBrief(prev, {
            id: full.id,
            title: full.title,
            revision: full.revision,
            updatedAt: full.updatedAt,
          }),
        );
        setOpenStates((prev) => ({
          ...prev,
          [id]: {
            ...prev[id],
            serverText: full.content,
            serverRevision: full.revision,
            dirty: true,
            conflict: {
              remoteRevision: full.revision,
              reason: `保存被拒：文档已在另一会话推进到 r${full.revision}。本地文字已保留，可对比后强制覆盖或放弃本地修改。`,
            },
          },
        }));
      } else {
        flash(`保存失败：${e.message}`);
      }
    }
  };

  const discardLocal = async (id) => {
    const full = await api.getDocument(id);
    setOpenStates((prev) => ({
      ...prev,
      [id]: {
        ...prev[id],
        serverText: full.content,
        serverRevision: full.revision,
        localText: full.content,
        dirty: false,
        conflict: null,
      },
    }));
  };

  // ---------- 新建文档（最多 20） ----------
  const createDoc = async () => {
    if (docs.length >= 20) {
      flash("工作区最多 20 个文档");
      return;
    }
    setCreating(true);
    try {
      const title = `doc${docs.length + 1}.mini`;
      const doc = await api.createDocument(
        title,
        "# 在这里写 def / use，# 后可写任意 Unicode（中文、emoji 🎉）\n",
      );
      setDocs((prev) => [
        ...prev,
        {
          id: doc.id,
          title: doc.title,
          revision: doc.revision,
          updatedAt: doc.updatedAt,
        },
      ]);
      await openDocFromServer(doc, doc.id);
    } finally {
      setCreating(false);
    }
  };

  // ---------- 重命名提交成功后的本地对齐 ----------
  const onRenameCommitted = useCallback(
    async (res, plan) => {
      setRenameOpen(false);
      flash(
        `重命名 ${plan.oldName} → ${plan.newName}：${res.documents.length} 个文档已在 r${res.revision} 原子提交`,
      );
      // 拉取每个被改文档
      for (const ref of res.documents) {
        const full = await api.getDocument(ref.id);
        setDocs((prev) =>
          upsertBrief(prev, {
            id: full.id,
            title: full.title,
            revision: full.revision,
            updatedAt: full.updatedAt,
          }),
        );
        setOpenStates((prev) => {
          const st = prev[full.id];
          if (!st) return prev;
          if (st.dirty) {
            // 本地有未提交文字：不能静默替换，标记冲突，保留本地文字
            return {
              ...prev,
              [full.id]: {
                ...st,
                serverText: full.content,
                serverRevision: full.revision,
                conflict: {
                  remoteRevision: full.revision,
                  reason: `跨文件重命名已将服务端版本推进到 r${full.revision}，你本地有未提交修改`,
                },
              },
            };
          }
          return {
            ...prev,
            [full.id]: {
              ...st,
              serverText: full.content,
              serverRevision: full.revision,
              localText: full.content,
              dirty: false,
              conflict: null,
            },
          };
        });
        const d = await api.diagnostics(full.id);
        applyRemoteDiagnostics(full.id, d.revision, d.diagnostics);
      }
    },
    [applyRemoteDiagnostics],
  );

  const startRenameFromCursor = () => {
    const ed = editorsRef.current[activeId];
    let name = "";
    if (ed) {
      const m = ed.getModel();
      const pos = ed.getPosition();
      const word = m.getWordAtPosition(pos);
      name = word?.word || "";
    }
    setRenameInitial(name);
    setRenameOpen(true);
  };

  const active = activeId ? openStates[activeId] : null;
  const errCount = useMemo(
    () =>
      active
        ? active.diagnostics.filter((d) => d.severity === "error").length
        : 0,
    [active],
  );

  return (
    <div className="app">
      <header className="topbar">
        <span className="logo">mini-lang 工作区</span>
        <span className={`conn ${connected ? "on" : "off"}`}>
          {connected ? "● 实时连接（多会话通知已开启）" : "○ 连接中…"}
        </span>
        <button onClick={startRenameFromCursor} disabled={!active}>
          跨文件重命名
          {active && getCursorWord(editorsRef.current[activeId])
            ? `（当前词：${getCursorWord(editorsRef.current[activeId])}）`
            : ""}
        </button>
        <button onClick={createDoc} disabled={creating || docs.length >= 20}>
          + 新建文档 {docs.length}/20
        </button>
      </header>

      <div className="body">
        <nav className="sidebar">
          {docs.map((d) => {
            const st = openStates[d.id];
            return (
              <div
                key={d.id}
                className={`doc-item ${activeId === d.id ? "active" : ""}`}
                onClick={() => openTab(d.id)}
                title={`${d.title} · r${d.revision}`}
              >
                <span className="doc-title">{d.title}</span>
                <span className="doc-rev">r{d.revision}</span>
                {st?.dirty && (
                  <span className="dot dirty" title="有未提交修改" />
                )}
                {st?.conflict && (
                  <span className="dot conflict" title="存在冲突" />
                )}
              </div>
            );
          })}
          {docs.length === 0 && <div className="empty-hint">还没有文档</div>}
        </nav>

        <main className="editor-pane">
          {active ? (
            <>
              <div className="doc-head">
                <strong>{active.title}</strong>
                <span className="rev-tag">
                  本地草稿 · 服务端 r{active.serverRevision}
                  {active.dirty ? " · 未保存" : " · 已同步"}
                </span>
                <span className={`diag-summary ${errCount ? "has-err" : ""}`}>
                  诊断 r{active.diagRevision}：{active.diagnostics.length} 条
                  {errCount ? `（${errCount} 个错误）` : ""}
                </span>
                <div className="spacer" />
                <button
                  onClick={() => discardLocal(active.id)}
                  disabled={!active.dirty}
                >
                  放弃本地修改
                </button>
                <button
                  className="primary"
                  onClick={() => save(active.id)}
                  disabled={!active.dirty && !active.conflict}
                >
                  {active.conflict ? "强制覆盖保存" : "保存"}
                </button>
              </div>

              {active.conflict && (
                <div className="conflict-banner">
                  <strong>⚠ 冲突：</strong>
                  {active.conflict.reason}
                  <div className="conflict-actions">
                    <details>
                      <summary>查看服务器版本（只读）</summary>
                      <pre className="remote-view">{active.serverText}</pre>
                    </details>
                    <button onClick={() => discardLocal(active.id)}>
                      放弃本地、采用服务器版本
                    </button>
                    <button className="primary" onClick={() => save(active.id)}>
                      保留本地文字并强制覆盖
                    </button>
                  </div>
                </div>
              )}

              <div className="editor-host">
                <DocEditor
                  docId={active.id}
                  text={active.localText}
                  diagnostics={active.diagnostics}
                  onChange={(t) => onLocalChange(active.id, t)}
                  onEditorReady={(ed) => (editorsRef.current[active.id] = ed)}
                />
              </div>
            </>
          ) : (
            <div className="empty-hint big">从左侧选择或新建一个文档</div>
          )}
        </main>
      </div>

      {notice && <div className="toast">{notice}</div>}

      <RenameModal
        open={renameOpen}
        initialName={renameInitial}
        onClose={() => setRenameOpen(false)}
        onCommitted={onRenameCommitted}
        onError={(e) => flash(`重命名被整批拒绝：${e.message}`)}
      />
    </div>
  );

  function flash(text) {
    setNotice(text);
    setTimeout(() => setNotice(""), 4000);
  }
}

function getCursorWord(editor) {
  try {
    return (
      editor.getModel().getWordAtPosition(editor.getPosition())?.word || ""
    );
  } catch {
    return "";
  }
}

function upsertBrief(list, brief) {
  const exists = list.some((d) => d.id === brief.id);
  if (!exists) return [...list, brief];
  return list.map((d) => (d.id === brief.id ? { ...d, ...brief } : d));
}

function mergeDocList(prev, snapshot) {
  if (prev.length === 0) return snapshot;
  const byId = new Map(prev.map((d) => [d.id, d]));
  for (const s of snapshot) byId.set(s.id, { ...byId.get(s.id), ...s });
  return [...byId.values()];
}
