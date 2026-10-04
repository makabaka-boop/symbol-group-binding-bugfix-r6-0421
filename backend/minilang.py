"""解析器：一个极小的“定义 + 引用 + 行尾注释”语言。

语言规格（按行）::

    line      := [ statement ] [ '#' comment ] NEWLINE
    statement := 'def' NAME                          # 定义
               | 'use' NAME                          # 引用
    NAME      := [A-Za-z_][A-Za-z0-9_]*              # 仅 ASCII

- 符号名只允许 ASCII；非 ASCII 字符只能出现在 ``#`` 之后的注释中。
- 所有行列号按 **UTF-16 code unit** 计算（Monaco 的 Position 约定）：
  BMP 字符记 1，增补平面字符（emoji 等代理对）记 2。
- 诊断中每个位置同时给出 1-based 的 utf16 行列和 0-based 的 Python 字符偏移，
  方便测试与替换范围互转。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
KEYWORDS = {"def", "use"}
# 语句终结符（# 开始注释），其余空白之外都是非法字符
TOKEN_SEPARATOR = "#"

Severity = Literal["error", "warning", "info"]


@dataclass
class Position:
    """UTF-16 位置。line/column 为 1-based；offset 为 0-based Python 字符偏移。"""

    line: int
    column: int
    offset: int

    def to_json(self) -> dict:
        return {"line": self.line, "column": self.column, "offset": self.offset}


@dataclass
class Range:
    start: Position
    end: Position

    def to_json(self) -> dict:
        return {"start": self.start.to_json(), "end": self.end.to_json()}


@dataclass
class Diagnostic:
    code: str
    message: str
    severity: Severity
    range: Range

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "range": self.range.to_json(),
        }


@dataclass
class Occurrence:
    """一个符号出现（定义或引用）。range 覆盖整个 NAME。"""

    kind: Literal["def", "use"]
    name: str
    range: Range
    # 仅 def 使用：该定义被多少个 use 引用
    use_count: int = 0


@dataclass
class ParsedDocument:
    occurrences: list[Occurrence] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # 注释区间（从 # 到行尾，不含换行），重命名时绝不能触碰
    comment_ranges: list[Range] = field(default_factory=list)
    # name -> [定义 Occurrence]（保留重复定义以便诊断）
    defs: dict[str, list[Occurrence]] = field(default_factory=dict)


def utf16_len(ch: str) -> int:
    """单个 Python 字符（码点）占用的 UTF-16 code unit 数。"""
    return 2 if ord(ch) > 0xFFFF else 1


def line_lengths_utf16(text: str) -> list[int]:
    """每行（不含换行符）的 UTF-16 长度，索引为 0-based 行号。"""
    lengths: list[int] = []
    cur = 0
    for ch in text:
        if ch == "\n":
            lengths.append(cur)
            cur = 0
        else:
            cur += utf16_len(ch)
    lengths.append(cur)
    return lengths


class _Counter:
    """在按 Python 字符扫描时同步维护 UTF-16 列。"""

    def __init__(self) -> None:
        self.py_col = 0
        self.u16_col = 0

    def add(self, ch: str) -> None:
        self.py_col += 1
        self.u16_col += utf16_len(ch)


def _pos(line_no: int, col: _Counter, line_start: int) -> Position:
    return Position(
        line=line_no + 1, column=col.u16_col + 1, offset=line_start + col.py_col
    )


def parse(text: str) -> ParsedDocument:
    doc = ParsedDocument()
    lines = text.split("\n")

    line_start = 0
    for line_no, line in enumerate(lines):
        line_u16_length = sum(utf16_len(c) for c in line)
        col = _Counter()
        i = 0
        n = len(line)

        def skip_ws() -> None:
            nonlocal i
            while i < n and line[i] in " \t":
                col.add(line[i])
                i += 1

        skip_ws()

        # 注释行 / 行尾注释
        if i < n and line[i] == TOKEN_SEPARATOR:
            comment_start = _pos(line_no, col, line_start)
            comment_end_col = _Counter()
            comment_end_col.py_col = n
            comment_end_col.u16_col = line_u16_length
            doc.comment_ranges.append(
                Range(comment_start, _pos(line_no, comment_end_col, line_start))
            )
        elif i < n:
            # 解析一条语句：keyword NAME
            m = NAME_RE.match(line, i)
            # 行首非法字符（含非 ASCII）诊断
            if not m or m.group(0) not in KEYWORDS:
                start = _pos(line_no, col, line_start)
                end_col = _Counter()
                end_col.py_col = i + 1
                end_col.u16_col = col.u16_col + (utf16_len(line[i]) if i < n else 1)
                bad = line[i]
                if ord(bad) > 127:
                    msg = f"非法字符 U+{ord(bad):04X}：非 ASCII 只能出现在 '#' 注释中"
                else:
                    msg = f"语法错误：意外的字符 {bad!r}，行首应为 def、use 或 '#'"
                doc.diagnostics.append(
                    Diagnostic(
                        "syntax",
                        msg,
                        "error",
                        Range(start, _pos(line_no, end_col, line_start)),
                    )
                )
            else:
                kw = m.group(0)
                kw_start = _pos(line_no, col, line_start)
                for ch in kw:
                    col.add(ch)
                i = m.end()
                skip_ws()

                nm = NAME_RE.match(line, i) if i < n else None
                if nm is None:
                    # 给出一个 0 宽或 1 宽的位置
                    start = _pos(line_no, col, line_start)
                    if i < n and ord(line[i]) > 127:
                        bad = line[i]
                        end_col = _Counter()
                        end_col.py_col = i + 1
                        end_col.u16_col = col.u16_col + utf16_len(bad)
                        rng = Range(start, _pos(line_no, end_col, line_start))
                        msg = f"非法符号名：{bad} 是非 ASCII 字符，符号名只能用 ASCII"
                        col.add(bad)
                        i += 1
                    elif i < n:
                        bad = line[i]
                        end_col = _Counter()
                        end_col.py_col = i + 1
                        end_col.u16_col = col.u16_col + 1
                        rng = Range(start, _pos(line_no, end_col, line_start))
                        msg = f"语法错误：{kw} 后应为 ASCII 符号名，遇到 {bad!r}"
                        col.add(bad)
                        i += 1
                    else:
                        rng = Range(start, start)
                        msg = f"语法错误：{kw} 后缺少符号名"
                    doc.diagnostics.append(Diagnostic("syntax", msg, "error", rng))
                else:
                    name = nm.group(0)
                    name_start = _pos(line_no, col, line_start)
                    for ch in name:
                        col.add(ch)
                    i = nm.end()
                    name_end = _pos(line_no, col, line_start)
                    occ = Occurrence(
                        kind=kw, name=name, range=Range(name_start, name_end)
                    )
                    doc.occurrences.append(occ)
                    if kw == "def":
                        doc.defs.setdefault(name, []).append(occ)

                    skip_ws()
                    # 语句后只允许注释或行尾
                    if i < n and line[i] != TOKEN_SEPARATOR:
                        start = _pos(line_no, col, line_start)
                        bad = line[i]
                        end_col = _Counter()
                        end_col.py_col = i + 1
                        end_col.u16_col = col.u16_col + utf16_len(bad)
                        msg = f"语法错误：符号名后只允许行尾注释，遇到 {bad!r}"
                        doc.diagnostics.append(
                            Diagnostic(
                                "syntax",
                                msg,
                                "error",
                                Range(start, _pos(line_no, end_col, line_start)),
                            )
                        )
                        col.add(bad)
                        i += 1
                    elif i < n and line[i] == TOKEN_SEPARATOR:
                        comment_start = _pos(line_no, col, line_start)
                        comment_end_col = _Counter()
                        comment_end_col.py_col = n
                        comment_end_col.u16_col = line_u16_length
                        doc.comment_ranges.append(
                            Range(
                                comment_start,
                                _pos(line_no, comment_end_col, line_start),
                            )
                        )

        line_start += len(line) + 1  # +1 for '\n'

    # 语义诊断：重复定义；引用未定义
    for name, occs in doc.defs.items():
        if len(occs) > 1:
            for occ in occs[1:]:
                doc.diagnostics.append(
                    Diagnostic(
                        "duplicate-def",
                        f"符号 {name!r} 重复定义（首次定义在第 {occs[0].range.start.line} 行）",
                        "error",
                        occ.range,
                    )
                )

    defined = set(doc.defs)
    for occ in doc.occurrences:
        if occ.kind == "use":
            if occ.name in defined:
                for d in doc.defs[occ.name]:
                    d.use_count += 1
            else:
                doc.diagnostics.append(
                    Diagnostic(
                        "undefined-name",
                        f"引用了未定义的符号 {occ.name!r}",
                        "error",
                        occ.range,
                    )
                )

    # 未使用定义：信息级
    for name, occs in doc.defs.items():
        if occs and occs[0].use_count == 0:
            doc.diagnostics.append(
                Diagnostic(
                    "unused-def",
                    f"符号 {name!r} 已定义但从未使用",
                    "info",
                    occs[0].range,
                )
            )

    # 稳定顺序：按 (行, 列) 排序
    doc.diagnostics.sort(key=lambda d: (d.range.start.line, d.range.start.column))
    return doc


def is_ascii_name(name: str) -> bool:
    return bool(NAME_RE.fullmatch(name))
