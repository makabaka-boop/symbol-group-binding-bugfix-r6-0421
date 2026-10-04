"""Workspace rename groups, retaining the preview and symbol occurrence evidence."""

import json
import time
import uuid
from .minilang import parse, is_ascii_name
from .store import StoreError, ConflictError, Edit, apply_edits_preview


def mapping_of(pairs):
    mapping = {}
    for pair in pairs:
        old, new = pair["oldName"], pair["newName"]
        if not is_ascii_name(old) or not is_ascii_name(new) or old == new:
            raise StoreError("invalid rename pair")
        if old in mapping or new in mapping.values():
            raise StoreError("rename sources and destinations must be unique")
        mapping[old] = new
    if not mapping:
        raise StoreError("empty rename group")
    return mapping


def bindings(docs, names):
    """每个文档中相关符号的出现（名字 + 偏移区间）。

    用 list 而非 tuple，保证与 rename_plans 中 JSON 序列化后的
    快照逐字节同构，提交时可直接与重算结果做相等比较。
    """
    return {
        d["id"]: [
            [o.name, o.range.start.offset, o.range.end.offset]
            for o in parse(d["content"]).occurrences
            if o.name in names
        ]
        for d in docs
        if any(o.name in names for o in parse(d["content"]).occurrences)
    }


def preview(store, pairs):
    mapping = mapping_of(pairs)
    with store._lock:
        docs = store.list_documents()
        declarations = {}
        for d in docs:
            for name, occurrences in parse(d["content"]).defs.items():
                declarations.setdefault(name, []).extend(occurrences)
        for old in mapping:
            if len(declarations.get(old, [])) != 1:
                raise StoreError("rename requires one declaration per source")
        for new in mapping.values():
            if new not in mapping and declarations.get(new):
                raise StoreError("destination already declared")
        edits, baselines, previews = [], {}, []
        for d in docs:
            local = []
            for o in parse(d["content"]).occurrences:
                if o.name in mapping:
                    local.append(
                        Edit(
                            d["id"],
                            o.range.start.offset,
                            o.range.end.offset,
                            o.name,
                            mapping[o.name],
                            o.range.to_json(),
                        ).to_json()
                    )
            if local:
                edits.extend(local)
                baselines[d["id"]] = d["revision"]
                previews.append(
                    {
                        "docId": d["id"],
                        "title": d["title"],
                        "baseRevision": d["revision"],
                        "editCount": len(local),
                        "before": d["content"],
                        "after": apply_edits_preview(d["content"], local),
                    }
                )
        plan = {
            "id": uuid.uuid4().hex,
            "kind": "group",
            "pairs": pairs,
            "createdAt": time.time(),
            "baselines": baselines,
            "edits": edits,
            "bindings": bindings(docs, set(mapping) | set(mapping.values())),
            "previews": previews,
        }
        store._conn.execute(
            "INSERT INTO rename_plans VALUES(?,?,?,?,?)",
            (plan["id"], "", "", plan["createdAt"], json.dumps(plan)),
        )
        store._gc_plans_locked()
        return plan


def commit(store, plan):
    """单事务提交整组重命名。

    - 交换与链式对应是同时发生的：应用的是预览时按原始声明算出的 edits，
      每个原始声明及其引用恰好改一次，不会被后续pair再次命中；
    - 提交前重算相关符号（源名与目标名）的绑定并与预览快照比对：
      预览后新增相关引用、改动原文或删除相关文档 -> 整组 409，不产生任何改动；
      无关文档的编辑不改变绑定，不妨碍提交；
    - 所有相关文档共享同一个新 revision，诊断在同一事务内重算，
      正文 / 修订通知 / 诊断对应同一次操作。
    """
    mapping = {p["oldName"]: p["newName"] for p in plan["pairs"]}
    names = set(mapping) | set(mapping.values())
    by_doc: dict[str, list[dict]] = {}
    for ed in plan["edits"]:
        by_doc.setdefault(ed["docId"], []).append(ed)

    with store._lock:
        new_revision = store._next_revision_locked()  # 整组共享一个修订号
        store._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = store._conn.execute(
                "SELECT id, title, content, revision FROM documents ORDER BY id"
            ).fetchall()
            docs = [
                {"id": r[0], "title": r[1], "content": r[2], "revision": r[3]}
                for r in rows
            ]

            # 1) 绑定校验：相关符号的出现（文档 + 偏移）必须仍与预览时一致
            if bindings(docs, names) != plan["bindings"]:
                raise ConflictError(
                    "相关符号的声明或引用在预览后已变化，整组不做任何改动"
                )
            by_id = {d["id"]: d for d in docs}

            # 2) 逐文档倒序应用预览时的 edits，每处偏移文本必须仍是 oldText
            now = time.time()
            updated: list[dict] = []
            for doc_id, eds in by_doc.items():
                doc = by_id.get(doc_id)
                if doc is None:
                    raise ConflictError(f"文档 {doc_id} 已被删除，整组拒绝")
                content = doc["content"]
                for ed in sorted(eds, key=lambda e: e["startOffset"], reverse=True):
                    s, e = ed["startOffset"], ed["endOffset"]
                    if content[s:e] != ed["oldText"]:
                        raise ConflictError(
                            f"文档 {doc['title']!r} 的索引已失效（偏移 {s} 处文本不是 "
                            f"{ed['oldText']!r}），整组拒绝"
                        )
                    content = content[:s] + ed["newText"] + content[e:]
                store._conn.execute(
                    "UPDATE documents SET content=?, revision=?, updated_at=? WHERE id=?",
                    (content, new_revision, now, doc_id),
                )
                store._update_diagnostics_locked(doc_id, content, new_revision)
                updated.append(
                    {"id": doc_id, "title": doc["title"], "revision": new_revision}
                )

            # 3) 计划一次性：随事务一起删除，任何失败都整体回滚
            store._conn.execute("DELETE FROM rename_plans WHERE id=?", (plan["id"],))
            store._conn.commit()
        except Exception:
            store._conn.rollback()
            raise

    return {
        "planId": plan["id"],
        "revision": new_revision,
        "documents": updated,
    }
