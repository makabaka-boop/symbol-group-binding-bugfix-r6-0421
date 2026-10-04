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
    return {
        d["id"]: [
            (o.name, o.range.start.offset, o.range.end.offset)
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
        return plan


def commit(store, plan):
    documents = []
    for pair in plan["pairs"]:
        step = store.build_rename_plan(pair["oldName"], pair["newName"])
        result = store.commit_rename(step["id"])
        documents.extend(result["documents"])
    store._conn.execute("DELETE FROM rename_plans WHERE id=?", (plan["id"],))
    return {
        "planId": plan["id"],
        "documents": documents,
        "revision": result["revision"],
    }
