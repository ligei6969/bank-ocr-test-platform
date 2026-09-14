"""命令行入口。

三种用法：

    python -m ai_service                      # 启动 HTTP 服务
    python -m ai_service --demo               # 用内置样例跑一次完整链路
    python -m ai_service --search "反光了怎么办"
    python -m ai_service --explain <payload.json>

``--demo`` 不依赖网络：默认 LLM 未配置，会走确定性降级分支，
所以它同时也是一个「降级路径是否可用」的自检命令。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

from ai_service.explain import ReviewContext, build_explainer
from ai_service.llm import build_llm_client

DEMO_CONTEXT: dict[str, Any] = {
    "request_id": "demo-3f9a1c8e",
    "doc_type": "bank_card",
    "review_result": "review",
    "quality_result": "review",
    "quality_reasons": ["image_blur"],
    "review_reasons": ["missing_valid_date", "image_blur"],
    "fields": {"card_number": "6222********1234", "name": "张*"},
    "question": "这张卡为什么需要人工复核？该怎么让用户重拍？",
}


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _run_server(args: argparse.Namespace) -> int:
    import uvicorn

    from ai_service.api import get_host, get_port

    uvicorn.run(
        "ai_service.api:app",
        host=args.host or get_host(),
        port=args.port or get_port(),
        log_level="debug" if args.verbose else "info",
    )
    return 0


async def _explain(payload: dict[str, Any], top_k: int) -> dict[str, Any]:
    explainer = build_explainer(llm=build_llm_client())
    return await explainer.explain(ReviewContext.from_payload(payload), top_k=top_k)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_service",
        description="银行 OCR 平台 AI 复核助手服务",
    )
    parser.add_argument("--host", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, help="监听端口，默认 8100")
    parser.add_argument("--demo", action="store_true", help="用内置样例跑一次完整链路并打印结果")
    parser.add_argument("--search", metavar="QUERY", help="只做检索，打印命中的知识片段")
    parser.add_argument("--explain", metavar="JSON_FILE", help="读取 JSON 上下文文件并生成解释")
    parser.add_argument("--top-k", type=int, default=5, help="检索返回条数，默认 5")
    parser.add_argument("--indent", type=int, default=2, help="JSON 输出缩进，0 表示紧凑")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    if args.search:
        explainer = build_explainer(llm=build_llm_client())
        hits = explainer._retriever.search(args.search, top_k=args.top_k)  # noqa: SLF001
        print(json.dumps(
            {"query": args.search, "count": len(hits), "hits": [hit.to_dict() for hit in hits]},
            ensure_ascii=False,
            indent=args.indent or None,
        ))
        return 0

    if args.explain:
        path = Path(args.explain)
        if not path.is_file():
            print(f"找不到文件: {path}", file=sys.stderr)
            return 2
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = asyncio.run(_explain(payload, args.top_k))
        print(json.dumps(result, ensure_ascii=False, indent=args.indent or None))
        return 0

    if args.demo:
        result = asyncio.run(_explain(DEMO_CONTEXT, args.top_k))
        print(json.dumps(result, ensure_ascii=False, indent=args.indent or None))
        return 0

    return _run_server(args)


if __name__ == "__main__":
    raise SystemExit(main())
