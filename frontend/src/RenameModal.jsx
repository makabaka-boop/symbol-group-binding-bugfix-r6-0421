import { useEffect, useState } from "react";
import { api } from "./api.js";

// 跨文件重命名对话框：先请求预览计划（含每个文档的基准修订与修改范围），
// 用户确认后才提交。预览期间任何相关文档变化都会导致提交 409。
export default function RenameModal({
  open,
  initialName,
  onClose,
  onCommitted,
  onError,
}) {
  const [oldName, setOldName] = useState(initialName || "");
  const [newName, setNewName] = useState("");
  const [group, setGroup] = useState("");
  const [plan, setPlan] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (open) {
      setOldName(initialName || "");
      setNewName("");
      setGroup("");
      setPlan(null);
      setErr("");
    }
  }, [open, initialName]);

  if (!open) return null;

  const asciiOk = (s) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(s);

  const preview = async () => {
    setErr("");
    const groupPairs = parseGroup(group);
    if (group.trim() && groupPairs instanceof Error) {
      setErr(groupPairs.message);
      return;
    }
    if (!groupPairs && (!asciiOk(oldName) || !asciiOk(newName))) {
      setErr("符号名只能是 ASCII 标识符（字母、数字、下划线，数字不开头）");
      return;
    }
    if (!groupPairs && oldName === newName) {
      setErr("新名字与原名相同");
      return;
    }
    setBusy(true);
    try {
      // 预览前先等一个微任务，UI 更顺
      const p = groupPairs
        ? await fetch("/api/rename/group/preview", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pairs: groupPairs }),
          }).then(async (r) => {
            const data = await r.json();
            if (!r.ok) throw new Error(data.detail);
            return data;
          })
        : await api.renamePreview(oldName, newName);
      setPlan(p);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const commit = async () => {
    setErr("");
    setBusy(true);
    try {
      const res = await api.renameCommit(plan.id);
      onCommitted(res, plan);
    } catch (e) {
      setErr(e.message);
      onError?.(e);
      // 提交失败（并发编辑/同名声明/索引失效）：计划作废，需要重新预览
      setPlan(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={() => !busy && onClose()}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>跨文件重命名</h2>
        <div className="form-row">
          <label>原符号名</label>
          <input
            value={oldName}
            disabled={!!plan}
            onChange={(e) => setOldName(e.target.value)}
            spellCheck={false}
          />
        </div>
        <div className="form-row">
          <label>新符号名</label>
          <input
            value={newName}
            disabled={!!plan}
            onChange={(e) => setNewName(e.target.value)}
            spellCheck={false}
            placeholder="仅 ASCII"
          />
        </div>

        <label>成组重命名（每行 原名 -&gt; 新名）</label>
        <textarea
          value={group}
          disabled={!!plan}
          onChange={(e) => setGroup(e.target.value)}
        />
        {err && <div className="error-box">{err}</div>}

        {plan && (
          <div className="plan-box">
            <div className="plan-meta">
              计划 <code>{plan.id.slice(0, 8)}</code>：共 {plan.edits.length}{" "}
              处修改，涉及 {Object.keys(plan.baselines).length} 个文档
              <br />
              <small>
                各文档基准修订：
                {Object.entries(plan.baselines)
                  .map(([id, r]) => `${id.slice(0, 6)}@r${r}`)
                  .join("，")}
              </small>
            </div>
            {plan.previews.map((p) => (
              <div key={p.docId} className="diff">
                <div className="diff-title">
                  {p.title}{" "}
                  <small>
                    (r{p.baseRevision}，{p.editCount} 处)
                  </small>
                </div>
                <pre className="diff-before">
                  --- 修改前 ---{"\n" + p.before}
                </pre>
                <pre className="diff-after">+++ 修改后 ---{"\n" + p.after}</pre>
              </div>
            ))}
            <div className="warn-line">
              确认期间若有任何相关文档被编辑、或出现同名声明，整批提交将被拒绝。
            </div>
          </div>
        )}

        <div className="modal-actions">
          <button onClick={onClose} disabled={busy}>
            取消
          </button>
          {!plan ? (
            <button className="primary" onClick={preview} disabled={busy}>
              {busy ? "生成中…" : "生成预览计划"}
            </button>
          ) : (
            <>
              <button onClick={() => setPlan(null)} disabled={busy}>
                重新生成
              </button>
              <button className="primary" onClick={commit} disabled={busy}>
                {busy ? "提交中…" : "确认提交（一次事务）"}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// 每行必须恰好是 "旧名 -> 新名"，两个名字都为 ASCII 标识符，
// 且组内源名、目标名各自唯一（交换与链式在同一时刻生效）。
// 无内容返回 null；格式错误返回 Error。
function parseGroup(text) {
  const lines = text
    .split(/\n/)
    .map((l) => l.trim())
    .filter(Boolean);
  if (!lines.length) return null;
  const re = /^[A-Za-z_][A-Za-z0-9_]*$/;
  const pairs = [];
  const sources = new Set();
  const destinations = new Set();
  for (const [i, line] of lines.entries()) {
    const parts = line.split("->");
    if (parts.length !== 2) {
      return new Error(`第 ${i + 1} 行格式应为：旧名 -> 新名`);
    }
    const oldName = parts[0].trim();
    const newName = parts[1].trim();
    if (!re.test(oldName) || !re.test(newName)) {
      return new Error(`第 ${i + 1} 行的符号名只能是 ASCII 标识符`);
    }
    if (oldName === newName) {
      return new Error(`第 ${i + 1} 行新名字与原名相同`);
    }
    if (sources.has(oldName)) return new Error(`源名 ${oldName} 重复出现`);
    if (destinations.has(newName)) {
      return new Error(`目标名 ${newName} 被多行使用`);
    }
    sources.add(oldName);
    destinations.add(newName);
    pairs.push({ oldName, newName });
  }
  return pairs;
}
