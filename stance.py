"""立场状态与形成规则（方案 §9、§10）。

核心约束：
- 判断只跟证据走，不跟用户立场、好感、心情走；
- 证据不足时 `uncertain` 就是正确结果；
- 用户提供新证据可以修正（revised），但不能预设一定改口或一定坚持。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

AGREE = "agree"
AGREE_ADD = "agree_with_addition"
DIFF_CRITERIA = "different_criteria"
DISAGREE = "disagree"
UNCERTAIN = "uncertain"
REVISED = "revised"

STANCE_LABELS = {
    AGREE: "基本认同",
    AGREE_ADD: "认同并补充",
    DIFF_CRITERIA: "标准不同",
    DISAGREE: "有依据的不同看法",
    UNCERTAIN: "资料不足，暂不站边",
    REVISED: "根据新信息修正",
}


@dataclass
class Stance:
    state: str = UNCERTAIN
    position: str = ""
    main_reason: str = ""
    agreement_points: list = field(default_factory=list)
    uncertainties: list = field(default_factory=list)
    changed: bool = False
    reason_code: str = ""

    def to_dict(self) -> dict:
        return {
            "state": self.state, "label": STANCE_LABELS.get(self.state, self.state),
            "position": self.position, "main_reason": self.main_reason,
            "agreement_points": list(self.agreement_points),
            "uncertainties": list(self.uncertainties),
            "changed": self.changed, "reason_code": self.reason_code,
        }


def _tokens(text: str) -> set:
    return set(re.findall(r"[A-Za-z]{1,6}|[\u4e00-\u9fa5]{2,4}", text or ""))


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta | tb))


def criteria_differ(user_criteria: list, default_criteria: list) -> bool:
    """用户明确给出的标准与默认关注点不同，且不属于包含关系。"""
    user = {str(c) for c in (user_criteria or []) if str(c).strip()}
    default = {str(c) for c in (default_criteria or [])}
    if not user or not default:
        return False
    return not (user & default)


def evidence_verdict(evidence_summary) -> str:
    """把证据状态压成一个判断口径：support / counter / mixed / none。"""
    status = getattr(evidence_summary, "status", "none")
    if status == "none":
        return "none"
    if status == "conflict":
        return "mixed"
    if not getattr(evidence_summary, "has_full_text", False):
        return "partial"
    return "support"


def decide(
    *,
    user_position: str,
    user_subject: str,
    evidence_summary,
    user_criteria: list,
    default_criteria: list,
    prior_state: str = "",
    prior_position: str = "",
    new_information: bool = False,
    evidence_objects: list | None = None,
) -> Stance:
    """形成或更新立场；纯函数。

    `evidence_objects` 是证据里提到的候选对象（如 ["B"]），用于判断证据是否支持用户结论；
    没有该信息时只根据资料是否充分给出保守判断。
    """
    summary = evidence_summary
    verdict = evidence_verdict(summary)
    uncertainties = list(getattr(summary, "uncertainties", []) or [])
    stance = Stance(uncertainties=uncertainties)

    if verdict == "none":
        stance.state = UNCERTAIN
        stance.position = "我这边还没有可靠资料，先不跟着下结论"
        stance.main_reason = "不能只凭印象编造比赛细节"
        stance.reason_code = "no_evidence"
        return stance

    if criteria_differ(user_criteria, default_criteria):
        stance.state = DIFF_CRITERIA
        stance.position = "咱俩衡量的点不太一样"
        stance.main_reason = "你更看重%s，我这边会先看%s" % (
            "、".join(user_criteria[:2]) or "你的标准", "、".join(default_criteria[:2]) or "默认标准")
        stance.reason_code = "criteria_diff"
        return stance

    objects = [str(o) for o in (evidence_objects or []) if str(o).strip()]
    supports_user = bool(user_subject and any(user_subject in obj or obj in user_subject for obj in objects))
    contradicts_user = bool(user_subject and objects and not supports_user)

    if verdict == "partial":
        if not supports_user and not contradicts_user:
            stance.state = UNCERTAIN
            stance.position = "资料只到摘要级别，我先保留意见"
            stance.reason_code = "summary_only"
            return stance

    if supports_user:
        if new_information and prior_state in (DISAGREE, DIFF_CRITERIA) and prior_position:
            stance.state = REVISED
            stance.changed = True
            stance.position = "你补充的这段确实改变了我的判断，我改过来"
            stance.reason_code = "revised_to_user"
        else:
            stance.state = AGREE_ADD if objects else AGREE
            stance.position = "这个我同意你的方向"
            stance.reason_code = "evidence_supports_user"
        stance.agreement_points = [user_position[:60]] if user_position else []
        stance.main_reason = _first_verified_reason(summary) or "公开资料与你的结论一致"
        return stance

    if contradicts_user:
        other = next((o for o in objects if not (user_subject and (user_subject in o or o in user_subject))), "")
        if new_information and prior_state in (AGREE, AGREE_ADD) and prior_position:
            stance.state = REVISED
            stance.changed = True
            stance.reason_code = "revised_away_from_user"
        else:
            stance.state = DISAGREE
            stance.reason_code = "evidence_counter"
        stance.position = ("我更偏向%s" % other) if other else "我看到的资料指向另一个结论"
        stance.main_reason = _first_verified_reason(summary) or "和我看到的资料对不上"
        stance.agreement_points = ["你说的那部分里，我也认可的信息我会单独讲清楚"]
        return stance

    stance.state = UNCERTAIN
    stance.position = "资料还不够我站边"
    stance.reason_code = "insufficient"
    return stance


def _first_verified_reason(summary) -> str:
    for record in getattr(summary, "verified", []) or []:
        snippet = getattr(record, "snippet", "") or ""
        if snippet:
            return snippet[:60]
    for record in getattr(summary, "reported", []) or []:
        snippet = getattr(record, "snippet", "") or ""
        if snippet:
            return "公开资料显示：" + snippet[:50]
    return ""


def keeps_verdict_without_new_evidence(new_state: str, prior_state: str) -> bool:
    """没有新证据时是否必须保留原结论（T03 底线）。

    Returns:
        bool: True 表示新结论与已有结论不同、且没有新证据支撑，应当退回原结论。
    """
    if not prior_state or prior_state == UNCERTAIN:
        return False
    return new_state != prior_state
