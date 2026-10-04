// 前端纯逻辑测试：后端 UTF-16 诊断 -> Monaco marker 坐标映射。
// 运行：node frontend/tests/positions.test.mjs
import assert from "node:assert";
import { toMarkers } from "../src/monacoSetup.js";

const monacoStub = {
  MarkerSeverity: { Error: 8, Warning: 4, Info: 2, Hint: 1 },
};

// 后端坐标按 UTF-16（1-based 行列）；Monaco 的 Position/IMarkerData 也是
// UTF-16、1-based，所以前端不做任何换算，直接绑定。
const diags = [
  {
    code: "undefined-name",
    message: "引用了未定义的符号 'ghost'",
    severity: "error",
    range: {
      start: { line: 4, column: 5, offset: 42 },
      end: { line: 4, column: 10, offset: 47 },
    },
  },
  {
    code: "unused-def",
    message: "符号 'x' 已定义但从未使用",
    severity: "info",
    range: {
      start: { line: 2, column: 5, offset: 8 },
      end: { line: 2, column: 6, offset: 9 },
    },
  },
];

const markers = toMarkers(monacoStub, diags);
assert.equal(markers.length, 2);
assert.deepEqual(
  {
    sl: markers[0].startLineNumber,
    sc: markers[0].startColumn,
    el: markers[0].endLineNumber,
    ec: markers[0].endColumn,
  },
  { sl: 4, sc: 5, el: 4, ec: 10 },
  "UTF-16 行列必须原样绑定到 Monaco marker"
);
assert.equal(markers[0].severity, 8);
assert.equal(markers[1].severity, 2);
assert.equal(markers[0].message, "引用了未定义的符号 'ghost'");

// emoji（U+1F44D 代理对，UTF-16 占 2 列）：后端列号透传
const emojiDiag = [
  {
    code: "syntax",
    message: "x",
    severity: "error",
    range: {
      start: { line: 2, column: 7, offset: 10 },
      end: { line: 2, column: 8, offset: 11 },
    },
  },
];
const [m] = toMarkers(monacoStub, emojiDiag);
assert.equal(m.startColumn, 7);
assert.equal(m.endColumn, 8);

console.log("前端位置映射测试通过 ✅（中文/emoji 的 UTF-16 行列直接绑定 Monaco）");
