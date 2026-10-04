"""Workspace rename groups: build the simultaneous-edit preview and persist it.

预览与提交必须描述同一次操作：这里生成的每个 Edit 都按 *本组对应关系*
同时映射（交换、链式都是一次同步改名），并连同基准 revision 与绑定证据
一起入库；提交端（Store.commit_group_plan）只执行这里记录的 edits，
绝不在确认时重新推导计划。
"""

import json
import time
import uuid

from .minilang import parse, is_ascii_name
from .store import StoreError, Edit, apply_edits_preview, group_bindings


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
            "bindings": group_bindings(
                docs, set(mapping) | set(mapping.values())
            ),
            "previews": previews,
        }
        try:
            store._conn.execute("BEGIN IMMEDIATE")
            store._conn.execute(
                "INSERT INTO rename_plans VALUES(?,?,?,?,?)",
                (plan["id"], "", "", plan["createdAt"], json.dumps(plan)),
            )
            store._gc_plans_locked()
            store._conn.commit()
        except Exception:
            store._conn.rollback()
            raise
        return plan


def commit(store, plan):
    """Compatibility wrapper: the single-transaction execution lives in Store."""
    return store.commit_group_plan(plan)
