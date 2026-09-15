"""Prompt 版本化注册表。

为什么要有这个模块
------------------
P0 的 prompt 是内联在 ``tool_manager.py`` / ``explain.py`` 里的裸字符串。这有三个问题：

1. **改了就说不清是哪版。** 解释质量变了，无法判断是模型换了、语料改了，还是
   prompt 被顺手改了。面试里被问「你怎么管理 prompt」时，内联字符串答不上来。
2. **没有稳定 id。** 结果里无法回传「本次用的是哪条 prompt 的哪一版」，
   线上排查只能靠翻代码 diff。
3. **无法集中评审。** prompt 是安全边界（本服务承诺「事实与措辞分离」正是靠
   prompt 约束模型不得编造阈值），散落在各处就没人审。

做法
----
每条 prompt 是一个不可变的 :class:`PromptTemplate`，带 ``id`` 与 ``version``：

* ``id`` 稳定不变，是这条 prompt 的身份；
* ``version`` 在**语义变化**时递增（改措辞、加约束都算），只改错别字不必递增；
* ``label`` 形如 ``explain_generate@v1``，进入 trace 与接口返回值，
  这样「这条解释是哪版 prompt 产出的」在数据里就能查到。

版本号是手写的，不是自动生成的 —— 有意如此。自动化版本号（内容哈希）的问题是
无法区分「改了错别字」和「改了语义」，而评审时真正关心的是后者。

注意：``body`` 使用 ``str.format`` 占位，因此模板里不能出现裸的 ``{`` / ``}``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping

PROMPT_REGISTRY_VERSION = "2026.09"


@dataclass(frozen=True)
class PromptTemplate:
    """一条可被引用、可被追踪版本的 prompt。"""

    id: str
    version: str
    purpose: str
    body: str
    system: str = ""

    @property
    def label(self) -> str:
        """稳定引用名，形如 ``explain_generate@v1``。"""
        return f"{self.id}@{self.version}"

    def render(self, /, **values: Any) -> str:
        """渲染模板。缺占位符会抛 ``KeyError``，这是有意的显式失败。"""
        return self.body.format(**values)


# ── 检索链路 ──────────────────────────────────────────────────────────────────

QUERY_REWRITE = PromptTemplate(
    id="query_rewrite",
    version="v1",
    purpose="把审核场景查询改写成多角度子查询，提升召回覆盖面",
    body=(
        "将以下审核场景的查询改写为 {n} 个不同角度的检索子查询，用于检索审核知识库。\n"
        "要求：每个子查询角度不同，分别覆盖「原因码含义」「处置建议」「拍摄规范」等不同方面。\n"
        '原始查询: "{query}"\n'
        '只返回 JSON 数组，例如: ["子查询1", "子查询2", "子查询3"]'
    ),
)


RERANK = PromptTemplate(
    id="rerank",
    version="v1",
    purpose="对召回结果按相关性重排，解决召回排序差",
    body=(
        "根据审核员的查询，对以下检索结果按相关性从高到低排序，返回 JSON 索引数组。\n"
        '查询: "{query}"\n'
        "检索结果:\n{listing}\n\n"
        "只返回 JSON 数组，例如 [最相关索引, ..., 最不相关索引]，不要任何其他文字。"
    ),
)


# ── 措辞生成 ──────────────────────────────────────────────────────────────────

EXPLAIN_GENERATE = PromptTemplate(
    id="explain_generate",
    version="v1",
    purpose="生成给审核员看的解释正文；事实以传入条目为准，模型只组织语言",
    system=(
        "你是银行影像审核的复核助手，服务对象是审核员。"
        "你只能用给定的【事实条目】和【知识片段】作答，不得编造阈值、规则或字段。"
        "输出中文，语气克制、专业，不使用营销语言，不重复免责声明。"
    ),
    body=(
        "审核员的问题：{question}\n\n"
        "证件类型：{doc_label}\n"
        "审核结论：{result_label}\n"
        "质量结果：{quality_result}\n"
        "错误信息：{error_message}\n\n"
        "【事实条目】（取自审核知识库，阈值与实现位置以这里为准）\n{facts}\n\n"
        "【知识片段】\n{knowledge}\n\n"
        "请输出一段 120 到 220 字的中文解释，回答三个问题："
        "① 这条记录为什么是这个结论；② 根因在哪一层（影像质量 / 字段解析 / 服务端）；"
        "③ 审核员接下来该做什么。直接输出解释正文，不要标题、不要 JSON、不要分点编号。"
    ),
)


# ── Agent 决策 ────────────────────────────────────────────────────────────────

AGENT_DECIDE = PromptTemplate(
    id="agent_decide",
    version="v1",
    purpose="Agent 的单步决策：在候选工具里选一个，或宣布收敛",
    system=(
        "你是一个银行影像审核 Agent，服务对象是审核员。"
        "你的职责是查清「这条记录为什么是这个结论」，而不是替审核员改判。"
        "你只能调用工具清单里列出的工具。"
        "阈值、规则、处置建议一律以工具返回的内容为准，不得凭记忆编造。"
        "只输出一个 JSON 对象，不要任何其他文字。"
    ),
    body=(
        "审核员的问题：{question}\n\n"
        "【记录上下文】\n{context_block}\n\n"
        "【可用工具】\n{tool_catalog}\n\n"
        "【已完成的步骤】\n{history}\n\n"
        "请决定下一步。输出 JSON 对象，字段含义：\n"
        "- thought：一句话推理，不超过 60 字；\n"
        '- action：要调用的工具名；信息已经足够时填 "finish"；\n'
        "- params：传给工具的参数对象，没有参数就填空对象；\n"
        '- answer：仅当 action 为 "finish" 时填写，120~220 字中文解释，'
        "回答「为什么是这个结论 / 根因在哪一层 / 审核员接下来做什么」。\n"
        "只允许使用上面列出的工具名。拿不准就调工具去查，不要猜。"
    ),
)


# ── 评测：LLM-as-Judge ────────────────────────────────────────────────────────

JUDGE_RUBRIC = PromptTemplate(
    id="judge_rubric",
    version="v1",
    purpose="对解释质量做四维 rubric 打分（评测层用，不进线上链路）",
    system=(
        "你是银行影像审核领域的评审专家，正在给一段 AI 解释打分。"
        "严格按 rubric 打分，不要因为文字流畅就抬高分数。"
        "你不知道答案该更长还是更短，长度本身不是评分依据。"
        "只输出一个 JSON 对象，不要任何其他文字。"
    ),
    body=(
        "【审核员的问题】\n{question}\n\n"
        "【这条记录的实际结论】{verdict}\n"
        "【涉及的原因码】{reason_codes}\n\n"
        "【可核对的事实条目】\n{facts}\n\n"
        "【待评分的解释】\n{answer}\n\n"
        "请按四个维度各打 0~5 分（整数或一位小数）：\n"
        "- relevance 相关性：是否正面回应了审核员的问题，而不是泛泛而谈；\n"
        "- accuracy 准确性：提到的阈值、规则、字段是否都能在上面的事实条目里找到，"
        "编造内容一律重扣；\n"
        "- completeness 完整性：是否讲清了「为什么是这个结论」「根因在哪一层」"
        "「接下来做什么」三件事；\n"
        "- usefulness 有用性：审核员看完能不能直接采取行动。\n\n"
        '只输出 JSON，例如：{{"relevance": 4, "accuracy": 5, "completeness": 3, '
        '"usefulness": 4, "rationale": "一句话理由"}}'
    ),
)


# ── 注册表 ────────────────────────────────────────────────────────────────────

_REGISTRY: Dict[str, PromptTemplate] = {
    prompt.id: prompt
    for prompt in (QUERY_REWRITE, RERANK, EXPLAIN_GENERATE, AGENT_DECIDE, JUDGE_RUBRIC)
}


def get_prompt(prompt_id: str) -> PromptTemplate:
    """按 id 取 prompt。id 不存在直接抛错 —— 拼错 id 必须立刻暴露。"""
    try:
        return _REGISTRY[prompt_id]
    except KeyError as exc:
        raise KeyError(f"未注册的 prompt id: {prompt_id}（已注册: {sorted(_REGISTRY)}）") from exc


def registry() -> Mapping[str, PromptTemplate]:
    """只读视图，供自检接口与测试遍历。"""
    return dict(_REGISTRY)


def label_of(prompt_id: str) -> str:
    """取 ``id@version``，用于写入 trace。"""
    return get_prompt(prompt_id).label


def collect_labels(trace: Any) -> Dict[str, str]:
    """从 trace 里收集本次实际**调用过**的 prompt 版本。

    trace 里每一步带 ``prompt`` 字段（形如 ``rerank@v1``）。语义是
    「这版 prompt 被送去模型了」，**不是**「这版 prompt 成功了」：

    * LLM 未配置 → 根本没调用，不报版本（报了就是假信息）；
    * LLM 调用了但返回垃圾 / 报错 → **报版本**，因为这正是排查时要找的线索
      （是 prompt 写坏了还是模型抽风），此时同一步还会带 ``prompt_failed``。

    只回传真正被调用过的 prompt，而不是把注册表整个倒出来。
    """
    found: Dict[str, str] = {}
    if not isinstance(trace, (list, tuple)):
        return found
    for entry in trace:
        if not isinstance(entry, dict):
            continue
        label = entry.get("prompt")
        if not isinstance(label, str) or "@" not in label:
            continue
        prompt_id, _, _version = label.partition("@")
        if prompt_id:
            found[prompt_id] = label
    return found
