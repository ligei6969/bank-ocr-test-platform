"""AI 复核助手服务（P0 最小切片）。

设计定位
--------
本服务是 ``bank-ocr-test-platform`` 的**独立 AI 能力进程**，不嵌入 ``app/``：
平台通过 HTTP（``app/ai_client.py``）调用它，AI 挂了审核主链路照常运行。

与 EchoMind 的关系
------------------
``retrieval.py`` 与 ``tool_manager.py`` 是从 EchoMind
（``mcp/knowledge_base.py``、``mcp/tool_manager.py``）移植并改造的：

* 保留其核心优化链路 —— **查询改写 → 并行召回 → 去重 → LLM 重排 → Top-K**，
  以及**三态熔断 / TTL 缓存 / 超时 / 降级**四件套；
* 去掉 ChromaDB 与 Redis 的硬依赖。原因是审核域语料规模很小
  （十余个原因码 + 少量规范文档），轻量词法检索在这里**比小模型向量检索更准、
  且结果确定、可单测**；向量检索保留为可选后端。
* 保留 EchoMind「外部能力不可用时自动降级」的思路：LLM 不可用时，
  改写、重排、生成三步全部切换到确定性策略，链路不中断。

运行
----
    python -m ai_service            # 启动 HTTP 服务（默认 127.0.0.1:8100）
    python -m ai_service --explain <request_id> ...
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
