"""讨论节奏：动作选择、复读抑制、收尾判断、批次与过期处理（方案 §11、§13.4）。"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field

ACTION_ANSWER = "answer"
ACTION_CLARIFY = "clarify"
ACTION_SEARCH = "search"
ACTION_CLOSE = "close"
ACTION_NONE = "none"


@dataclass
class ActionDecision:
    action: str = ACTION_NONE
    reason: str = ""
    repeat_suppressed: bool = False
    should_close: bool = False
    round_no: int = 0


@dataclass
class TurnContext:
    """一轮讨论的输入摘要。"""

    route_kind: str = ""
    needs_discussion: bool = False
    new_information: bool = False
    prior_state: str = ""
    prior_reasons: list = field(default_factory=list)
    new_reason: str = ""
    rounds_without_info: int = 0
    review_after: int = 3
    no_info_limit: int = 2
    stop_requested: bool = False
    pressure: bool = False
    evidence_status: str = "none"
    evidence_supports_user: bool = False
    asks_question: bool = False
    shadow: bool = False


_PRONOUNS = "他她它我你您"


def _bigrams(text: str) -> set:
    """用字符二元组做相似度，避免分词粒度不同导致“换说法”漏判。"""
    cleaned = re.sub(r"[\s\W_]+", "", str(text or ""))
    for ch in _PRONOUNS:
        cleaned = cleaned.replace(ch, "")
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def reason_repeated(new_reason: str, prior_reasons: list, threshold: float = 0.6) -> bool:
    """新理由是否只是换个说法的旧理由。"""
    new_tokens = _bigrams(new_reason)
    if not new_tokens:
        return False
    for old in prior_reasons or []:
        old_tokens = _bigrams(old)
        if not old_tokens:
            continue
        overlap = len(new_tokens & old_tokens) / max(1, len(new_tokens | old_tokens))
        if overlap >= threshold:
            return True
    return False


def decide_action(ctx: TurnContext) -> ActionDecision:
    """根据路由、证据、轮次挑一个动作；纯函数。"""
    decision = ActionDecision()

    if ctx.stop_requested:
        decision.action = ACTION_CLOSE
        decision.should_close = True
        decision.reason = "用户要求停止或换话题"
        return decision

    if not ctx.needs_discussion:
        decision.action = ACTION_NONE
        decision.reason = "本轮不需要讨论（喜好/情绪/普通聊天）"
        return decision

    if ctx.evidence_status == "none" and ctx.asks_question:
        decision.action = ACTION_CLARIFY
        decision.reason = "缺少资料，先问一个具体问题"
        return decision

    if ctx.evidence_status == "none" and not ctx.prior_state:
        decision.action = ACTION_SEARCH
        decision.reason = "需要判断但没有可追溯资料"
        return decision

    if ctx.pressure and not ctx.new_information:
        decision.repeat_suppressed = True
        decision.action = ACTION_ANSWER
        decision.reason = "用户在施压但没有新依据，不当作证据"
        return decision

    repeated = reason_repeated(ctx.new_reason, ctx.prior_reasons)
    if repeated and not ctx.new_information:
        decision.repeat_suppressed = True
        if ctx.rounds_without_info + 1 >= ctx.no_info_limit:
            decision.action = ACTION_CLOSE
            decision.should_close = True
            decision.reason = "连续没有新信息，允许保留分歧并收尾"
        else:
            decision.action = ACTION_ANSWER
            decision.reason = "理由重复，换角度简短回应而不复读"
        return decision

    if ctx.new_information:
        decision.action = ACTION_ANSWER
        decision.reason = "用户提供了新信息，需要重新评估"
        return decision

    if ctx.rounds_without_info + 1 >= ctx.no_info_limit:
        decision.action = ACTION_CLOSE
        decision.should_close = True
        decision.reason = "检查点：本轮没有信息增量"
        return decision

    decision.action = ACTION_ANSWER
    decision.reason = "继续当前判断"
    return decision


def next_round_count(rounds_without_info: int, new_information: bool) -> int:
    return 0 if new_information else rounds_without_info + 1


def batch_id(session_id: str, message_ids: list) -> str:
    """批次编号：同一批消息重复触发时得到同一个 id（幂等，T20）。"""
    joined = "|".join(sorted(str(m) for m in (message_ids or []) if m))
    digest = hashlib.sha1((session_id + "::" + joined).encode("utf-8")).hexdigest()[:16]
    return "b" + digest


def is_stale(analysis_revision: int, current_revision: int) -> bool:
    """分析期间话题被更新过，则结果作废（T17）。"""
    return current_revision > analysis_revision


@dataclass
class PendingAnalysis:
    """异步分析任务的最小状态，用于超时与过期控制。"""

    session_id: str
    batch_id: str
    revision: int
    started_utc: float
    timeout_seconds: float = 45.0

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.started_utc > self.timeout_seconds
