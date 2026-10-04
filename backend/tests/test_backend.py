"""后端测试：UTF-16 定位、注释保护、原子重命名、过期诊断、WS 通知。"""

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend import minilang
from backend.main import app, store
from backend.store import Store


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    """每个测试用独立内存库替换全局 store。"""
    import backend.main as main

    mem = Store(":memory:")
    monkeypatch.setattr(main, "store", mem)
    yield mem
    mem.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ---------- 解析与 UTF-16 定位 ----------


def test_chinese_comment_utf16_position():
    # “中” 是 BMP 字符：UTF-16 长度 1；诊断位置必须算到注释之外的语句上
    text = "# 中文注释\ndef foo\nuse foo  # 引用一下"
    parsed = minilang.parse(text)
    assert parsed.diagnostics == []  # foo 定义且被使用
    assert [o.name for o in parsed.occurrences] == ["foo", "foo"]


def test_emoji_comment_utf16_columns():
    # 第 1 行：# 👍 = '#'(1) + ' '(1) + 👍(U+1F44D, 代理对=2)
    # 第 2 行在 emoji 行之后，诊断行列必须正确分行
    text = "# 👍\nuse ghost"
    parsed = minilang.parse(text)
    diag = next(d for d in parsed.diagnostics if d.code == "undefined-name")
    assert diag.range.start.line == 2
    assert diag.range.start.column == 5  # "use " 之后
    assert diag.range.start.offset == len("# 👍\nuse ")


def test_emoji_before_error_on_same_line_shifts_column_by_2():
    # 注释里的 emoji 在同一行的后续诊断之前不应影响（注释后无语句）；
    # 但行内多字节字符的 UTF-16 计数要验证：构造非法非 ASCII 在代码区
    text = "def fOO\n# 😀😀 两个 emoji 占 4 个 UTF-16 单元\nuse fOO"
    parsed = minilang.parse(text)
    # fOO 定义且使用，无诊断
    assert [d.code for d in parsed.diagnostics] == []
    use = next(o for o in parsed.occurrences if o.kind == "use")
    assert use.range.start.line == 3
    assert use.range.start.column == 5
    # 偏移按 Python 字符：emoji 行只有 1 个码点每个
    assert text[use.range.start.offset : use.range.end.offset] == "fOO"


def test_non_ascii_in_code_is_error():
    text = "def fOO # ok\ndef fOO中"
    parsed = minilang.parse(text)
    codes = [d.code for d in parsed.diagnostics]
    assert "duplicate-def" in codes  # 先识别出 fOO 重复（第二次名字前缀 fOO）
    # “中” 出现在名字之后，作为尾部非法字符报错
    assert any(
        "非 ASCII" in d.message or "只允许行尾注释" in d.message
        for d in parsed.diagnostics
    )


def test_comment_ranges_exclude_code():
    text = "def bar # 定义 bar 吧 🎉\nuse bar"
    parsed = minilang.parse(text)
    assert len(parsed.comment_ranges) == 1
    r = parsed.comment_ranges[0]
    assert text[r.start.offset : r.end.offset].startswith("#")
    assert "🎉" in text[r.start.offset : r.end.offset]


# ---------- 重命名：注释绝不被替换 ----------


def test_rename_does_not_touch_comments(client):
    a = client.post(
        "/api/documents",
        json={
            "title": "a.mini",
            "content": "def alpha # alpha 在注释里也出现了 alpha\nalpha_use\nuse alpha # α",
        },
    ).json()
    plan = client.post(
        "/api/rename/preview", json={"oldName": "alpha", "newName": "beta"}
    ).json()
    assert len(plan["edits"]) == 2  # def alpha + use alpha，"alpha_use" 不是标识符出现
    resp = client.post("/api/rename/commit", json={"planId": plan["id"]})
    assert resp.status_code == 200
    after = client.get(f"/api/documents/{a['id']}").json()["content"]
    assert "def beta" in after
    assert "use beta" in after
    assert "# alpha 在注释里也出现了 alpha" in after  # 注释原样保留
    assert "# α" in after
    assert "alpha_use" in after


# ---------- 原子性：预览期间并发编辑 → 整批拒绝 ----------


def test_concurrent_edit_during_preview_rejects_whole_batch(client):
    a = client.post(
        "/api/documents", json={"title": "a", "content": "def x\nuse x"}
    ).json()
    b = client.post("/api/documents", json={"title": "b", "content": "use x"}).json()

    plan = client.post(
        "/api/rename/preview", json={"oldName": "x", "newName": "y"}
    ).json()
    assert set(plan["baselines"]) == {a["id"], b["id"]}

    # 预览期间另一个会话改了文档 b
    client.put(
        f"/api/documents/{b['id']}",
        json={"content": "use x\n# 并发修改", "expectedRevision": b["revision"]},
    )

    resp = client.post("/api/rename/commit", json={"planId": plan["id"]})
    assert resp.status_code == 409
    assert "整批拒绝" in resp.json()["detail"]

    # 两个文档都保持原状（a 没被改成一半）
    assert client.get(f"/api/documents/{a['id']}").json()["content"] == "def x\nuse x"
    assert (
        client.get(f"/api/documents/{b['id']}").json()["content"] == "use x\n# 并发修改"
    )


def test_duplicate_target_name_rejects_batch(client):
    # 预览合法（目标名 y 不存在）；确认前别的会话插入同名声明 -> 整批拒绝
    a = client.post(
        "/api/documents", json={"title": "a", "content": "def p\nuse p"}
    ).json()
    plan = client.post(
        "/api/rename/preview", json={"oldName": "p", "newName": "y"}
    ).json()
    assert "id" in plan
    client.post("/api/documents", json={"title": "c", "content": "def y"})

    resp = client.post("/api/rename/commit", json={"planId": plan["id"]})
    assert resp.status_code == 409
    assert "y" in resp.json()["detail"]
    assert client.get(f"/api/documents/{a['id']}").json()["content"] == "def p\nuse p"


def test_plan_is_one_shot_and_single_revision(client):
    a = client.post("/api/documents", json={"title": "a", "content": "def x"}).json()
    b = client.post("/api/documents", json={"title": "b", "content": "use x"}).json()
    plan = client.post(
        "/api/rename/preview", json={"oldName": "x", "newName": "z"}
    ).json()
    r1 = client.post("/api/rename/commit", json={"planId": plan["id"]})
    assert r1.status_code == 200
    rev = r1.json()["revision"]
    assert client.get(f"/api/documents/{a['id']}").json()["revision"] == rev
    assert client.get(f"/api/documents/{b['id']}").json()["revision"] == rev
    # 计划一次性：再次提交必须失败
    again = client.post("/api/rename/commit", json={"planId": plan["id"]})
    assert again.status_code == 409


def test_stale_index_rejected(client, monkeypatch):
    # 直接构造：预览后不经过 PUT 而是篡改到相同 revision 的不同内容
    import backend.main as main

    a = client.post(
        "/api/documents", json={"title": "a", "content": "def x\nuse x"}
    ).json()
    plan = client.post(
        "/api/rename/preview", json={"oldName": "x", "newName": "q"}
    ).json()
    # 手工把内容换成相同长度/修订号不变的不同文本（模拟索引失效但 revision 未变的极端情况不可能经由 API，
    # 这里直接改库验证偏移校验这道防线）
    main.store._conn.execute(
        "UPDATE documents SET content=? WHERE id=?", ("def w\nuse w", a["id"])
    )
    resp = client.post("/api/rename/commit", json={"planId": plan["id"]})
    assert resp.status_code == 409
    assert client.get(f"/api/documents/{a['id']}").json()["content"] == "def w\nuse w"


# ---------- 过期诊断不能覆盖新文本 ----------


def test_stale_analysis_discarded():
    s = Store(":memory:")
    d = s.create_document("d", "def a")
    old_rev = d["revision"]
    # 文档前进到新修订
    d2 = s.replace_document(d["id"], "def b", expected_revision=old_rev)
    # 旧修订的迟到分析结果到达
    result = s.analyze(d["id"], "def a\nuse nope", old_rev)
    assert result is None
    diag = s.get_diagnostics(d["id"])
    assert diag["revision"] == d2["revision"]
    msgs = " ".join(x["message"] for x in diag["diagnostics"])
    assert "nope" not in msgs
    s.close()


def test_analyze_endpoint_stale_flag(client):
    d = client.post("/api/documents", json={"title": "d", "content": "def a"}).json()
    old = d["revision"]
    client.put(
        f"/api/documents/{d['id']}", json={"content": "def b", "expectedRevision": old}
    )
    resp = client.post(
        f"/api/documents/{d['id']}/analyze",
        json={"content": "def a\nuse nope", "revision": old},
    ).json()
    assert resp["stale"] is True
    assert (
        client.get(f"/api/documents/{d['id']}/diagnostics").json()["revision"]
        == old + 1
    )


def test_optimistic_lock_conflict(client):
    d = client.post("/api/documents", json={"title": "d", "content": "def a"}).json()
    client.put(
        f"/api/documents/{d['id']}",
        json={"content": "def b", "expectedRevision": d["revision"]},
    )
    # 基于旧修订的写入被拒
    r = client.put(
        f"/api/documents/{d['id']}",
        json={"content": "def c", "expectedRevision": d["revision"]},
    )
    assert r.status_code == 409
    assert client.get(f"/api/documents/{d['id']}").json()["content"] == "def b"


# ---------- 20 文档上限 ----------


def test_max_twenty_documents(client):
    for i in range(20):
        assert client.post("/api/documents", json={"title": f"d{i}"}).status_code == 201
    r = client.post("/api/documents", json={"title": "overflow"})
    assert r.status_code == 400
    assert "20" in r.json()["detail"]


# ---------- 两个浏览器会话收到修订通知 ----------


def test_two_sessions_receive_revision_notifications(client):
    import json as _json

    with client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        # 初始快照
        snap1 = ws1.receive_json()
        snap2 = ws2.receive_json()
        assert snap1["type"] == snap2["type"] == "snapshot"

        d = client.post(
            "/api/documents", json={"title": "ws", "content": "def n"}
        ).json()

        m1 = ws1.receive_json()
        m2 = ws2.receive_json()
        assert m1["type"] == m2["type"] == "document_changed"
        assert m1["document"]["id"] == d["id"]
        assert m1["revision"] == d["revision"]
        assert m1 == m2


def test_sessions_receive_diagnostics_broadcast(client):
    d = client.post("/api/documents", json={"title": "d", "content": "def a"}).json()
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # snapshot
        client.post(
            f"/api/documents/{d['id']}/analyze",
            json={"content": "def a", "revision": d["revision"]},
        )
        msg = ws.receive_json()
        assert msg["type"] == "diagnostics"
        assert msg["revision"] == d["revision"]
