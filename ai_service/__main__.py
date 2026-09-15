"""命令行入口。

五种用法：

    python -m ai_service                      # 启动 HTTP 服务
    python -m ai_service --demo               # 用内置样例跑一次完整链路
    python -m ai_service --agent              # 用 Agent 路径跑一次（多步工具决策）
    python -m ai_service --live               # 真实调用一次模型（需配 LLM_API_KEY）
    python -m ai_service --search "反光了怎么办"
    python -m ai_service --explain <payload.json>

``--demo`` 不依赖网络：默认 LLM 未配置，会走确定性降级分支，
所以它同时也是一个「降级路径是否可用」的自检命令。

``--agent`` 走 P1 的多步决策路径，会打印每一步的工具调用与预算消耗，
不加 ``--live`` 时同样是离线的（确定性工具序列）。

``--live`` 是 ``--demo`` 的真实模型版本，用来手动冒烟：确认 key 配对了、
模型回了、结构化解析过了。**默认路径永远是离线的**，只有显式加 ``--live``
才会出网。
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


LIVE_ENV_HINT = (
    "配置方式（任选一组）：\n"
    "  LLM_API_KEY=<key>                  # 也可用 OPENAI_API_KEY / ANTHROPIC_API_KEY\n"
    "  LLM_PROVIDER=openai|anthropic      # 有 key 但没写时按 key 前缀自动判断\n"
    "  LLM_BASE_URL=<网关地址>             # 走中转站 / 自建网关时必填\n"
    "  LLM_MODEL=<模型名>\n"
    "\n注意：本命令会真实出网并产生费用；CI 与默认路径都不会调用它。"
)


def _run_live(args: argparse.Namespace) -> int:
    """真实调用一次模型跑完整链路，用于手动冒烟。

    没有可用模型时**只打印提示并返回非零码**，不抛异常 ——
    冒烟入口的作用是帮人排错，它自己不该变成一个需要排错的错误。
    """
    llm = build_llm_client()
    if not llm.available:
        print("[--live] 没有可用的模型，无法执行真实调用。", file=sys.stderr)
        print(f"[--live] 原因：{getattr(llm, 'reason', '未配置')}", file=sys.stderr)
        print(LIVE_ENV_HINT, file=sys.stderr)
        return 2

    # 诊断信息一律走 stderr，stdout 保持纯 JSON，方便直接管道给 jq
    print(f"[--live] 使用模型：{llm.name}", file=sys.stderr)
    explainer = build_explainer(llm=llm)
    try:
        result = asyncio.run(
            explainer.explain(ReviewContext.from_payload(DEMO_CONTEXT), top_k=args.top_k)
        )
    except Exception as exc:  # noqa: BLE001 - 冒烟入口要给出可读结论而不是栈
        print(f"[--live] 调用失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=args.indent or None))
    print(
        "[--live] 生效引擎："
        f"generation={result['engine']['generation']} "
        f"rewrite={result['engine']['rewrite']} "
        f"rerank={result['engine']['rerank']}",
        file=sys.stderr,
    )
    print(
        f"[--live] prompt 版本：{result['prompt_versions']['used'] or '（未调用模型）'}",
        file=sys.stderr,
    )
    return 0


def _run_agent(args: argparse.Namespace) -> int:
    """用 Agent 路径跑一次内置样例，并打印决策轨迹。"""
    from ai_service.agent import AgentBudget, run_agent_for_context

    llm = build_llm_client()
    budget = AgentBudget(
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
    )
    result = asyncio.run(
        run_agent_for_context(
            ReviewContext.from_payload(DEMO_CONTEXT),
            llm=llm,
            budget=budget,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=args.indent or None))

    steps = [entry for entry in result["trace"] if entry.get("executed")]
    print(
        f"[--agent] 决策引擎={result['engine']['decision']} "
        f"停止原因={result['stop_reason']} "
        f"truncated={result['truncated']}",
        file=sys.stderr,
    )
    print(f"[--agent] 工具序列={[entry['tool'] for entry in steps]}", file=sys.stderr)
    print(
        f"[--agent] 预算消耗={result['budget_used']}（上限 {result['budget']}）",
        file=sys.stderr,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_service",
        description="银行 OCR 平台 AI 复核助手服务",
    )
    parser.add_argument("--host", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, help="监听端口，默认 8100")
    parser.add_argument("--demo", action="store_true", help="用内置样例跑一次完整链路并打印结果")
    parser.add_argument(
        "--live",
        action="store_true",
        help="真实调用模型跑一次（需配 LLM_API_KEY；会出网并产生费用）",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help="用 Agent 路径跑一次内置样例（多步工具决策，打印轨迹与预算）",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=6,
        help="Agent 最大步数（默认 6）",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=3000,
        help="Agent token 预算（默认 3000，字符级估算）",
    )
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

    if args.live:
        return _run_live(args)

    if args.agent:
        return _run_agent(args)

    if args.demo:
        result = asyncio.run(_explain(DEMO_CONTEXT, args.top_k))
        print(json.dumps(result, ensure_ascii=False, indent=args.indent or None))
        return 0

    return _run_server(args)


if __name__ == "__main__":
    raise SystemExit(main())
