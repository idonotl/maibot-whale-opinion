"""提示块构建：给 Planner 判断用、给 Replyer 表达用（方案 §13.3）。

要求：
- 用独立标记包裹，重试时替换旧块而不是不断累加；
- 只写本轮结论、依据、未知项和边界，不写模型长推理；
- 明确“情绪影响表达，证据约束判断”。
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

PLANNER_OPEN = "[[OPINION-PLAN]]"
PLANNER_CLOSE = "[[/OPINION-PLAN]]"
REPLYER_OPEN = "[[OPINION-VOICE]]"
REPLYER_CLOSE = "[[/OPINION-VOICE]]"

BOUNDARIES = (
    "不虚构没读过的原文或没看过的画面；不把用户施压、夸奖、撒娇当作证据；"
    "不重复已经说过的理由；意见跟着依据变化，表达跟着关系变化。"
)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _bounded_block(lines: list[str], max_chars: int) -> str:
    """Keep the closing delimiter when optional discussion data fills the budget."""
    block = "\n".join(lines)
    if len(block) <= max_chars:
        return block
    suffix = "…\n" + lines[-1]
    return "\n".join(lines[:-1])[:max(0, max_chars - len(suffix))].rstrip() + suffix


def build_planner_block(state: dict, max_chars: int = 900) -> str:
    """Planner 侧：告知当前讨论状态，供其决定是否检索、澄清或收尾。"""
    if not state.get("active"):
        return ""
    lines = [PLANNER_OPEN]
    lines.append("讨论状态：%s｜话题：%s" % (state.get("stance_label", "未定"), state.get("topic_key", "")[:40]))
    if state.get("scope"):
        lines.append("评价范围：%s" % state["scope"])
    if state.get("criteria"):
        lines.append("评价标准：%s" % "、".join(state["criteria"][:4]))
    if state.get("position"):
        lines.append("我方结论：%s" % _clip(state["position"], 80))
    if state.get("main_reason"):
        lines.append("主要依据：%s" % _clip(state["main_reason"], 100))
    if state.get("user_position"):
        lines.append("对方结论：%s" % _clip(state["user_position"], 60))
    evidence = state.get("evidence") or []
    if evidence:
        lines.append("已有资料：%s" % "；".join(e[:60] for e in evidence[:3]))
    if state.get("uncertainties"):
        lines.append("资料缺口：%s" % "；".join(state["uncertainties"][:3]))
    lines.append("建议动作：%s" % state.get("action_reason", "按判断回答"))
    if state.get("needs_search"):
        lines.append("需要检索：先查事件、范围与关键回合，用中性检索词，不要把结论写进查询。")
    if state.get("evidence_status") == "partial":
        lines.append("现有资料只有搜索摘要：判断前先 read_webpage 读至少一条正文，别再凭标题下结论。")
    if state.get("need_other_source"):
        lines.append("有链接读不到正文（脚本渲染或验证页），换搜索结果里的另一条链接再试，换 2 条仍失败就保留意见。")
    lines.append("边界：" + BOUNDARIES)
    lines.append(PLANNER_CLOSE)
    return _bounded_block(lines, max_chars)


def build_replyer_block(state: dict, max_chars: int = 900) -> str:
    """Replyer 侧：本轮必须表达的立场与语气边界。"""
    if not state.get("active"):
        return ""
    if state.get("repeat_suppressed"):
        return ""

    lines = [REPLYER_OPEN]
    lines.append("直接表达本轮判断，分歧或追问才补必要依据，不固定先复述再评价；假设与试演不写成现实事件。")
    lines.append("本轮立场：%s" % state.get("stance_label", ""))
    if state.get("position"):
        lines.append("待核对的判断（不是回复台词）：%s" % _clip(state["position"], 80))
    if state.get("main_reason"):
        lines.append("依据：%s" % _clip(state["main_reason"], 100))
    if state.get("agreement_points"):
        lines.append("可用共识（无需重复对方已知内容）：%s" % _clip(state["agreement_points"][0], 60))
    if state.get("uncertainties"):
        lines.append("不确定项（只自己留着，不要说成免责声明）：%s"
                     % "；".join(state["uncertainties"][:2]))
    if state.get("forced_choice") and state.get('stance_state') != 'uncertain':
        lines.append("对方在要一个明确答案：直接选一个并给一条理由，不要用"
                     "“我勉强敢说”“有点瞎猜”“资料不足”这类前缀削弱自己；"
                     "也别编具体数据，用你确实知道的一两点做依据。")
    elif state.get('forced_choice'):
        lines.append('缺少依据时不因被催就编定论；可以区分玩笑押注与事实判断，不能同时说无法判断又给确定结论。')
    lines.append('承接本话题已发出的选择。改变判断时简短说明新增依据或承认接岔，不重讲整段经过，不编造后来才知道的过程。不为保持一致而坚持错误。')
    lines.append("语气：%s" % state.get("tone_hint", "温软、简短，一两句为主"))
    if state.get("should_close"):
        lines.append("收尾：可以说各自看重的点不同，保留不同意见，不要求对方认输。")
    if state.get("ask_question"):
        lines.append("最多问一个具体问题，不连续反问。")
    lines.append("禁止：不要复述以上要点当清单；没有实际画面依据时，不要说“我看到/回放里”这类画面细节；"
                 "索要来源时给真实链接，其余省去查证过程；不要加“仅供参考/不算权威/没细看”这类防御尾巴；"
                 "不要因为关系好就改口。")
    lines.append(REPLYER_CLOSE)
    return _bounded_block(lines, max_chars)


def strip_block(text: str, open_marker: str, close_marker: str) -> str:
    """移除已有的块（重试时不叠加）。"""
    if not text:
        return ""
    while True:
        start = text.find(open_marker)
        if start < 0:
            break
        end = text.find(close_marker, start)
        if end < 0:
            text = text[:start]
            break
        text = text[:start] + text[end + len(close_marker):]
    return text.strip()


def merge_block(existing: str, block: str, open_marker: str, close_marker: str) -> str:
    """替换式合并：先删旧块，再追加新块。"""
    base = strip_block(existing or "", open_marker, close_marker)
    if not block:
        return base
    return (base + "\n\n" + block).strip() if base else block


def build_planner_item(state: dict, max_chars: int = 900) -> dict | None:
    """构造注入到 Planner items 的上下文项；没有讨论时返回 None。"""
    block = build_planner_block(state, max_chars)
    if not block:
        return None
    return {
        "item_type": "UserMessageItem",
        "meta": {"item_id": "whale-opinion-" + uuid4().hex, "logical_turn_id": None,
                 "timestamp": datetime.now(timezone.utc).isoformat()},
        "parts": [{"type": "text", "text": block}],
        "source": "local.whale-opinion",
    }
