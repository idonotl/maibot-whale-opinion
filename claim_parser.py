"""主张解析：提取对象、观点、评价范围与标准。

规则负责常见句式，模型只负责把自然语言补成结构化字段；模型输出必须可校验，
解析失败时退回规则结果，绝不编造。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SCOPE_HINTS = {
    "整场": "整场比赛", "全场": "整场比赛", "这场": "整场比赛", "这场比赛": "整场比赛",
    "上半场": "上半场", "下半场": "下半场", "第一局": "第一局", "第二局": "第二局",
    "关键团": "关键团战", "前期": "前期", "后期": "后期",
}
CRITERIA_HINTS = {
    "稳定性": ("稳", "稳定", "失误", "送", "波动"),
    "关键决策": ("决策", "指挥", "开团", "抓机会", "关键"),
    "团队贡献": ("团队", "配合", "贡献", "辅助", "带节奏"),
    "精彩操作": ("操作", "手法", "秀", "对线", "单杀", "反杀", "亮眼"),
    "数据表现": ("伤害", "经济", "kda", "数据", "补刀"),
}
ENTITY_PATTERN = re.compile(r"([A-Za-z]{1,6}|[\u4e00-\u9fa5]{1,6})\s*(?:打得|表现|发挥|是|拿了|最|更|不|比)")
EVENT_PATTERN = re.compile(r"(?:今天|昨天|昨晚|刚才|这场|这局|第[一二三四五六七八九十]+[场局]|[\d]{1,2}月[\d]{1,2}日|[A-Z]{2,6}\s*(?:vs|VS|打)\s*[A-Z]{2,6})")


@dataclass
class Claim:
    """一条用户主张的结构化结果。"""

    topic_key: str = ""
    event_ref: str = ""
    scope: str = ""
    criteria: list[str] = field(default_factory=list)
    user_position: str = ""
    subject: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "topic_key": self.topic_key, "event_ref": self.event_ref, "scope": self.scope,
            "criteria": list(self.criteria), "user_position": self.user_position,
            "subject": self.subject, "reason": self.reason,
        }


def detect_criteria(text: str) -> list[str]:
    found = []
    low = (text or "").lower()
    for name, keywords in CRITERIA_HINTS.items():
        if any(k in low for k in keywords):
            found.append(name)
    return found


def detect_scope(text: str) -> str:
    raw = text or ""
    for hint, scope in SCOPE_HINTS.items():
        if hint in raw:
            return scope
    return ""


def detect_event(text: str) -> str:
    match = EVENT_PATTERN.search(text or "")
    return match.group(0) if match else ""


SUBJECT_STOPWORDS = {"反正", "其实", "我就", "我就说", "我觉得", "感觉", "这场", "今天", "那个", "这个"}


def detect_subject(text: str) -> str:
    match = ENTITY_PATTERN.search(text or "")
    if not match:
        return ""
    name = match.group(1).strip().strip("反正其实我就说感觉")
    if not name or name in SUBJECT_STOPWORDS or len(name) > 6:
        return ""
    return name


def build_topic_key(claim: Claim) -> str:
    parts = [p for p in (claim.event_ref, claim.scope, claim.subject) if p]
    if parts:
        return ":".join(parts)[:80]
    return (claim.user_position or "话题")[:40]


def parse_rules(text: str, criteria_default: list[str] | None = None) -> Claim:
    """只用规则解析，永远返回可用的 Claim。"""
    raw = (text or "").strip()
    claim = Claim()
    claim.event_ref = detect_event(raw)
    claim.scope = detect_scope(raw)
    claim.criteria = detect_criteria(raw) or list(criteria_default or [])
    claim.subject = detect_subject(raw)
    claim.user_position = raw[:120]
    claim.reason = next((raw.split(m, 1)[1].strip()[:80] for m in ("因为", "毕竟", "主要") if m in raw), "")
    claim.topic_key = build_topic_key(claim)
    return claim


ANALYSIS_SYSTEM = (
    "你是讨论分析器。只输出一个 JSON 对象，不要解释、不要 Markdown 代码块。"
    "字段固定为：kind(事实/评价/喜好/情绪/混合/其他)、event_ref(事件标识，如“10月29日 IG vs HLE”)、"
    "scope(评价范围)、criteria(数组，最多4项)、user_position(一句话概括用户结论)、"
    "subject(被评价对象)、reason(用户给的理由，没写就空字符串)。"
    "无法判断的字段填空字符串或空数组，不要猜测具体比分、数据或细节。"
)


def parse_llm_result(text: str, fallback: Claim) -> tuple[Claim, bool]:
    """校验模型输出；任何异常都退回规则结果并标记未采用。"""
    raw = (text or "").strip()
    if not raw:
        return fallback, False
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return fallback, False
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return fallback, False
    if not isinstance(data, dict):
        return fallback, False
    claim = Claim(
        event_ref=str(data.get("event_ref") or fallback.event_ref)[:60],
        scope=str(data.get("scope") or fallback.scope)[:40],
        criteria=[str(c)[:12] for c in (data.get("criteria") or []) if str(c).strip()][:4] or fallback.criteria,
        user_position=str(data.get("user_position") or fallback.user_position)[:120],
        subject=str(data.get("subject") or fallback.subject)[:40],
        reason=str(data.get("reason") or fallback.reason)[:100],
    )
    claim.topic_key = build_topic_key(claim)
    return claim, True
