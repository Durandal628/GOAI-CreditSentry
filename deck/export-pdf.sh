#!/usr/bin/env bash
# 把 deck/index.html 导出为 16:9 的提交用 PDF。
# 用法：bash deck/export-pdf.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$DIR/index.html"
OUT="$DIR/信衡CreditSentry-初赛方案.pdf"

CHROME=""
for c in \
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  "/Applications/Chromium.app/Contents/MacOS/Chromium" \
  "$(command -v google-chrome || true)" \
  "$(command -v chromium || true)"
do
  if [ -n "$c" ] && [ -x "$c" ]; then CHROME="$c"; break; fi
done

if [ -z "$CHROME" ]; then
  echo "未找到 Chrome / Edge / Chromium。"
  echo "可改用手动导出：浏览器打开 $SRC → 打印 → 目标「另存为 PDF」→"
  echo "  纸张自定义 1280×720px（或 A4 横向）、边距「无」、勾选「背景图形」。"
  exit 1
fi

echo "使用浏览器：$CHROME"
"$CHROME" \
  --headless \
  --disable-gpu \
  --no-pdf-header-footer \
  --print-to-pdf="$OUT" \
  --virtual-time-budget=6000 \
  "file://$SRC"

echo "已生成：$OUT"
