"""证据适配层：把真实工具结果与用户补充整理成可追溯的证据条目。

方案 §3 明确提醒：Planner 调了工具不代表插件能读到结果。本模块只接受
宿主上下文中真实出现的 tool_result 结构，读不到就返回空，绝不虚构。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

URL_PATTERN = re.compile(r"https?://[^\s\"'<>）)】]+")
KIND_VERIFIED = "verified_fact"
KIND_REPORTED = "reported_opinion"
KIND_USER = "user_report"
KIND_INFERENCE = "inference"
KIND_UNKNOWN = "unknown"

_SUMMARY_HINTS = ("搜索摘要", "摘要：", "以下为摘要", "summary")
_BLOCKED_HINTS = ("环境异常", "完成验证", "安全验证", "访问验证", "captcha", "just a moment", "access denied")
CONTENT_TOOLS = ("internet_search", "read_webpage", "web_search", "web_fetch", "bili_search", "bili_video_probe", "bili_trending")
_FULL_HINTS = ("正文", "全文", "content")


@dataclass
class EvidenceRecord:
    kind: str = KIND_UNKNOWN
    source_type: str = "tool"
    tool_name: str = ""
    query: str = ""
    url: str = ""
    host: str = ""
    title: str = ""
    snippet: str = ""
    read_level: str = "none"      # full / summary / none
    event_time: str = ""
    source_group: str = ""
    success: bool = True
    limitation: str = ""
    fetched_utc: float = 0.0

    def to_repo(self) -> dict:
        return {
            "kind": self.kind, "source_type": self.source_type, "tool_name": self.tool_name,
            "query": self.query, "url": self.url, "host": self.host, "title": self.title,
            "snippet": self.snippet[:600], "read_level": self.read_level,
            "event_time": self.event_time, "source_group": self.source_group,
            "success": self.success, "limitation": self.limitation,
            "fetched_utc": self.fetched_utc,
        }


def _as_dict(item) -> dict:
    if isinstance(item, dict):
        return item
    text = str(item or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


# 本仓库实际使用的上下文项类型（2026-10-29 用 planner 预览核对）
_OUTPUT_ITEM_TYPES = {
    "functioncalloutputitem", "functioncalloutput", "functionresultitem",
    "toolresultitem", "toolresult", "tool_result", "function_result",
}


def extract_tool_results(items) -> list[dict]:
    """从 Planner 的 Context Items 中提取工具输出。

    已核对本仓库的真实结构：
        {"item_type": "FunctionCallOutputItem", "tool_name": "read_webpage",
         "output": "...", "success": true, "call_id": "..."}
    同时兼容 kind/type、content/text/payload 等旧式写法；无法识别就跳过。
    """
    results = []
    for raw in (items or []):
        item = _as_dict(raw)
        if not item:
            continue
        kind = str(item.get("item_type") or item.get("kind") or item.get("type") or "").lower()
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if kind not in _OUTPUT_ITEM_TYPES and not item.get("tool_name") and not payload.get("tool"):
            continue
        tool = str(item.get("tool_name") or item.get("tool") or payload.get("tool") or item.get("name") or "")
        success = item.get("success", payload.get("success", True))
        content = item.get("output", item.get("content", item.get("text", payload.get("content", ""))))
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False)
        results.append({
            "tool": tool,
            "success": bool(success),
            "content": str(content or ""),
            "call_id": str(item.get("call_id") or item.get("tool_call_id") or ""),
        })
    return results


def _parse_payload(content: str) -> dict:
    """把工具输出里的 JSON 拆成 title/url/snippet；解析不了就返回空。"""
    text = (content or "").strip()
    if "{" not in text and "[" not in text:
        return {}
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0] or [-1])
    end = max(text.rfind("}"), text.rfind("]"))
    if start < 0 or end <= start:
        return {}
    snippet = text[start:end + 1]
    try:
        data = json.loads(snippet)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    title = str(data.get("title") or "")
    url = str(data.get("url") or data.get("link") or "")
    body = data.get("content") or data.get("text") or data.get("snippet") or ""
    results = data.get("results") or data.get("items") or []
    if isinstance(results, list) and results:
        first = results[0] if isinstance(results[0], dict) else {}
        title = title or str(first.get("title") or "")
        url = url or str(first.get("url") or first.get("link") or "")
        joined = []
        for row in results[:3]:
            if isinstance(row, dict):
                joined.append("%s｜%s" % (str(row.get("title") or "")[:40], str(row.get("snippet") or "")[:60]))
        body = body or "；".join(joined)
    if data.get("error") or data.get("error_code"):
        return {"error": str(data.get("error") or data.get("error_detail") or "工具失败")[:120]}
    if not title and not body and not url:
        return {}
    return {"title": title, "url": url, "body": str(body), "is_search": bool(results)}


def classify(tool: str, content: str, success: bool, query: str = "", event_time: str = "") -> EvidenceRecord:
    """把一次工具结果转换成证据条目；纯函数，便于离线测试。"""
    text = (content or "").strip()
    urls = URL_PATTERN.findall(text)
    url = urls[0] if urls else ""
    host = urlparse(url).netloc if url else ""
    record = EvidenceRecord(
        tool_name=tool[:40], query=query[:80], url=url[:200], host=host[:60],
        event_time=event_time[:40], success=success)

    if not success:
        record.kind = KIND_UNKNOWN
        record.read_level = "none"
        record.limitation = "工具调用失败，未获得资料"
        return record
    if not text:
        record.kind = KIND_UNKNOWN
        record.limitation = "工具没有返回内容"
        return record
    if any(hint in text.lower() for hint in _BLOCKED_HINTS):
        record.kind = KIND_UNKNOWN
        record.read_level = "none"
        record.limitation = "页面是验证/反爬页，没有正文"
        return record
    if tool not in CONTENT_TOOLS:
        record.kind = KIND_UNKNOWN
        record.limitation = "非资料类工具，不计为证据"
        return record

    payload = _parse_payload(text)
    if payload.get("error"):
        record.kind = KIND_UNKNOWN
        record.read_level = "none"
        record.limitation = "工具返回错误：" + payload["error"]
        return record
    if payload:
        record.title = payload["title"][:100] or "搜索结果"
        record.url = record.url or payload["url"][:200]
        record.host = record.host or (urlparse(record.url).netloc if record.url else "")
        body = payload["body"]
        if payload["is_search"]:
            record.kind = KIND_REPORTED
            record.read_level = "summary"
            record.snippet = _condense(body)
            record.limitation = "搜索摘要，未读正文"
            record.source_group = "%s|%s" % (record.host or tool, record.title[:18])
            return record
        # 网页正文：如果是"标题即正文"（正文没抽出来），不能算读过原文
        title_text = " ".join((payload["title"] or "").split())
        body_text = " ".join(body.split())
        body_is_title = bool(title_text) and (body_text == title_text or body_text in title_text
                                              or len(body_text) <= max(40, len(title_text) // 2))
        if tool in {"read_webpage", "web_fetch"}:
            if body_is_title:
                record.read_level = "summary"
                record.kind = KIND_REPORTED
                record.limitation = "网页正文未抽取到，只拿到标题"
            else:
                record.read_level = "full"
                record.kind = KIND_VERIFIED
        else:
            record.read_level = "full" if len(body_text) > 600 else "summary"
            record.kind = KIND_VERIFIED if record.read_level == "full" else KIND_REPORTED
        record.snippet = _condense(body)
        record.title = record.title or _first_line(body)[:100]
        record.source_group = "%s|%s" % (record.host or tool, (record.title or record.url)[:18])
        return record

    lowered = text.lower()
    looks_full = len(text) > 600 and not any(h in lowered for h in _SUMMARY_HINTS)
    record.read_level = "full" if looks_full else "summary"
    record.kind = KIND_VERIFIED if looks_full else KIND_REPORTED
    record.title = _first_line(text)[:100]
    record.snippet = _condense(text)
    if not urls:
        record.limitation = "结果未带可追溯链接"
    if record.read_level == "summary":
        record.limitation = (record.limitation + "；仅有摘要，未读正文").lstrip("；")
    # 同源转载归并键：域名 + 标题前 18 字
    record.source_group = "%s|%s" % (host or tool, record.title[:18])
    return record


def user_report(text: str) -> EvidenceRecord:
    """用户补充的事实，标记为未核实。"""
    record = EvidenceRecord(
        kind=KIND_USER, source_type="user", read_level="none",
        title="用户补充", snippet=_condense(text or ""),
        limitation="用户陈述，未独立核实", source_group="user|" + _condense(text or "")[:18])
    return record


@dataclass
class EvidenceSummary:
    status: str = "none"          # none / partial / full / conflict
    verified: list = field(default_factory=list)
    reported: list = field(default_factory=list)
    user_reports: list = field(default_factory=list)
    uncertainties: list = field(default_factory=list)
    independent_sources: int = 0
    has_full_text: bool = False

    def to_dict(self) -> dict:
        return {
            "status": self.status, "independent_sources": self.independent_sources,
            "has_full_text": self.has_full_text, "uncertainties": list(self.uncertainties),
        }


def summarize(records: list) -> EvidenceSummary:
    """汇总证据状态、独立来源数与缺口；纯函数。"""
    summary = EvidenceSummary()
    groups = {}
    for record in records or []:
        if isinstance(record, dict):
            record = EvidenceRecord(**{k: v for k, v in record.items()
                                       if k in EvidenceRecord.__dataclass_fields__})
        if not record.success:
            summary.uncertainties.append("有工具调用失败")
            continue
        if record.kind == KIND_USER:
            summary.user_reports.append(record)
            continue
        if record.kind == KIND_VERIFIED:
            summary.verified.append(record)
        elif record.kind == KIND_REPORTED:
            summary.reported.append(record)
        key = record.source_group or record.url or record.title
        groups[key] = groups.get(key, 0) + 1

    summary.independent_sources = len(groups)
    summary.has_full_text = bool(summary.verified)
    if not summary.verified and not summary.reported:
        summary.status = "none"
        summary.uncertainties.append("缺少可追溯的公开资料")
    elif summary.verified and summary.independent_sources >= 2:
        summary.status = "full"
    else:
        summary.status = "partial"
        if not summary.has_full_text:
            summary.uncertainties.append("只看到摘要，未读正文")
        if summary.independent_sources <= 1:
            summary.uncertainties.append("只有单一来源")
    if len(groups) < len(summary.verified) + len(summary.reported):
        summary.uncertainties.append("部分来源为同源转载")
    if summary.user_reports:
        summary.uncertainties.append("含用户补充，未独立核实")
    return summary


def freshness_note(event_time: str, hours: float = 6.0) -> str:
    """时效提示（直播中/已结束）。"""
    if not event_time:
        return ""
    return "资料时间：%s；超过 %d 小时需重新核实" % (event_time[:40], int(hours))


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        cleaned = " ".join(line.split())
        if cleaned:
            return cleaned
    return ""


def _condense(text: str, limit: int = 400) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:limit]
