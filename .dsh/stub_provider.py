"""本地桩供应商：OpenAI 兼容的 `/chat/completions`。

**为什么需要它**：验收里有一条「构造『模型返回假 event_id』的用例能拦截并重试 / 降级」——
真模型不会配合你造假，而这一类输入恰恰是必须拦住的（红线 3：没有有效证据不能标 fact）。
桩让它变成可复现的真机场景：请求走**真实的 Gateway → Router → 链路**，
只有供应商那一端是假的。

默认返回一条引用不存在 `event_id` 的 `fact`，并把 token 用量报大
（便于配合极小的单次上限触发 mid-check）。

环境变量：
    STUB_PORT              监听端口（默认 8777）
    STUB_PROMPT_TOKENS     每次调用上报的输入 token（默认 8000）
    STUB_COMPLETION_TOKENS 每次调用上报的输出 token（默认 6000）

用法：python .dsh/stub_provider.py          # 前台跑，Ctrl+C 停
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PORT = int(os.environ.get("STUB_PORT", 8777))
PROMPT_TOKENS = int(os.environ.get("STUB_PROMPT_TOKENS", 8000))
COMPLETION_TOKENS = int(os.environ.get("STUB_COMPLETION_TOKENS", 6000))

#: 一条"看起来很正经"的结论，但证据 id 是编造的。
FAKE_CONTENT = json.dumps(
    {
        "insights": [
            {
                "type": "fact",
                "severity": "high",
                "confidence": 0.95,
                "title": "磁盘即将写满（证据：event 999999999）",
                "summary": "桩供应商编造的结论，引用了不存在的 event_id",
                "evidence_ids": ["999999999"],
            }
        ],
        "knowledge_candidates": [],
    },
    ensure_ascii=False,
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        payload = {
            "model": "stub-model",
            "choices": [{"message": {"role": "assistant", "content": FAKE_CONTENT}}],
            "usage": {
                "prompt_tokens": PROMPT_TOKENS,
                "completion_tokens": COMPLETION_TOKENS,
            },
        }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        """静音：桩的访问日志没有价值，反而会淹没真机的结构化日志。"""


def main() -> int:
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"桩供应商已就绪：http://127.0.0.1:{PORT}/chat/completions（每次上报 "
          f"{PROMPT_TOKENS}/{COMPLETION_TOKENS} tokens）")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
