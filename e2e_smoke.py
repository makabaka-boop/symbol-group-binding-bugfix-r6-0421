"""端到端冒烟：两个浏览器会话（2 条 WS）+ 中文/emoji 定位 + 重命名并发拒绝。"""

import asyncio
import json

import httpx
import websockets

BASE = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000/ws"


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=10) as http:
        # 两个浏览器会话
        ws1 = await websockets.connect(WS)
        ws2 = await websockets.connect(WS)
        snap1 = json.loads(await ws1.recv())
        snap2 = json.loads(await ws2.recv())
        assert snap1["type"] == snap2["type"] == "snapshot"
        print("1) 两个会话都收到 snapshot：", len(snap1["documents"]), "个文档")

        # 中文 + emoji 注释文档
        content = "# 初始化：中文注释 🎉\ndef 猫\nuse 猫 # 引用一下 👍\nuse ghost"
        # 注：'猫' 非 ASCII 不能做符号名 -> 用 ASCII 名字 + 中文注释
        content = "# 初始化：中文注释 🎉\ndef cat\nuse cat # 引用一下 👍\nuse ghost"
        r = await http.post(
            "/api/documents", json={"title": "demo.mini", "content": content}
        )
        r.raise_for_status()
        doc = r.json()
        print("2) 建文档", doc["id"], "r", doc["revision"])

        m1 = json.loads(await ws1.recv())
        m2 = json.loads(await ws2.recv())
        assert m1["type"] == m2["type"] == "document_changed"
        assert m1 == m2 and m1["revision"] == doc["revision"]
        print("3) 两个会话同时收到 document_changed r", m1["revision"])

        # 异步分析（绑定修订），ghost 未定义
        r = await http.post(
            f"/api/documents/{doc['id']}/analyze",
            json={"content": content, "revision": doc["revision"]},
        )
        ana = r.json()
        assert not ana["stale"]
        diag = ana["diagnostics"]
        undef = [d for d in diag if d["code"] == "undefined-name"][0]
        print("4) undefined-name UTF-16 位置：", undef["range"]["start"])
        assert undef["range"]["start"]["line"] == 4
        assert undef["range"]["start"]["column"] == 5
        # ws1/ws2 都收到 diagnostics 广播
        dm1 = json.loads(await ws1.recv())
        dm2 = json.loads(await ws2.recv())
        assert dm1["type"] == dm2["type"] == "diagnostics"
        print("5) 两个会话都收到 diagnostics 广播，共", len(dm1["diagnostics"]), "条")

        # 旧修订的迟到分析不能覆盖新文本
        await http.put(
            f"/api/documents/{doc['id']}",
            json={"content": "def dog", "expectedRevision": doc["revision"]},
        )
        change1 = json.loads(await ws1.recv())
        change2 = json.loads(await ws2.recv())
        assert change1["revision"] == change2["revision"] == doc["revision"] + 1
        stale = (
            await http.post(
                f"/api/documents/{doc['id']}/analyze",
                json={"content": content, "revision": doc["revision"]},
            )
        ).json()
        assert stale["stale"] is True
        fresh_diag = (await http.get(f"/api/documents/{doc['id']}/diagnostics")).json()
        assert fresh_diag["revision"] == doc["revision"] + 1
        assert all("ghost" not in d["message"] for d in fresh_diag["diagnostics"])
        print("6) 旧修订分析被标记 stale 且未覆盖新文本诊断 r", fresh_diag["revision"])

        # 第二个文档，准备跨文件重命名
        d2 = (
            await http.post(
                "/api/documents",
                json={"title": "other.mini", "content": "use dog # 狗"},
            )
        ).json()
        await ws1.recv()
        await ws2.recv()

        plan = (
            await http.post(
                "/api/rename/preview", json={"oldName": "dog", "newName": "hound"}
            )
        ).json()
        print("7) 预览计划：", plan["edits"].__len__(), "处，基准", plan["baselines"])
        assert all("狗" not in e["oldText"] for e in plan["edits"])  # 注释不在编辑范围
        assert plan["previews"][0]["after"].count("hound") >= 1

        # 预览期间另一会话并发编辑
        await http.put(
            f"/api/documents/{d2['id']}",
            json={
                "content": "use dog # 狗\n# 会话二的并发修改",
                "expectedRevision": d2["revision"],
            },
        )
        await ws1.recv()
        await ws2.recv()
        rej = await http.post("/api/rename/commit", json={"planId": plan["id"]})
        assert rej.status_code == 409
        print("8) 并发编辑后提交 -> 409：", rej.json()["detail"])
        # 全批不变
        untouched = (await http.get(f"/api/documents/{doc['id']}")).json()["content"]
        assert untouched == "def dog", untouched
        # 计划一次性，作废
        rej2 = await http.post("/api/rename/commit", json={"planId": plan["id"]})
        assert rej2.status_code == 409

        # 重新预览，这次无干扰，原子提交
        plan2 = (
            await http.post(
                "/api/rename/preview", json={"oldName": "dog", "newName": "hound"}
            )
        ).json()
        ok = (
            await http.post("/api/rename/commit", json={"planId": plan2["id"]})
        ).json()
        print(
            "9) 原子提交成功 r", ok["revision"], "改了", len(ok["documents"]), "个文档"
        )
        # 两个会话各自收到两次 document_changed
        changes = []
        for _ in range(2):
            changes.append(json.loads(await ws1.recv()))
        assert all(
            c["type"] == "document_changed" and c["revision"] == ok["revision"]
            for c in changes
        )
        for _ in range(2):
            json.loads(await ws2.recv())
        final1 = (await http.get(f"/api/documents/{doc['id']}")).json()
        final2 = (await http.get(f"/api/documents/{d2['id']}")).json()
        assert "def hound" in final1["content"]
        assert "use hound # 狗" in final2["content"]
        assert "# 会话二的并发修改" in final2["content"]
        print("10) 重命名只改符号、注释完好：")
        for f in (final1, final2):
            print("   ", f["title"], "r", f["revision"], "->", repr(f["content"]))

        await ws1.close()
        await ws2.close()

    print("\n全部端到端场景通过 ✅")


asyncio.run(main())
