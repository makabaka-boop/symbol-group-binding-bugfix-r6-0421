# mini-lang 代码工作区

React + Monaco 编辑器（最多 20 个文档）配合 FastAPI 后台的小语言代码工作区。

## 小语言

每行只能是：

```
def NAME        # 声明（NAME 仅 ASCII：[A-Za-z_][A-Za-z0-9_]*）
use NAME        # 引用
# 任意 Unicode 注释（中文、emoji 皆可，只能出现在 # 后）
```

诊断：未定义引用（error）、重复定义（error）、未使用定义（info）、语法错误（error）。

- 所有诊断位置使用 **UTF-16 code unit 的 1-based 行列**（与 Monaco `Position` 一致；
  emoji 等增补平面字符占 2 列），另附 0-based Python 偏移供替换使用。
- 诊断**绑定文档 revision**：异步分析回写时以 revision 为门闩，旧修订的迟到结果
  返回 `stale: true` 并被丢弃，不能覆盖新文本。

## 跨文件重命名（两阶段 + 单事务提交）

1. `POST /api/rename/preview` `{oldName,newName}` → 返回计划：每处修改的字符偏移范围、
   旧/新文本、每个文档的**基准 revision** 和改前/改后全文预览。
2. `POST /api/rename/commit` `{planId}`：在**单个 SQLite 事务**（`BEGIN IMMEDIATE`）内：
   - 校验每个相关文档 revision 仍等于基准（预览期间被编辑 → 409 整批拒绝）；
   - 校验工作区内不存在 `newName` 的同名声明；
   - 逐处校验偏移处文本仍是 `oldText`（索引失效 → 409）；
   - 倒序应用偏移替换，注释区间永远不会成为修改范围。

   任一步失败全部回滚，不会改到一半；成功后相关文档共享同一新 revision。
   计划一次性，提交后删除。

## 多会话实时通知

`WS /ws` 向所有浏览器会话广播 `document_changed` 与 `diagnostics`。
客户端规则：

- 本地**干净**：自动对齐服务端新文本；
- 本地有**未提交文字**：文字原样保留，显示冲突横幅，可对比服务器版本后
  「放弃本地」或「强制覆盖」。

## 运行

```bash
# 后端
pip install -r requirements.txt
uvicorn backend.main:app --port 8000

# 前端（另一终端，自动代理 /api 与 /ws 到 8000）
cd frontend
npm install
npm run dev          # http://localhost:5173
```

## 测试

```bash
python3 -m pytest backend/tests -q          # 16 个后端测试
node frontend/tests/positions.test.mjs      # 前端 UTF-16 映射
python3 e2e_smoke.py                         # 双 WS 会话端到端冒烟（需先启动后端）
```

覆盖点：中文与 emoji 注释后的 UTF-16 定位、注释文本不被重命名替换、
预览期间并发编辑导致整批拒绝且全批不变、同名声明拒绝、索引失效拒绝、
旧诊断过期、乐观锁冲突、两个会话收到同一修订通知、20 文档上限。

## 主要文件

| 文件 | 作用 |
| --- | --- |
| `backend/minilang.py` | 解析器：ASCII 符号、Unicode 注释、UTF-16 位置、诊断 |
| `backend/store.py` | SQLite 存储、修订、诊断绑定、重命名预览/单事务提交 |
| `backend/main.py` | REST + `/ws` 广播 + 绑定修订的异步分析端点 |
| `frontend/src/App.jsx` | 工作区 UI：标签页、冲突保留、保存与重命名编排 |
| `frontend/src/RenameModal.jsx` | 预览计划对话框（基准修订 + 前后对比） |
| `frontend/src/DocEditor.jsx` | Monaco 编辑器与 marker 绑定 |

## Rename groups
The rename dialog accepts one `source -> destination` per line, with a preview before confirmation. Sources bind to original declarations; swaps and chains are simultaneous. One group has one revision and either updates every original occurrence or updates nothing. New references to group symbols invalidate the preview; unrelated workspace edits do not. Comments are preserved. Group preview is also available through POST /api/rename/group/preview.
