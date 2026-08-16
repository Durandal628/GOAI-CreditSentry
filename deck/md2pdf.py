#!/usr/bin/env python3
"""把 Markdown 文档渲染成 A4 PDF。

为什么不直接手写一份 HTML：手写的那份会和 `.md` 漂移。
提交材料与仓库文档说的必须是同一件事，因此渲染源只能有一个——那就是 `.md` 本身。

只支持这份文档实际用到的语法子集（标题、引用、分隔线、段落、**加粗**、`代码`），
遇到不支持的语法原样输出而不是猜——静默猜错比原样保留更难发现。

用法::

    python3 deck/md2pdf.py docs/作品简介.md deck/作品简介.pdf
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import tempfile

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium",
]

CSS = """
:root{
  --ink:#161B21; --ink-2:#414B53; --ink-3:#7C878E;
  --rule:#D8DEDC; --accent:#0E5C63; --accent-soft:#EAF2F2; --chip:#F1F4F3;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#fff;color:var(--ink);
  font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",system-ui,sans-serif;
  font-size:10.5pt;line-height:1.85;-webkit-font-smoothing:antialiased;}
.page{width:210mm;min-height:297mm;padding:20mm 20mm 16mm;margin:0 auto;}

h1{font-size:21pt;font-weight:700;letter-spacing:-.01em;margin:0 0 4mm;line-height:1.25;}
.meta{font-size:9pt;color:var(--ink-2);line-height:1.75;border-left:2.5px solid var(--accent);
  padding:2mm 0 2mm 4mm;margin:0 0 5mm;background:var(--accent-soft);}
.meta code{background:transparent;color:var(--accent);font-weight:600;}
hr{border:0;border-top:1px solid var(--rule);margin:5mm 0;}

p{margin:0 0 3.6mm;text-align:justify;}
/* 每段以「**标题**：」开头，把它提成一个可扫读的小节抬头 */
p.sec{margin:0 0 4mm;}
p.sec > b.lead{display:block;font-size:11pt;color:var(--accent);font-weight:700;
  letter-spacing:.02em;margin-bottom:1.2mm;}
b{color:var(--ink);font-weight:600;}
code{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:.9em;
  background:var(--chip);border:1px solid var(--rule);border-radius:2px;padding:0 3px;}

.foot{margin-top:8mm;padding-top:3mm;border-top:1px solid var(--rule);
  font-size:8pt;color:var(--ink-3);display:flex;justify-content:space-between;gap:8mm;}
.foot span:last-child{font-family:ui-monospace,Menlo,monospace;}

@page{size:A4;margin:0;}
@media print{html,body{background:#fff;} .page{margin:0;}}
"""


def inline(text: str) -> str:
    """行内标记。先转义再还原，避免文档里的尖括号被当成标签。"""
    t = html.escape(text)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    return t


def render(md: str) -> str:
    out: list[str] = []
    quote: list[str] = []

    def flush_quote() -> None:
        if quote:
            out.append('<div class="meta">' + "<br>".join(quote) + "</div>")
            quote.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("> "):
            quote.append(inline(line[2:]))
            continue
        flush_quote()
        if not line.strip():
            continue
        if line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("---"):
            out.append("<hr>")
        else:
            body = inline(line)
            # 「**小节名**：正文」→ 把小节名提成抬头，正文另起一行
            m = re.match(r"^<b>(.+?)</b>：(.*)$", body)
            if m:
                out.append(f'<p class="sec"><b class="lead">{m.group(1)}</b>{m.group(2)}</p>')
            else:
                out.append(f"<p>{body}</p>")
    flush_quote()
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    md = open(src, encoding="utf-8").read()

    page = (f"<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
            f"<title>{os.path.basename(src)}</title><style>{CSS}</style></head><body>"
            f'<div class="page">{render(md)}'
            f'<div class="foot"><span>信衡 CreditSentry · GOAI 赛道一 新智基座｜Agent Infra</span>'
            f'<span>github.com/Durandal628/GOAI-CreditSentry</span></div>'
            f"</div></body></html>")

    chrome = next((c for c in CHROME_CANDIDATES if os.path.exists(c)), None)
    if not chrome:
        print("未找到 Chrome / Edge / Chromium，无法导出 PDF", file=sys.stderr)
        return 1

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                     encoding="utf-8") as f:
        f.write(page)
        tmp = f.name
    try:
        subprocess.run([chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={os.path.abspath(dst)}",
                        "--virtual-time-budget=8000", f"file://{tmp}"],
                       capture_output=True, check=True)
    finally:
        os.unlink(tmp)
    print(f"{dst}　{os.path.getsize(dst) / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
