#!/usr/bin/env python3
"""OpenAI 兼容的故障注入伪端点。

**为什么需要它。** `--llm live` 新增的三段代码——传输重试、Schema 修复轮、
失败策略降级——有一个共同特点：**只在模型表现不好时才会被执行到**。
拿真实端点测不了它们，因为你无法要求 qwen「请这次返回一个非法枚举值」。
于是这三条路径要么永远不被覆盖，要么等真机上线那天第一次被执行到。

这个伪端点把「模型表现不好」变成一个可点播、可复现、零成本的输入。

**关键设计：正常模式下它调用确定性推理器。** 这意味着 `--fault none` 时，
经 HTTP + JSON 解析 + 归一化 + Schema 校验的 live 路径，产出应当与 stub 模式
**逐字节相同**。任何差异都说明新加的这几层扭曲了内容——这正是
``tools/live_conformance.py`` 断言的东西。伪端点因此不只是「假装有个模型」，
它是一把标尺。

用法::

    python3 tools/mock_llm_server.py --fault none        # 前台起服务
    python3 tools/mock_llm_server.py --fault bad-enum --port 8077

    # 另一个终端
    python3 poc/run_demo.py --case CASE-001 --llm live --preset offline-mock

通常不需要手工起：``tools/live_conformance.py`` 会在进程内拉起并逐个故障模式回归。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from poc.creditsentry.llm import _STUB_REASONERS  # noqa: E402

DEFAULT_PORT = 8077

#: 全部故障模式。键是 CLI 取值，值是它检验的代码路径——
#: 每一条都对应 llm.py / schemas.py 里的一段具体逻辑，不是随手编的坏输入。
FAULTS: dict[str, str] = {
    "none": "正常返回（应与 stub 模式逐字节一致）",
    "prose": "JSON 外包一层寒暄与 ``` 围栏 → 检验 _extract_json",
    "bad-enum": "verdict/conclusion 取非法枚举值 → 检验 Schema 校验 + 修复轮",
    "loose-format": "置信度写成 \"62%\"、单值不加列表 → 检验 normalize 不该触发修复轮",
    "no-evidence": "根因不带 evidence_ids → 检验「无证据不决策」的 Schema 执行点",
    "fake-evidence": "引用不存在的证据编号 → 检验 Skill 注入的语义校验器",
    "partial-checklist": "质疑清单只回执一项 → 检验完成性契约",
    "resolution-by-target": "回执只给 target 不给 item_id → 检验回执对齐兜底",
    "always-bad": "每次都返回非法输出 → 检验修复轮用尽后的失败策略",
    "http500": "首次 500 → 检验传输层退避重试",
    "ratelimit": "首次 429 → 检验限流重试",
    "auth-fail": "恒定 401 → 检验鉴权失败**不重试**、立即上抛",
    "no-json-mode": "拒绝 response_format → 检验 json_mode: auto 自动降级",
}


def _task_of(payload: dict[str, Any]) -> str:
    """从入参形状判断任务类型。

    质疑侧的 payload 必带 ``assertion`` 与 ``checklist``（两者由上下文装配器
    以 required=True 放入，永不被裁剪），定性侧则没有。这个区分因此是稳定的。
    """
    return "devils_advocate" if "assertion" in payload else "risk_root_cause"


def _good(payload: dict[str, Any]) -> dict[str, Any]:
    """正常输出：直接跑确定性推理器，保证与 stub 模式同源。"""
    return _STUB_REASONERS[_task_of(payload)](payload)


def _corrupt(fault: str, payload: dict[str, Any]) -> Any:
    """按故障模式把正常输出改坏。改坏的方式刻意贴近真实模型的常见失误。"""
    task = _task_of(payload)
    out = _good(payload)

    if fault == "bad-enum":
        if task == "risk_root_cause":
            out["conclusion"] = "HIGH_RISK"          # 模型自造枚举值
        else:
            out["verdict"] = "MAYBE_REFUTED"
    elif fault == "loose-format":
        # 这一类**不应该**触发修复轮：它是表述偏差，normalize 就该吸收掉
        if task == "risk_root_cause":
            for c in out.get("root_causes", []):
                c["confidence"] = f"{c['confidence'] * 100:.0f}%"
                if len(c.get("evidence_ids", [])) == 1:
                    c["evidence_ids"] = c["evidence_ids"][0]   # 单值不加列表
            out = {"result": out}                              # 外层包装
        else:
            out["verdict"] = out["verdict"].lower()
    elif fault == "no-evidence" and task == "risk_root_cause":
        for c in out.get("root_causes", []):
            c["evidence_ids"] = []
    elif fault == "fake-evidence" and task == "risk_root_cause":
        for c in out.get("root_causes", []):
            c["evidence_ids"] = ["EV-0000-0000"]
    # 下面两类只存在于质疑侧。必须显式判 task——否则会给定性侧的输出
    # 塞进一个它本不该有的字段，等价性比对就会因为伪端点自己的缺陷而失败
    elif fault == "partial-checklist" and task == "devils_advocate":
        out["checklist_resolutions"] = out.get("checklist_resolutions", [])[:1]
    elif fault == "resolution-by-target" and task == "devils_advocate":
        # 只给 target 不给 item_id——中小档位模型的高频行为
        out["checklist_resolutions"] = [
            {k: v for k, v in r.items() if k != "item_id"}
            for r in out.get("checklist_resolutions", [])
        ]
    elif fault == "always-bad":
        return {"note": "我认为这个案子风险不大", "confidence": "比较高"}
    return out


def _wrap_prose(obj: Any) -> str:
    return ("好的，我已经完成分析。以下是结构化结论：\n\n```json\n"
            + json.dumps(obj, ensure_ascii=False, indent=2)
            + "\n```\n\n如需进一步说明请告知。")


class _Handler(BaseHTTPRequestHandler):
    fault = "none"
    counter: dict[str, int] = {}
    lock = threading.Lock()
    verbose = False

    # ---- HTTP ------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802  (BaseHTTPRequestHandler 的命名约定)
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._send(404, {"error": {"message": f"未知路径 {self.path}"}})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        messages = body.get("messages", [])
        model = body.get("model", "mock")

        with self.lock:
            n = self.counter.get(model, 0) + 1
            self.counter[model] = n

        # 修复轮的判定：初次是 system + user 两条，修复轮会多出
        # assistant（模型上次原文）与 user（修复指令）两条
        is_repair = len(messages) > 2

        if err := self._maybe_http_error(n):
            return err
        if self.fault == "no-json-mode" and "response_format" in body:
            return self._send(400, {"error": {
                "message": "this model does not support response_format=json_object",
                "type": "invalid_request_error"}})

        try:
            payload = json.loads(messages[1]["content"])
        except (json.JSONDecodeError, IndexError, KeyError, TypeError):
            # 不是本系统的推理请求（例如 tools/preflight.py 的连通性探针）。
            # 照 OpenAI 语义返回一个最小合法响应即可，让伪端点也能当探针靶子用
            return self._send(200, {
                "id": "mock-probe", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 6},
            })

        # always-bad 恒定失败；其余故障只在首轮触发，修复轮给正确答案——
        # 这正是「模型收到错误提示后能改对」这一现实情形
        if self.fault == "always-bad":
            obj = _corrupt("always-bad", payload)
        elif is_repair or self.fault in ("none", "http500", "ratelimit", "no-json-mode"):
            obj = _good(payload)
        else:
            obj = _corrupt(self.fault, payload)

        content = _wrap_prose(obj) if (self.fault == "prose" and not is_repair) \
            else json.dumps(obj, ensure_ascii=False)
        if self.verbose:
            print(f"  [mock] {model} #{n} repair={is_repair} fault={self.fault} "
                  f"→ {len(content)} 字符", file=sys.stderr)
        return self._send(200, {
            "id": f"mock-{n}", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": len(json.dumps(messages)) // 4,
                      "completion_tokens": len(content) // 4},
        })

    def _maybe_http_error(self, n: int):
        """传输层故障。首次失败、随后成功，用于验证退避重试确实生效。"""
        if self.fault == "http500" and n == 1:
            return self._send(500, {"error": {"message": "internal error"}})
        if self.fault == "ratelimit" and n == 1:
            return self._send(429, {"error": {"message": "rate limit exceeded"}})
        if self.fault == "auth-fail":
            return self._send(401, {"error": {"message": "invalid api key"}})
        return None

    def _send(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a: Any) -> None:
        """默认静音。伪端点的日志会淹没被测程序自己的输出。"""


def serve(fault: str = "none", port: int = DEFAULT_PORT,
          verbose: bool = False) -> HTTPServer:
    """构造并返回一个未启动的 HTTPServer。调用方决定前台跑还是丢线程里。"""
    if fault not in FAULTS:
        raise SystemExit(f"未知故障模式：{fault}（可选：{'、'.join(FAULTS)}）")
    handler = type("_H", (_Handler,), {"fault": fault, "counter": {},
                                       "lock": threading.Lock(), "verbose": verbose})
    return HTTPServer(("127.0.0.1", port), handler)


def main() -> int:
    p = argparse.ArgumentParser(description="OpenAI 兼容的故障注入伪端点")
    p.add_argument("--fault", default="none", choices=sorted(FAULTS))
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--list-faults", action="store_true")
    args = p.parse_args()

    if args.list_faults:
        for name, desc in FAULTS.items():
            print(f"  {name:<22}{desc}")
        return 0

    srv = serve(args.fault, args.port, args.verbose)
    print(f"伪端点已启动 http://127.0.0.1:{args.port}/v1　故障模式：{args.fault}")
    print(f"  → {FAULTS[args.fault]}")
    print(f"另开终端跑：python3 poc/run_demo.py --case CASE-001 --llm live --preset offline-mock")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
