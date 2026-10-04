"""SQLite 存储层：文档、修订、诊断快照、重命名计划。

关键不变量：
- 每次文档改动生成新的整数 revision（单调递增，全工作区共享）。
- 诊断绑定到生成时的文档 revision；旧修订的异步分析结果不能覆盖新文本。
- 重命名分两步：POST /rename/preview 生成计划（含每个文档的基准 revision
  与基于偏移量的修改范围）；POST /rename/commit 携带计划 id 一次性提交，
  在单个 SQLite 事务内完成校验 + 改写。任一文档 revision 变化、出现同名
  声明或索引已失效（stale）时整批拒绝，数据库保持原状。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .minilang import parse

MAX_DOCUMENTS = 20


class StoreError(Exception):
    """业务错误（4xx）。"""


class ConflictError(StoreError):
    """修订冲突 / 计划失效（409）。"""


@dataclass
class Edit:
    """计划中的一次文本替换：把 [start_offset, end_offset) 替换为 new_text。"""

    doc_id: str
    start_offset: int
    end_offset: int
    old_text: str
    new_text: str
    range: dict  # UTF-16 形式，仅用于预览展示

    def to_json(self) -> dict:
        return {
            "docId": self.doc_id,
            "startOffset": self.start_offset,
            "endOffset": self.end_offset,
            "oldText": self.old_text,
            "newText": self.new_text,
            "range": self.range,
        }


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnostics (
    doc_id    TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    revision  INTEGER NOT NULL,
    payload   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rename_plans (
    id          TEXT PRIMARY KEY,
    old_name    TEXT NOT NULL,
    new_name    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    payload     TEXT NOT NULL   -- 完整计划 JSON（基准 revision + edits）
);
"""


class Store:
    def __init__(self, db_path: str | Path = ":memory:"):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("BEGIN")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 文档 ----------

    def list_documents(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, content, revision, updated_at FROM documents ORDER BY id"
            ).fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "content": r[2],
                "revision": r[3],
                "updatedAt": r[4],
            }
            for r in rows
        ]

    def get_document(self, doc_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, content, revision, updated_at FROM documents WHERE id=?",
                (doc_id,),
            ).fetchone()
        if row is None:
            raise StoreError(f"文档 {doc_id!r} 不存在")
        return {
            "id": row[0],
            "title": row[1],
            "content": row[2],
            "revision": row[3],
            "updatedAt": row[4],
        }

    def create_document(self, title: str, content: str = "") -> dict:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                count = self._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[
                    0
                ]
                if count >= MAX_DOCUMENTS:
                    raise StoreError(f"工作区最多 {MAX_DOCUMENTS} 个文档")
                doc_id = uuid.uuid4().hex[:12]
                revision = self._next_revision_locked()
                now = time.time()
                self._conn.execute(
                    "INSERT INTO documents(id, title, content, revision, updated_at) VALUES(?,?,?,?,?)",
                    (doc_id, title, content, revision, now),
                )
                self._update_diagnostics_locked(doc_id, content, revision)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            doc = self.get_document(doc_id)
        return doc

    def replace_document(
        self, doc_id: str, content: str, expected_revision: int | None
    ) -> dict:
        """整篇替换（编辑器保存时调用）。携带 expected_revision 做乐观锁。"""
        with self._lock:
            doc = self._get_row_locked(doc_id)
            if expected_revision is not None and doc[3] != expected_revision:
                raise ConflictError(
                    f"修订冲突：文档当前修订为 {doc[3]}，客户端基于 {expected_revision}"
                )
            if content == doc[2]:
                return {
                    "id": doc[0],
                    "title": doc[1],
                    "content": doc[2],
                    "revision": doc[3],
                    "updatedAt": doc[4],
                }
            revision = self._next_revision_locked()
            now = time.time()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE documents SET content=?, revision=?, updated_at=? WHERE id=?",
                    (content, revision, now, doc_id),
                )
                self._update_diagnostics_locked(doc_id, content, revision)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return self.get_document(doc_id)

    # ---------- 诊断 ----------

    def _update_diagnostics_locked(
        self, doc_id: str, content: str, revision: int
    ) -> None:
        parsed = parse(content)
        payload = json.dumps(
            [d.to_json() for d in parsed.diagnostics], ensure_ascii=False
        )
        self._conn.execute(
            "INSERT INTO diagnostics(doc_id, revision, payload) VALUES(?,?,?) "
            "ON CONFLICT(doc_id) DO UPDATE SET revision=excluded.revision, payload=excluded.payload",
            (doc_id, revision, payload),
        )

    def analyze(self, doc_id: str, content: str, revision: int) -> dict | None:
        """异步分析结果回写。仅当 revision 仍是文档当前修订时生效；
        旧修订的迟到结果一律丢弃，不能覆盖新文本的诊断。

        返回 {"revision":..., "diagnostics":[...]} 或 None（已过期）。
        """
        parsed = parse(content)
        diags = [d.to_json() for d in parsed.diagnostics]
        with self._lock:
            row = self._get_row_locked(doc_id)
            if row[3] != revision:
                return None
            # 内容也可能完全一致；仍以 revision 绑定为准
            payload = json.dumps(diags, ensure_ascii=False)
            self._conn.execute(
                "UPDATE diagnostics SET revision=?, payload=? WHERE doc_id=?",
                (revision, payload, doc_id),
            )
        return {"docId": doc_id, "revision": revision, "diagnostics": diags}

    def get_diagnostics(self, doc_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT revision, payload FROM diagnostics WHERE doc_id=?", (doc_id,)
            ).fetchone()
        if row is None:
            doc = self.get_document(doc_id)
            return {"docId": doc_id, "revision": doc["revision"], "diagnostics": []}
        return {"docId": doc_id, "revision": row[0], "diagnostics": json.loads(row[1])}

    # ---------- 跨文件重命名 ----------

    def build_rename_plan(self, old_name: str, new_name: str) -> dict:
        from .minilang import is_ascii_name

        if not is_ascii_name(old_name):
            raise StoreError("原符号名必须是 ASCII 标识符")
        if not is_ascii_name(new_name):
            raise StoreError("新符号名必须是 ASCII 标识符")
        if old_name == new_name:
            raise StoreError("新符号名与原名相同")

        with self._lock:
            docs = self.list_documents()
            if not any(parse(d["content"]).defs.get(old_name) for d in docs):
                raise StoreError(f"工作区中没有 {old_name!r} 的声明")
            if any(parse(d["content"]).defs.get(new_name) for d in docs):
                raise StoreError(f"工作区中已存在 {new_name!r} 的声明，无法重命名")

            edits: list[Edit] = []
            baselines: dict[str, int] = {}
            for d in docs:
                parsed = parse(d["content"])
                occs = [o for o in parsed.occurrences if o.name == old_name]
                if not occs:
                    continue
                baselines[d["id"]] = d["revision"]
                for occ in occs:
                    s, e = occ.range.start.offset, occ.range.end.offset
                    old_text = d["content"][s:e]
                    assert old_text == old_name
                    edits.append(
                        Edit(
                            doc_id=d["id"],
                            start_offset=s,
                            end_offset=e,
                            old_text=old_text,
                            new_text=new_name,
                            range=occ.range.to_json(),
                        )
                    )

            plan_id = uuid.uuid4().hex
            plan = {
                "id": plan_id,
                "oldName": old_name,
                "newName": new_name,
                "createdAt": time.time(),
                "baselines": baselines,
                "edits": [ed.to_json() for ed in edits],
            }
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO rename_plans(id, old_name, new_name, created_at, payload) VALUES(?,?,?,?,?)",
                    (
                        plan_id,
                        old_name,
                        new_name,
                        plan["createdAt"],
                        json.dumps(plan, ensure_ascii=False),
                    ),
                )
                # 计划只短期有效
                self._gc_plans_locked()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return plan

    def commit_rename(self, plan_id: str) -> dict:
        """原子提交重命名。单事务内：取计划 -> 校验全部基准 revision ->
        校验新名字不与任何现有声明冲突 -> 校验偏移处文本仍是 old_text
        -> 改写所有相关文档。任何一步失败，整批回滚。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM rename_plans WHERE id=?", (plan_id,)
            ).fetchone()
            if row is None:
                raise ConflictError("重命名计划不存在或已失效，请重新生成预览")
            plan = json.loads(row[0])

            if plan.get("kind") == "group":
                from .rename_group import commit

                return commit(self, plan)

            # 分组
            by_doc: dict[str, list[dict]] = {}
            for ed in plan["edits"]:
                by_doc.setdefault(ed["docId"], []).append(ed)

            new_revision = self._next_revision_locked()  # 预留一个全工作区修订号
            try:
                # BEGIN IMMEDIATE 语义：with self._conn 开事务
                self._conn.execute("BEGIN IMMEDIATE")

                # 1) 所有相关文档的 revision 必须仍等于计划基准
                current: dict[str, tuple] = {}
                for doc_id, baseline in plan["baselines"].items():
                    r = self._conn.execute(
                        "SELECT id, title, content, revision FROM documents WHERE id=?",
                        (doc_id,),
                    ).fetchone()
                    if r is None:
                        raise ConflictError(f"文档 {doc_id} 已被删除，整批拒绝")
                    if r[3] != baseline:
                        raise ConflictError(
                            f"文档 {r[1]!r} 在预览期间被修改（基准 {baseline} -> 当前 {r[3]}），整批拒绝"
                        )
                    current[doc_id] = r

                # 2) 工作区内任何文档都不能已有 new_name 的声明（同名冲突/索引失效）
                all_rows = self._conn.execute(
                    "SELECT id, title, content FROM documents"
                ).fetchall()
                for r in all_rows:
                    if parse(r[2]).defs.get(plan["newName"]):
                        raise ConflictError(
                            f"文档 {r[1]!r} 中已存在 {plan['newName']!r} 的声明，整批拒绝"
                        )

                # 3) 逐文档倒序应用偏移替换，每处 old_text 必须匹配
                now = time.time()
                updated: list[dict] = []
                for doc_id, eds in by_doc.items():
                    r = current[doc_id]
                    content = r[2]
                    eds_sorted = sorted(
                        eds, key=lambda e: e["startOffset"], reverse=True
                    )
                    for ed in eds_sorted:
                        s, e = ed["startOffset"], ed["endOffset"]
                        if content[s:e] != ed["oldText"]:
                            raise ConflictError(
                                f"文档 {r[1]!r} 的索引已失效（偏移 {s} 处文本不是 "
                                f"{ed['oldText']!r}），整批拒绝"
                            )
                        content = content[:s] + ed["newText"] + content[e:]
                    self._conn.execute(
                        "UPDATE documents SET content=?, revision=?, updated_at=? WHERE id=?",
                        (content, new_revision, now, doc_id),
                    )
                    self._update_diagnostics_locked(doc_id, content, new_revision)
                    updated.append(
                        {
                            "id": doc_id,
                            "title": r[1],
                            "content": content,
                            "revision": new_revision,
                            "updatedAt": now,
                        }
                    )

                self._conn.execute("DELETE FROM rename_plans WHERE id=?", (plan_id,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        return {
            "planId": plan_id,
            "oldName": plan["oldName"],
            "newName": plan["newName"],
            "revision": new_revision,
            "documents": [
                {"id": u["id"], "title": u["title"], "revision": u["revision"]}
                for u in updated
            ],
        }

    def _gc_plans_locked(self) -> None:
        cutoff = time.time() - 600
        self._conn.execute("DELETE FROM rename_plans WHERE created_at < ?", (cutoff,))

    # ---------- 内部 ----------

    def _get_row_locked(self, doc_id: str):
        row = self._conn.execute(
            "SELECT id, title, content, revision, updated_at FROM documents WHERE id=?",
            (doc_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"文档 {doc_id!r} 不存在")
        return row

    def _next_revision_locked(self) -> int:
        r = self._conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM documents"
        ).fetchone()[0]
        return int(r) + 1


def apply_edits_preview(content: str, edits: list[dict]) -> str:
    """纯函数：按偏移倒序应用 edits，供预览 diff 使用（不落库）。"""
    out = content
    for ed in sorted(edits, key=lambda e: e["startOffset"], reverse=True):
        s, e = ed["startOffset"], ed["endOffset"]
        out = out[:s] + ed["newText"] + out[e:]
    return out
