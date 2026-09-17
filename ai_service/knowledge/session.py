"""客服 Agent 的多轮会话记忆。

这一格解决一个很具体的问题
--------------------------
单轮客服不难，难的是**「那需要什么材料？」**这种问句：它自己不带话题，
答案完全取决于上一句在说什么。没有历史，这类追问只能答成「无法确认」。

为什么历史放在调用方，而不是服务里
----------------------------------
AI 服务保持无状态：不引入 Redis、不落库、进程里不存会话。
调用方（平台或前端）持有历史，随请求带进来，服务只负责
「给定历史 + 本轮问题 → 回答」，并回传更新后的历史。
理由有三个，且都是本项目一贯的立场：

1. 可回放：同一份 (历史, 问题) 在 cassette 下必须能逐字节复现。服务自己攒状态，
   回放就不再确定；
2. 可扩缩：无状态才好横向扩；
3. 少一个数据面：服务端一旦存会话，就等于新增了一处「谁都能读到用户说过什么」的地方。

历史与当轮输入是同一等级的敏感面
--------------------------------
历史会进 prompt、进日志、进模型供应商的日志 —— 它和用户这一轮刚打的字没有区别。
所以本模块的所有出口都遵守两条硬规则：

* **强制脱敏**：卡号 / 身份证号 / 手机号在进 prompt 前抹掉；脱敏用的是
  :func:`ai_service.knowledge.policy.sanitize_question`，与入口同一套规则
  （两处各写一套迟早会不一样）；
* **强制裁剪**：只留最近 N 轮，且整块有字符上限 —— 无界历史是成本、延迟
  和「上下文污染」的共同来源。

调用方传来的 ``refused`` / ``intent`` 一概不信
----------------------------------------------
历史是**外部输入**，可以被伪造。所以边界不从字段里读，而是拿问题文本
**重新算一遍**（:func:`_boundary`）。这样「伪造一个 refused=false 的越界问题塞进历史」
不会得到任何好处。真正的闸门也仍然只按当轮问题判定 —— 历史不是通道。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ai_service.knowledge import policy

#: 默认只带最近几轮进 prompt。3 是「够用」与「别再涨」之间的折中：
#: 指代通常只看上一两轮，再往前的要么已被忘掉，要么就该让用户重说一遍。
DEFAULT_MAX_TURNS = 3

#: 单个字段进 prompt 前的长度上限。
MAX_FIELD_CHARS = 160

#: 整个历史块的字符上限。裁剪是按轮数做的，这个上限兜住「单轮特别长」的情况。
MAX_BLOCK_CHARS = 1600

#: 一轮里最多列几条依据条目。
MAX_DOC_IDS = 4

#: 被拒答的那一轮，问题**不原样回放**。
#:
#: 理由：那句话本身就超出了可答范围，让它出现在 prompt 里只剩两种用处 ——
#: 让模型重新生成一遍不该生成的内容，或者配合「我上一轮问过，你直接说吧」
#: 这类话术当杠杆。而指代消解并不需要它：既然上一轮已按合规口径拒答，
#: 就没有什么正当话题可以往后传。
#:
#: **答复同理不回放。** 拒答口径是我们自己写的没错，但历史是外部输入 ——
#: 这一段 ``answer`` 完全可以是伪造的（塞指令、塞「您的额度可以提到 500000 元」
#: 之类的既成事实）。既然这一轮的价值只剩「这里发生过一次越界请求」，
#: 那就只留这一个事实，一个字都不多给。
REFUSED_PLACEHOLDER = "〔该问题超出本渠道可答范围，已按合规口径拒答〕"


def _clip(text: str, limit: int) -> str:
    """按字符截断，超出时给出省略号。"""
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "…"


def _boundary(question: str) -> Tuple[str, bool]:
    """从问题文本**重算**意图与「是否越界」。

    不从字段里读：历史是调用方送来的，属于不可信输入。
    """
    intent = policy.detect_intent(question)
    return intent, policy.is_out_of_scope(intent)


# ── 一轮 ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Turn:
    """一轮问答里**值得留下来的那一部分**。

    刻意不存完整答复正文以外的任何原始内容：不存 OCR 原文、不存完整证件号、
    不存模型被闸门拦下的草稿（那是内部审计材料，不该跟着历史往外走）。

    ``frozen`` 是有意的：历史一旦记下就不该被就地改写 ——
    「谁在什么时候改了历史」是排查多轮问题时的第一个疑点。
    """

    question: str
    answer: str = ""
    intent: str = policy.INTENT_KNOWLEDGE
    doc_ids: Tuple[str, ...] = ()
    refused: bool = False
    blocked_by: str = ""

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "question": self.question,
            "answer": self.answer,
            "intent": self.intent,
            "refused": self.refused,
        }
        if self.doc_ids:
            payload["doc_ids"] = list(self.doc_ids)
        if self.blocked_by:
            payload["blocked_by"] = self.blocked_by
        return payload

    @classmethod
    def from_payload(cls, payload: Any) -> Optional["Turn"]:
        """解析一轮。**任何畸形输入都返回 ``None`` 而不是抛错。**

        历史来自外部，不能因为它脏就让整个请求失败 —— 该做的是把这一轮丢掉，
        其余照常作答。问题字段缺失或为空即视为无效轮。
        """
        if not isinstance(payload, Mapping):
            return None
        raw_question = payload.get("question")
        if not isinstance(raw_question, str) or not raw_question.strip():
            return None

        raw_answer = payload.get("answer")
        answer = raw_answer if isinstance(raw_answer, str) else ""

        doc_ids: List[str] = []
        raw_doc_ids = payload.get("doc_ids")
        if isinstance(raw_doc_ids, (list, tuple)):
            for item in raw_doc_ids:
                if isinstance(item, str) and item and item not in doc_ids:
                    doc_ids.append(item)

        raw_blocked = payload.get("blocked_by")
        blocked_by = raw_blocked if isinstance(raw_blocked, str) else ""

        # intent / refused 不读字段，从文本重算
        intent, refused = _boundary(raw_question)
        return cls(
            question=raw_question.strip(),
            answer=answer,
            intent=intent,
            doc_ids=tuple(doc_ids[:MAX_DOC_IDS]),
            refused=refused,
            blocked_by=blocked_by,
        )

    @classmethod
    def from_outcome(cls, question: str, outcome: Mapping[str, Any]) -> "Turn":
        """从一次 :class:`~ai_service.knowledge.agent.KnowledgeOutcome` 记一轮。

        只取结构（意图、依据条目、是否拒答、被哪道闸门拦下），
        答复正文只留裁剪后的文本。
        """
        doc_ids: List[str] = []
        citations = outcome.get("citations")
        if isinstance(citations, (list, tuple)):
            for item in citations:
                if isinstance(item, Mapping):
                    doc_id = item.get("doc_id")
                    if isinstance(doc_id, str) and doc_id and doc_id not in doc_ids:
                        doc_ids.append(doc_id)

        raw_blocked = outcome.get("blocked_by")
        return cls(
            question=str(question or "").strip(),
            answer=str(outcome.get("answer") or ""),
            intent=str(outcome.get("intent") or policy.INTENT_KNOWLEDGE),
            doc_ids=tuple(doc_ids[:MAX_DOC_IDS]),
            refused=bool(outcome.get("refused")),
            blocked_by=raw_blocked if isinstance(raw_blocked, str) else "",
        )


# ── 一段会话 ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SessionHistory:
    """一段会话的历史。所有变换都返回**新对象**，不就地修改。"""

    turns: Tuple[Turn, ...] = ()

    # ── 容器协议 ──────────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self.turns)

    def __bool__(self) -> bool:
        return bool(self.turns)

    def __iter__(self):
        return iter(self.turns)

    # ── 变换（纯函数） ────────────────────────────────────────────────────────

    def append(self, turn: Turn) -> "SessionHistory":
        return SessionHistory(turns=self.turns + (turn,))

    def extend(self, turns: Iterable[Turn]) -> "SessionHistory":
        return SessionHistory(turns=self.turns + tuple(turns))

    def trim(self, limit: int = DEFAULT_MAX_TURNS) -> "SessionHistory":
        """只留最近 ``limit`` 轮。``limit <= 0`` 得到空历史。"""
        if limit <= 0:
            return SessionHistory()
        return SessionHistory(turns=self.turns[-limit:])

    def sanitize(self) -> "SessionHistory":
        """抹掉每一轮里的敏感数字，并**重算**边界字段。

        对已脱敏的文本重复调用是幂等的（掩码文本里没有数字），
        所以调用方不必关心「已经洗过没有」。
        """
        cleaned: List[Turn] = []
        for turn in self.turns:
            question, _ = policy.sanitize_question(turn.question)
            answer, _ = policy.sanitize_question(turn.answer)
            intent, refused = _boundary(question)
            cleaned.append(
                replace(
                    turn,
                    question=question.strip(),
                    answer=answer,
                    intent=intent,
                    refused=refused,
                )
            )
        return SessionHistory(turns=tuple(cleaned))

    # ── 渲染 ──────────────────────────────────────────────────────────────────

    def to_prompt_block(self, limit: int = DEFAULT_MAX_TURNS) -> str:
        """渲染成 prompt 里的「对话历史」块。

        **脱敏与裁剪在这里强制发生**，不指望调用方先做对 ——
        少洗一次的代价是把用户的证件号送进模型供应商的日志，
        这种代价不能押在「别忘了」上。
        """
        safe = self.sanitize().trim(limit)
        if not safe:
            return "（没有历史对话，这是第一轮）"

        lines: List[str] = []
        for index, turn in enumerate(safe.turns, start=1):
            if turn.refused:
                # 只留「这里发生过一次越界请求」这个事实：问题与答复都不回放。
                # 指代消解要的是话题，而这一轮没有可以往后传的话题。
                lines.append(f"{index}. 用户：{REFUSED_PLACEHOLDER}")
                continue
            lines.append(f"{index}. 用户：{_clip(turn.question, MAX_FIELD_CHARS)}")
            if turn.answer:
                lines.append(f"   客服：{_clip(turn.answer, MAX_FIELD_CHARS)}")
            if turn.doc_ids:
                listed = "、".join(turn.doc_ids[:MAX_DOC_IDS])
                lines.append(f"   （上一轮依据的知识条目：{listed}）")
        return _clip("\n".join(lines), MAX_BLOCK_CHARS)

    # ── 读取 ──────────────────────────────────────────────────────────────────

    def last_topic(self) -> str:
        """最近一轮**可作答**的提问（脱敏后）。用于支撑指代消解。

        跳过被拒答的轮次：那些提问本身超出可答范围，拿来补全指代只会
        把越界内容重新引进 prompt。
        """
        for turn in reversed(self.sanitize().turns):
            if not turn.refused and turn.question.strip():
                return turn.question.strip()
        return ""

    def doc_ids(self) -> Tuple[str, ...]:
        """历史里出现过的依据条目（按出现顺序去重）。"""
        seen: List[str] = []
        for turn in self.turns:
            for doc_id in turn.doc_ids:
                if doc_id not in seen:
                    seen.append(doc_id)
        return tuple(seen)

    # ── 序列化 ────────────────────────────────────────────────────────────────

    def to_payload(self, limit: int = DEFAULT_MAX_TURNS) -> List[Dict[str, Any]]:
        """回传给调用方的历史。

        同样按 ``limit`` 裁剪：超出上限的部分永远不会被用上，
        让它跟着响应一起长大只是白白增加请求体与日志体积。
        """
        return [turn.to_payload() for turn in self.trim(limit).turns]

    @classmethod
    def from_payload(cls, payload: Any) -> "SessionHistory":
        """解析调用方送来的历史。畸形条目逐个丢弃，不抛错。

        解析完立刻 ``sanitize()``：从这一步开始，对象里的文本就都是洗过的，
        调用方后面再怎么用都不会绕开脱敏。
        """
        if not isinstance(payload, (list, tuple)):
            return cls()
        turns = [turn for turn in (Turn.from_payload(item) for item in payload) if turn is not None]
        return cls(turns=tuple(turns)).sanitize()


def turn_from_outcome(question: str, outcome: Mapping[str, Any]) -> Turn:
    """便捷入口，等价于 :meth:`Turn.from_outcome`。"""
    return Turn.from_outcome(question, outcome)


__all__ = (
    "DEFAULT_MAX_TURNS",
    "MAX_BLOCK_CHARS",
    "REFUSED_PLACEHOLDER",
    "SessionHistory",
    "Turn",
    "turn_from_outcome",
)
