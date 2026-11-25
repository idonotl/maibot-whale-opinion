"""话题与意图识别：区分事实、评价、喜好、情绪、混合。

设计约束（方案 §5）：
- 不能因为出现“最好”就启动辩论；
- 个人喜好与情绪倾诉默认不进入讨论；
- 规则优先，只有在规则判不清时才交给模型。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FACT_MARKERS = ("官方", "mvp", "冠军", "亚军", "公布", "宣布", "记录", "比分", "数据", "排名", "奖项", "拿了", "得了")
EVAL_MARKERS = ("最好", "最强", "最稳", "最厉害", "更厉害", "更强", "更稳", "不如", "吊打", "稳赢",
                "拉胯", "发挥", "表现", "应该", "配得上", "值得", "配不上",
                "重要", "关键作用", "立功", "背锅", "大腿")
PREFERENCE_MARKERS = ("我最喜欢", "我就喜欢", "我更喜欢", "我偏爱", "我喜欢", "我爱看", "我讨厌", "我不喜欢", "我的菜")
EMOTION_MARKERS = ("气死", "难受", "好烦", "烦死", "委屈", "崩溃", "绷不住", "emo", "心累", "累了", "想哭", "啊啊啊")
REASON_MARKERS = ("因为", "所以", "说明", "毕竟", "主要", "关键是", "原因")
COMPARE_PATTERN = re.compile(r"[ABCDabcd甲乙丙丁]\s*(?:比|不如|胜过|强过|好过)")
OBJECT_PATTERN = re.compile(r"([A-Za-z0-9\u4e00-\u9fa5]{1,12}?)\s*(?:是|拿了|得了|打得|表现|发挥|配得上|不如|比)")

QUESTION_MARKERS = (
    "吗", "呢", "?", "？", "你觉得", "你看", "你说", "怎么看", "认为",
    "谁表现", "谁最", "谁更强", "谁打得", "你的看法", "你怎么想", "评价一下",
    "说说你的", "分析一下", "给我你的看法", "你更看好",
)
OPINION_ASK_PATTERN = re.compile(r"谁(?:打|表现|发挥|最|更)|(?:你的|说说你的|给我你的)(?:看法|意见|判断)")

_TASTE = re.compile(r'好不好吃|好吃|难吃|好不好喝|好喝|难喝|香不香|甜不甜|咸不咸|口味|好不好看|好看|难看|可不可爱|可爱|配色|穿搭|发型|(?:取|起)(?:个)?名字|(?:选|喜欢).{0,5}颜色|(?:蓝色|白色|红色|黑色).{0,3}还是')
_OBJECTIVE = re.compile(r'热量|卡路里|过敏|食品安全|成分|营养|多少钱|价格|保质期|中毒|致癌|蛋白质|糖分|统计|实验|数据|证据|官方')
_FORMAL = re.compile(r'证据|数据|客观|核实|核对|认真分析|严谨|事实依据')
_PLAYFUL = re.compile(r'接梗|玩梗|按梗|凭感觉|随便押|盲猜|押一个')


def continues_discussion(text: str, prior: dict) -> bool:
    """A previous topic alone cannot make ordinary conversation part of a debate."""
    if not prior.get('active'):
        return False
    return bool(re.search(r'你漏了|补充.{0,8}证据|这(?:场|局)|那(?:场|局|波)|关键团|回放|前期|后期|对线|单杀|反杀|这个证据|这份数据|刚才.{0,8}(?:观点|结论|判断)|我(?:仍然|还是)觉得.{0,8}(?:最强|最好|更强)', text, re.I))


@dataclass
class RouteResult:
    """一次入站消息的路由判断。"""

    kind: str = "none"          # fact / evaluation / preference / emotion / mixed / none
    needs_discussion: bool = False
    requires_opinion: bool = False
    asks_question: bool = False
    reason: str = ""
    signals: list[str] = field(default_factory=list)

    @property
    def may_debate(self) -> bool:
        """只有评价类、混合类才可能需要表达不同看法。"""
        return self.needs_discussion


def _hits(text: str, markers) -> list[str]:
    low = text.lower()
    return [m for m in markers if m.lower() in low]


def route(text: str, *, addressed: bool = True, recent_discussion: bool = False) -> RouteResult:
    """规则路由；纯函数，便于离线测试。"""
    raw = (text or "").strip()
    if len(raw) < 2:
        return RouteResult(kind="none", reason="空消息")

    if _PLAYFUL.search(raw) and not _FORMAL.search(raw):
        return RouteResult(kind='casual', asks_question=True, reason='玩笑选边由角色自然回应，不强制证据立场')

    if _TASTE.search(raw) and not _OBJECTIVE.search(raw):
        return RouteResult(kind='preference', asks_question=bool(re.search(r'吗|呢|？|\?|你觉得|你选|还是', raw)),
                           reason='口味、审美或取名的个人取向，不进入证据辩论')

    if _FORMAL.search(raw) and any(m in raw for m in ('你觉得','你怎么看','分析','核实','核对')):
        return RouteResult(kind='evaluation', needs_discussion=addressed, requires_opinion=True,
                           asks_question=True, reason='明确要求基于事实依据判断')

    low = raw.lower()
    fact = _hits(low, FACT_MARKERS)
    evaluation = _hits(low, EVAL_MARKERS)
    preference = _hits(low, PREFERENCE_MARKERS)
    emotion = _hits(low, EMOTION_MARKERS)
    reason = any(m in raw for m in REASON_MARKERS)
    asks = (any(m in raw for m in QUESTION_MARKERS) or raw.endswith(("吗", "呢"))
            or bool(OPINION_ASK_PATTERN.search(raw)))

    # 1) 纯情绪倾诉：先共情，不辩论
    if emotion and not evaluation:
        return RouteResult(kind="emotion", asks_question=asks, reason="情绪表达优先共情", signals=emotion)

    # 2) 个人喜好：尊重，可分享偏好，不纠正
    if preference and not (evaluation and reason):
        opinion_asked = any(m in raw for m in ("你觉得", "你看呢", "你怎么看"))
        return RouteResult(kind="preference", asks_question=asks or opinion_asked,
                           reason="个人偏好不作为错误纠正", signals=preference)

    # 3) 事实陈述：必要时核实，但默认不辩论
    if fact and not evaluation:
        return RouteResult(kind="fact", asks_question=asks, reason="事实陈述，需要时核实", signals=fact)

    # 4) 混合：事实/奖项 + 评价推导
    if fact and evaluation and reason:
        return RouteResult(kind="mixed", needs_discussion=addressed or recent_discussion,
                           requires_opinion=True, asks_question=asks,
                           reason="事实与评价推导混合，需分开处理", signals=fact + evaluation)

    # 5) 评价
    if evaluation:
        strong = any(m in raw for m in ("最好", "最强", "最稳", "最厉害", "更厉害", "更强", "不如", "配得上", "拉胯"))
        provoke = any(m in raw for m in ("不服", "凭什么", "你敢", "反驳", "争一下", "谁更强"))
        # 只有“最好”这类词不够：需要理由、比较、明确提问，或本来就在讨论中
        enough_signal = bool(reason or compare_explicit(raw) or asks or provoke or recent_discussion)
        needs = bool(addressed and strong and enough_signal) or bool(addressed and asks and strong)
        return RouteResult(kind="evaluation", needs_discussion=needs, requires_opinion=needs,
                           asks_question=asks, reason="评价类，允许不同结论" if needs else "评价类但无需表态",
                           signals=evaluation)

    # A request for an opinion alone is not a formal evidence dispute.
    if any(m in raw for m in ("你觉得", "你怎么看", "说说你的看法", "你的意见")):
        followup = recent_discussion and bool(re.fullmatch(r'(?:那|所以)?你(?:觉得|怎么看)(?:呢)?[？?。！!]*',raw))
        formal = bool(_FORMAL.search(raw) or followup)
        return RouteResult(kind='evaluation' if formal else 'casual',
                           needs_discussion=addressed and formal, requires_opinion=formal,
                           asks_question=True, reason='延续明确讨论或要求事实依据' if formal else '普通征求看法，不注入证据辩论')

    return RouteResult(kind="none", asks_question=asks, reason="未识别为需要判断的话题")


def compare_explicit(text: str) -> bool:
    """是否出现明确的 A 比 B 之类的比较结构。"""
    return bool(COMPARE_PATTERN.search(text)) or "还是" in text and "更" in text


def guess_object(text: str) -> str:
    """从评价句里粗提被评价的对象（用于需要澄清时提问）。"""
    match = OBJECT_PATTERN.search(text or "")
    if match:
        return match.group(1).strip()
    return ""


def looks_like_stop(text: str) -> bool:
    """用户要求停止或换话题。"""
    raw = (text or "").strip()
    return any(m in raw for m in ("别说了", "不聊这个", "换个话题", "算了不讨论", "不想说这个", "跳过"))


def looks_like_pressure(text: str) -> bool:
    """用户在施压而不提供新信息（方案 §10）。"""
    raw = (text or "").strip()
    return any(m in raw for m in ("你必须", "你就得承认", "认输", "服不服", "别嘴硬", "反正我说得对", "你要听我的"))
