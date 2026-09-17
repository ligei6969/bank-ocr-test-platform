"""银行业务知识客服 Agent（`ai_service/` 内的第二个 surface）。

与审核 Agent 的关系
-------------------
共用内核：``tool_manager`` 的工具框架（白名单 / schema / 熔断 / 缓存 / 兜底）、
``prompts`` 的版本化注册表、``structured`` 的结构化解析、``llm`` 的可插拔适配、
``cassette`` 的录制回放、``agentkit`` 的轨迹断言。
新增的只有：一套客服语料、一套客服工具、一个 persona prompt、以及**边界策略**。

不新建仓库、不新建进程 —— 它是同一个服务里的第二个产品面。

三条安全边界（每一条都有测试，且**不只靠 prompt**）
--------------------------------------------------
1. 不泄露审核内部信息（原因码、OCR 原文、完整证件号、内部阈值）
2. 不给个性化金融建议（额度、利率、能否批卡、该办哪张）
3. 不编造业务规则（答不出就说答不出，并转人工）

第 3 条的兜法来自审核 Agent 的教训：``规则必须优先于模型``。
审核侧曾经把「证据不足必须转人工」只写在降级路径里，结果模型回一句 finish
就绕过去了。这里同理 —— 越界的**判定与拒答话术是确定性代码**，
模型只负责把在范围内的知识组织成语言。
"""

from ai_service.knowledge.agent import (
    KNOWLEDGE_AGENT_PROMPT_ID,
    KnowledgeAgent,
    KnowledgeBudget,
    KnowledgeOutcome,
    build_knowledge_agent,
    run_knowledge_ask,
)
from ai_service.knowledge.session import (
    DEFAULT_MAX_TURNS,
    SessionHistory,
    Turn,
    turn_from_outcome,
)

__all__ = (
    "DEFAULT_MAX_TURNS",
    "KNOWLEDGE_AGENT_PROMPT_ID",
    "KnowledgeAgent",
    "KnowledgeBudget",
    "KnowledgeOutcome",
    "SessionHistory",
    "Turn",
    "build_knowledge_agent",
    "run_knowledge_ask",
    "turn_from_outcome",
)
