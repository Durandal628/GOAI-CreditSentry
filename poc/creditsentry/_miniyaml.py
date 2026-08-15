"""YAML 子集解析器 —— 为了保住「零第三方依赖」而自带的最小实现。

我们需要读 ``config/*.yaml``，但引入 PyYAML 会破坏「三条命令、零依赖、零网络」
这个复现承诺。配置文件的 Schema 由我们自己定义，因此只需支持一个**明确划定的子集**：

- 映射：``key: value``，以缩进表达嵌套（缩进必须用空格，制表符直接报错）
- 块列表：``- 标量`` 与 ``- key: value``（后续更深缩进行归属该项）
- 内联列表：``[a, b, c]``
- 标量：字符串（可带单/双引号）、整数、浮点、``true`` / ``false``、``null`` / ``~``
- 注释：整行 ``#`` 开头，或值之后的 `` #``（URL 中的 ``#`` 不受影响）

**不支持**：锚点与引用、多文档、块标量（``|`` / ``>``）、复杂键、流式映射 ``{}``。
遇到不支持的语法一律抛 ``YamlError``，而不是静默解析出错误结果——
配置解析静默出错比直接失败危险得多。
"""

from __future__ import annotations

from typing import Any


class YamlError(Exception):
    """配置文件语法超出支持的子集，或格式非法。"""


def _strip_comment(line: str) -> str:
    """去掉行尾注释。只在 ``#`` 前有空白时才视为注释，避免误伤 URL 中的锚点。"""
    if line.lstrip().startswith("#"):
        return ""
    out: list[str] = []
    quote: str | None = None
    prev_space = False
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            prev_space = False
            continue
        if ch == "#" and prev_space:
            break
        out.append(ch)
        prev_space = ch in " \t"
    return "".join(out).rstrip()


def _split_inline(text: str) -> list[str]:
    """按逗号切分内联列表，忽略引号与嵌套括号内的逗号。"""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    buf: list[str] = []
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "[":
            depth += 1
            buf.append(ch)
        elif ch in "]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _scalar(tok: str) -> Any:
    t = tok.strip()
    if not t:
        return None
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        return t[1:-1]
    if t.startswith("{"):
        raise YamlError(f"不支持流式映射：{t!r}（请改用缩进块）")
    if t.startswith("[") and t.endswith("]"):
        return [_scalar(x) for x in _split_inline(t[1:-1])]
    if t in ("|", ">"):
        raise YamlError("不支持块标量（| / >）")
    low = t.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _lines(text: str) -> list[tuple[int, str, int]]:
    """归一化为 (缩进, 内容, 原始行号)，丢弃空行与注释行。"""
    out: list[tuple[int, str, int]] = []
    for no, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError(f"第 {no} 行缩进含制表符，请改用空格")
        s = _strip_comment(raw)
        if not s.strip():
            continue
        out.append((len(s) - len(s.lstrip(" ")), s.strip(), no))
    return out


def _parse_block(rows: list[tuple[int, str, int]], i: int, indent: int) -> tuple[Any, int]:
    if rows[i][1].startswith("- ") or rows[i][1] == "-":
        return _parse_list(rows, i, indent)
    return _parse_map(rows, i, indent)


def _parse_map(rows: list[tuple[int, str, int]], i: int, indent: int) -> tuple[dict, int]:
    out: dict[str, Any] = {}
    while i < len(rows) and rows[i][0] == indent:
        _, line, no = rows[i]
        if line.startswith("- "):
            break
        if ":" not in line:
            raise YamlError(f"第 {no} 行不是合法的 'key: value'：{line!r}")
        key, _, rest = line.partition(":")
        key = key.strip()
        if key in out:
            raise YamlError(f"第 {no} 行重复键：{key!r}")
        rest = rest.strip()
        if rest:
            out[key] = _scalar(rest)
            i += 1
            continue
        # 值在下一层：可能是更深缩进的块，也可能是同缩进的块列表
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if nxt and nxt[0] > indent:
            out[key], i = _parse_block(rows, i + 1, nxt[0])
        elif nxt and nxt[0] == indent and nxt[1].startswith("- "):
            out[key], i = _parse_list(rows, i + 1, indent)
        else:
            out[key] = None
            i += 1
    return out, i


def _parse_list(rows: list[tuple[int, str, int]], i: int, indent: int) -> tuple[list, int]:
    out: list[Any] = []
    while i < len(rows) and rows[i][0] == indent and rows[i][1].startswith(("- ", "-")):
        _, line, no = rows[i]
        item = line[1:].strip()
        if not item:
            raise YamlError(f"第 {no} 行为空列表项")
        # "- key: value"：该项是映射，后续更深缩进行继续归属它
        if ":" in item and not item[0] in "\"'[":
            key, _, rest = item.partition(":")
            sub: dict[str, Any] = {}
            item_indent = indent + 2
            if rest.strip():
                sub[key.strip()] = _scalar(rest)
                i += 1
            else:
                nxt = rows[i + 1] if i + 1 < len(rows) else None
                if nxt and nxt[0] > indent:
                    sub[key.strip()], i = _parse_block(rows, i + 1, nxt[0])
                else:
                    sub[key.strip()] = None
                    i += 1
            while i < len(rows) and rows[i][0] >= item_indent and not rows[i][1].startswith("- "):
                rest_map, i = _parse_map(rows, i, rows[i][0])
                sub.update(rest_map)
            out.append(sub)
        else:
            out.append(_scalar(item))
            i += 1
    return out, i


def parse(text: str) -> Any:
    """解析 YAML 子集文本。语法超出子集时抛 :class:`YamlError`。"""
    rows = _lines(text)
    if not rows:
        return None
    value, idx = _parse_block(rows, 0, rows[0][0])
    if idx != len(rows):
        _, line, no = rows[idx]
        raise YamlError(f"第 {no} 行缩进不一致，无法归属：{line!r}")
    return value


def load(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return parse(f.read())
