"""观点与讨论系统：生命周期、钩子编排、异步分析与发送提交。"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from typing import Any, Dict

from maibot_sdk import API, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .claim_parser import ANALYSIS_SYSTEM, parse_llm_result, parse_rules
from .config import OpinionConfig
from .discussion import (
    ACTION_CLARIFY, ACTION_CLOSE, ACTION_SEARCH, ActionDecision, TurnContext,
    batch_id, decide_action, next_round_count,
)
from .evidence import EvidenceSummary, classify, extract_tool_results, summarize, user_report
from . import prompt_builder, stance as stance_mod
from .repository import Repository
from .topic_router import continues_discussion, looks_like_pressure, looks_like_stop, route

STANCE_OPEN = prompt_builder.REPLYER_OPEN
STANCE_CLOSE = prompt_builder.REPLYER_CLOSE


def normalize(raw: dict, session: str) -> dict | None:
    """把宿主消息字典转成插件内部结构（与既有插件一致的字段口径）。"""
    info = raw.get("message_info") or {}
    user = str((info.get("user_info") or {}).get("user_id") or "")
    bot = str(raw.get("account_id") or "")
    text = str(raw.get("processed_plain_text") or "").strip()
    mid = str(raw.get("message_id") or "")
    if not user or not bot or user == bot or not text or not mid:
        return None
    if any(raw.get(k) for k in ("is_notify", "is_command", "is_emoji")):
        return None
    actual = str(raw.get("session_id") or session)
    if actual != session:
        return None
    try:
        stamp = float(raw.get("timestamp") or 0)
    except (TypeError, ValueError):
        return None
    return {
        "user": user, "bot": bot, "text": text[:2000], "mid": mid, "session": session,
        "platform": str(raw.get("platform") or ""), "stamp": stamp,
        "group": str((info.get("group_info") or {}).get("group_id") or ""),
        "namespace": str(raw.get("scope") or ""),
        "at_bot": bool(raw.get("is_at") or raw.get("at_bot") or info.get("is_at")),
        "reply_to_bot": bool(raw.get("reply_to") and str(raw.get("reply_to")) == bot),
    }


class WhaleOpinion(MaiBotPlugin):
    """判断层与表达层分离的讨论插件；默认关闭、试运行。"""

    config_model = OpinionConfig

    def __init__(self):
        super().__init__()
        self.repo: Repository | None = None
        self.started = time.time()
        self._tasks: set = set()
        self._cache: OrderedDict = OrderedDict()
        self._states: Dict[str, dict] = {}
        self._last_analysis: Dict[str, float] = {}
        self._processed: Dict[str, set] = {}
        self._pending_evidence: Dict[str, dict] = {}
        self._tool_buffer: Dict[str, list] = {}
        self._stats = {"batches": 0, "analyses": 0, "blocks": 0, "errors": 0, "sent": 0}

    # ---------- 生命周期 ----------
    async def on_load(self):
        cfg = self.config.plugin
        if not cfg.enabled:
            self._get_logger().info("whale-opinion: 未启用（plugin.enabled=false）")
            return
        self.repo = Repository(self.ctx.paths.data_dir / "opinions.sqlite3")
        await asyncio.to_thread(self.repo.start)
        self._track(asyncio.create_task(self._cleanup_loop()))
        self._get_logger().info(
            "whale-opinion: 已启动（shadow_mode=%s, 私聊=%d, 群=%d）",
            cfg.shadow_mode, len(cfg.private_user_ids), len(cfg.group_ids))

    async def on_unload(self):
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()
        self._states.clear()
        self._cache.clear()
        self.repo = None

    async def on_config_update(self, scope, config_data, version):
        if scope in ("", "self"):
            await self.on_unload()
            await self.on_load()

    def _track(self, task):
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _cleanup_loop(self):
        cfg = self.config.plugin
        while True:
            await asyncio.sleep(3600)
            try:
                if self.repo:
                    await asyncio.to_thread(
                        self.repo.cleanup, cfg.topic_active_ttl_hours,
                        cfg.closed_topic_retention_days, cfg.audit_retention_days,
                        cfg.evidence_retention_days)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._get_logger().exception("whale-opinion: 清理失败")

    # ---------- 范围判定 ----------
    def _scene(self, message: dict) -> str:
        cfg = self.config.plugin
        if message["platform"] == "webui":
            prefix = cfg.webui_test_prefix
            if prefix and message["user"].startswith("webui_user_" + prefix):
                return "webui:" + message["user"]
            return ""
        if message["platform"] != "qq" or message["bot"] != cfg.bot_account_id:
            return ""
        if message["group"]:
            return "group:" + message["group"] if message["group"] in cfg.group_ids else ""
        return "private:" + message["user"] if message["user"] in cfg.private_user_ids else ""

    def _addressed(self, message: dict, scene: str) -> bool:
        """群聊里只在确实面向她时才进入讨论。"""
        if scene.startswith(("private:", "webui:")):
            return True
        return bool(message.get("at_bot") or message.get("reply_to_bot"))

    # ---------- 消息获取 ----------
    async def _recent(self, session: str, since: float) -> list:
        now = time.time()
        try:
            raws = await self.ctx.message.get_by_time_in_chat(
                chat_id=session, start_time=since, end_time=now, limit=30,
                limit_mode="latest", filter_mai=True, filter_command=True)
        except Exception:
            return []
        rows = []
        for raw in sorted(raws or [], key=lambda r: float(r.get("timestamp") or 0)):
            message = normalize(raw, session) if isinstance(raw, dict) else None
            if not message:
                continue
            scene = self._scene(message)
            if not scene:
                continue
            message["scene"] = scene
            rows.append(message)
        return rows

    # ---------- Planner 钩子 ----------
    @HookHandler(
        "maisaka.planner.before_request",
        name="opinion_judge",
        description="读取已接受消息、吸收真实工具结果并注入讨论判断块；shadow 模式下不改请求。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=2500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def opinion_judge(self, **kwargs: Any) -> Dict[str, Any]:
        if not self.repo or not self.config.plugin.enabled:
            return {"action": "continue"}
        try:
            return await self._judge(kwargs)
        except Exception as exc:
            self._stats["errors"] += 1
            self._get_logger().warning("whale-opinion: Planner 判断失败 %s", type(exc).__name__)
            return {"action": "continue"}

    async def _judge(self, kwargs: dict) -> Dict[str, Any]:
        cfg = self.config.plugin
        session = str(kwargs.get("session_id") or "")
        if not session:
            return {"action": "continue"}

        # 1) 吸收本会话未处理的消息（按 message_id 去重，不受时间抖动影响）
        if session not in self._states:
            await self._restore_state(session)
        seen = self._processed.setdefault(session, set())
        messages = await self._recent(session, max(self.started, time.time() - 900))
        fresh = [m for m in messages if m["mid"] not in seen]
        self._get_logger().debug(
            "whale-opinion: planner hook session=%s 已接受消息=%d 新消息=%d",
            session[-24:], len(messages), len(fresh))
        if fresh:
            for message in fresh:
                seen.add(message["mid"])
            if len(seen) > 200:
                self._processed[session] = set(list(seen)[-200:])
            await self._handle_batch(session, fresh)

        # 2) 吸收宿主真实工具结果（可选）
        original_items = kwargs.get('items') or []
        items = [i for i in original_items if not (isinstance(i,dict) and i.get('source') == 'local.whale-opinion')]
        if items:
            kinds: Dict[str, int] = {}
            for item in items:
                key = str(item.get("kind") or item.get("type") or "?") if isinstance(item, dict) else type(item).__name__
                kinds[key] = kinds.get(key, 0) + 1
            self._get_logger().debug("whale-opinion: items=%d kinds=%s", len(items), kinds)
        if cfg.enable_web_evidence:
            await self._ingest_tools(session, items)

        # 3) 注入判断块
        state = self._states.get(session)
        if not state or not state.get('active') or cfg.shadow_mode:
            return ({'action':'continue','modified_kwargs':{**kwargs,'items':items}}
                    if items != original_items else {'action':'continue'})
        item = prompt_builder.build_planner_item(state, cfg.max_prompt_chars)
        if not item:
            return {"action": "continue"}
        items = list(items)
        items.append(item)
        self._stats["blocks"] += 1
        return {"action": "continue", "modified_kwargs": {**kwargs, "items": items}}

    async def _restore_state(self, session: str) -> dict | None:
        """重启后从数据库恢复最近的开放话题，避免讨论凭空消失。"""
        if not self.repo:
            return None
        cfg = self.config.plugin
        row = await asyncio.to_thread(self.repo.latest_topic, session,
                                      float(cfg.topic_active_ttl_hours))
        if not row or row.get("discussion_state") != "open":
            return None
        recent = await self._recent(session, time.time()-86400)
        if recent:
            latest = recent[-1]
            restored = {'active':True,'topic_key':row.get('topic_key','')}
            routed = route(latest['text'], addressed=self._addressed(latest, latest['scene']), recent_discussion=True)
            if looks_like_stop(latest['text']) or (
                    not looks_like_pressure(latest['text']) and not routed.needs_discussion and (
                        routed.kind in ('preference','emotion','casual') or not continues_discussion(latest['text'],restored))):
                await self._suspend_discussion(session, restored, 'restore-casual')
                return None
        from .claim_parser import detect_subject
        state = {
            "active": True, "session": session, "user_key": row.get("user_key", ""),
            "topic_key": row.get("topic_key", ""), "scope": row.get("scope", ""),
            "criteria": row.get("criteria_json") or list(cfg.default_criteria),
            "user_position": row.get("user_position", ""),
            "user_subject": detect_subject(row.get("user_position", "")),
            "stance_state": row.get("bot_stance", "uncertain"),
            "stance_label": stance_mod.STANCE_LABELS.get(row.get("bot_stance", "uncertain"), "暂不站边"),
            "position": row.get("bot_position", ""), "main_reason": row.get("main_reason", ""),
            "agreement_points": row.get("agreement_json") or [],
            "uncertainties": row.get("uncertainties_json") or [],
            "evidence_status": row.get("evidence_status", "none"),
            "evidence": [], "should_close": False, "repeat_suppressed": False,
            "no_new_info_rounds": int(row.get("no_new_info_rounds") or 0),
            "reasons": row.get("reasons_json") or [], "tone_hint": "温软、简短，一两句为主",
            "restored": True,
        }
        self._states[session] = state
        self._get_logger().info("whale-opinion: 已从数据库恢复讨论 %s（立场=%s）",
                                state["topic_key"][:40], state["stance_state"])
        return state

    async def _suspend_discussion(self, session, prior, batch):
        self._states[session] = {'active':False,'session':session,'batch_id':batch}
        self._tool_buffer.pop(session,None)
        if prior.get('topic_key'):
            await asyncio.to_thread(self.repo.close_topic,session,prior['topic_key'])
            await asyncio.to_thread(self.repo.audit,session,prior['topic_key'],'suspend',
                                    '已转入喜好、情绪或日常聊天，停止注入旧讨论')

    async def _handle_batch(self, session: str, messages: list) -> None:
        """处理一批新消息：路由 → 解析 → 证据 → 动作 → 状态。"""
        cfg = self.config.plugin
        latest = messages[-1]
        scene = latest["scene"]
        text = latest["text"]
        addressed = self._addressed(latest, scene)
        batch = batch_id(session, [m["mid"] for m in messages])

        prior = self._states.get(session) or {}
        if not prior.get('active'):
            prior = {}
        route_result = route(text, addressed=addressed, recent_discussion=bool(prior))
        self._get_logger().debug(
            "whale-opinion: batch scene=%s addressed=%s text=%r kind=%s needs=%s",
            scene, addressed, text[:40], route_result.kind, route_result.needs_discussion)
        stop = looks_like_stop(text)
        pressure = looks_like_pressure(text)

        if stop or (not pressure and not route_result.needs_discussion and (
                route_result.kind in ('preference', 'emotion', 'casual') or not continues_discussion(text, prior))):
            # Keep an inactive sentinel so the next planner/tool pass cannot restore the old topic.
            await self._suspend_discussion(session, prior, batch)
            return

        if not route_result.needs_discussion and not prior:
            return

        claim = parse_rules(text, cfg.default_criteria)
        if prior and self._same_topic(prior, claim):
            # 同一场讨论里的补充/表态沿用原话题，不另起新话题
            claim.topic_key = prior.get("topic_key") or claim.topic_key
        elif not route_result.needs_discussion and prior:
            claim.topic_key = prior.get("topic_key") or claim.topic_key
        topic_key = claim.topic_key or (prior.get("topic_key") or "")
        if not topic_key:
            return

        row = await asyncio.to_thread(self.repo.get_topic, session, topic_key)
        evidence_rows = await asyncio.to_thread(self.repo.list_evidence, session, topic_key, 8)
        summary = summarize([r for r in evidence_rows if r.get("success")])
        objects = self._candidate_objects(claim.subject, evidence_rows)

        prior_state = (row or {}).get("bot_stance", "")
        prior_reasons = (row or {}).get("reasons_json") or []
        new_info = bool(pressure is False and (claim.reason or "").strip()
                        and not any(claim.reason in r for r in prior_reasons))
        rounds_without = int((row or {}).get("no_new_info_rounds") or 0)

        decision = decide_action(TurnContext(
            route_kind=route_result.kind, needs_discussion=route_result.needs_discussion,
            new_information=new_info, prior_state=prior_state, prior_reasons=prior_reasons,
            new_reason=claim.reason, rounds_without_info=rounds_without,
            review_after=cfg.discussion_review_after_turns, no_info_limit=cfg.no_new_information_limit,
            stop_requested=False, pressure=pressure, evidence_status=summary.status,
            evidence_supports_user=bool(objects and claim.subject and claim.subject in objects),
            asks_question=route_result.asks_question, shadow=cfg.shadow_mode))

        verdict = stance_mod.decide(
            user_position=claim.user_position, user_subject=claim.subject,
            evidence_summary=summary, user_criteria=claim.criteria,
            default_criteria=list(cfg.default_criteria), prior_state=prior_state,
            prior_position=(row or {}).get("bot_position", ""), new_information=new_info,
            evidence_objects=objects)

        if not new_info and stance_mod.keeps_verdict_without_new_evidence(verdict.state, prior_state):
            # 没有新依据时不改变结论（T03）
            verdict.state = prior_state
            verdict.position = (row or {}).get("bot_position", "") or verdict.position
            verdict.changed = False
            verdict.reason_code = "kept_without_new_evidence"

        reasons = list(prior_reasons)
        if claim.reason:
            reasons = ([claim.reason] + reasons)[:cfg.repeat_window_turns]

        keep_position = bool(prior.get("user_position")) and route_result.asks_question
        state = {
            "active": True, "batch_id": batch, "session": session, "scene": scene, "user_key": latest["user"],
            "topic_key": topic_key, "event_ref": claim.event_ref,
            "scope": claim.scope or (row or {}).get("scope", ""),
            "criteria": claim.criteria or list(cfg.default_criteria),
            "user_position": (prior.get("user_position") if keep_position else claim.user_position),
            "user_subject": (prior.get("user_subject") if keep_position else claim.subject),
            "stance_state": verdict.state,
            "stance_label": stance_mod.STANCE_LABELS.get(verdict.state, verdict.state),
            "position": verdict.position, "main_reason": verdict.main_reason,
            "agreement_points": verdict.agreement_points,
            "uncertainties": verdict.uncertainties or summary.uncertainties,
            "evidence": [r.get("title") or r.get("url") for r in evidence_rows[:3]],
            "evidence_status": summary.status,
            "action_reason": decision.reason, "action": decision.action,
            "repeat_suppressed": decision.repeat_suppressed,
            "should_close": decision.should_close or decision.action == ACTION_CLOSE,
            "needs_search": decision.action == ACTION_SEARCH,
            "ask_question": decision.action == ACTION_CLARIFY or (route_result.asks_question and not row),
            "forced_choice": bool(re.search(r"必须|一定要|就选一个|选一个|只能选", text)),
            "tone_hint": "温软、简短，一两句为主",
            "changed": verdict.changed,
        }
        self._states[session] = state
        self._stats["batches"] += 1

        await asyncio.to_thread(self.repo.upsert_topic, {
            "session_id": session, "user_key": latest["user"], "topic_key": topic_key,
            "event_ref": claim.event_ref, "scope": state["scope"], "criteria": state["criteria"],
            "user_position": (prior.get("user_position") if keep_position else claim.user_position),
            "user_subject": (prior.get("user_subject") if keep_position else claim.subject), "bot_stance": verdict.state,
            "bot_position": verdict.position, "main_reason": verdict.main_reason,
            "agreement_points": verdict.agreement_points, "uncertainties": state["uncertainties"],
            "evidence_status": summary.status, "discussion_state":
                "closed" if state["should_close"] else "open",
            "no_new_info_rounds": next_round_count(rounds_without, new_info),
            "reasons": reasons,
        })
        await asyncio.to_thread(self.repo.add_turn, {
            "session_id": session, "user_key": latest["user"], "batch_id": batch,
            "message_id": latest["mid"], "action": decision.action, "new_info": new_info,
            "stance_before": prior_state, "stance_after": verdict.state,
        })
        buffered = self._tool_buffer.pop(session, [])
        if buffered and cfg.enable_web_evidence:
            for raw in buffered:
                record = classify(raw["tool"], raw["content"], raw["success"],
                                  query=claim.user_position[:60])
                if await asyncio.to_thread(self.repo.add_evidence, session, topic_key, record.to_repo()):
                    self._get_logger().info(
                        "whale-opinion: 补录证据 kind=%s read=%s tool=%s",
                        record.kind, record.read_level, record.tool_name)
            state = await self._recompute(session, state)
        await asyncio.to_thread(self.repo.audit, session, topic_key, decision.action,
                                decision.reason, 0, 0, "")
        self._get_logger().info(
            "whale-opinion: %s | 话题=%s | 路由=%s | 立场=%s | 动作=%s%s",
            scene, topic_key[:40], route_result.kind, verdict.state, decision.action,
            "（试运行）" if cfg.shadow_mode else "")

        # 4) 需要更细的结构时再做一次异步模型分析（不阻塞本轮）
        if self._should_analyze(session, route_result.needs_discussion, row is None, new_info):
            self._track(asyncio.create_task(self._analyze(session, batch, text, claim, state)))

    def _same_topic(self, prior: dict, claim) -> bool:
        """判断本轮是否还在同一个事件里讨论。"""
        prior_event = str(prior.get("event_ref") or "")
        new_event = str(claim.event_ref or "")
        if prior_event and new_event and prior_event != new_event and len(new_event) > 4:
            return False
        return bool(prior.get("topic_key"))

    def _should_analyze(self, session: str, needs: bool, new_topic: bool, new_info: bool) -> bool:
        cfg = self.config.plugin
        if not needs:
            return False
        last = self._last_analysis.get(session, 0.0)
        if time.time() - last < cfg.analysis_min_interval_seconds:
            return False
        return new_topic or new_info

    async def _analyze(self, session: str, batch: str, text: str, claim, state: dict) -> None:
        """一次结构化分析；失败或超时都不影响已有状态。"""
        cfg = self.config.plugin
        self._last_analysis[session] = time.time()
        self._stats["analyses"] += 1
        prompt = [
            {"role": "system", "content": ANALYSIS_SYSTEM},
            {"role": "user", "content": text[:1200]},
        ]
        try:
            result = await asyncio.wait_for(
                self.ctx.llm.generate(
                    prompt=prompt, task_name=cfg.analysis_task, temperature=0.1,
                    max_tokens=cfg.analysis_output_tokens_max,
                    rpc_timeout_ms=int(cfg.analysis_timeout_seconds * 1000) + 5000),
                timeout=cfg.analysis_timeout_seconds)
        except asyncio.TimeoutError:
            await asyncio.to_thread(self.repo.audit, session, state.get("topic_key", ""),
                                    "analysis", "模型分析超时", 1, 0, "timeout")
            return
        except Exception as exc:
            await asyncio.to_thread(self.repo.audit, session, state.get("topic_key", ""),
                                    "analysis", "模型分析失败", 1, 0, type(exc).__name__)
            return

        response = ""
        if isinstance(result, dict):
            response = str(result.get("response") or result.get("content") or "")
        refined, used = parse_llm_result(response, claim)
        if not used:
            await asyncio.to_thread(self.repo.audit, session, state.get("topic_key", ""),
                                    "analysis", "模型输出无法解析，保留规则结果", 1, 0, "invalid_output")
            return

        current = self._states.get(session)
        if not current or not current.get('active') or current.get('batch_id') != batch:
            return
        if refined.topic_key and refined.topic_key != current.get("topic_key"):
            return
        current = dict(current)
        current["criteria"] = refined.criteria or current.get("criteria")
        current["scope"] = refined.scope or current.get("scope")
        self._states[session] = current
        await asyncio.to_thread(self.repo.audit, session, current.get("topic_key", ""),
                                "analysis", "结构化分析已采纳", 1, 0, "")

    # ---------- 证据适配 ----------
    async def _ingest_tools(self, session: str, items: list) -> None:
        results = extract_tool_results(items)
        state = self._states.get(session)
        if state is not None and not state.get('active'):
            return
        if not state:
            state = await self._restore_state(session)
        if self._states.get(session, {}).get('active') is False:
            return
        if not state:
            # 工具可能先于讨论建立；先缓存，等话题出现再归入
            if results:
                buffer = self._tool_buffer.setdefault(session, [])
                buffer.extend(results)
                del buffer[:-10]
                self._get_logger().debug(
                    "whale-opinion: 暂无话题，缓存工具结果 %d 条（累计 %d）",
                    len(results), len(buffer))
            return
        if not results:
            return
        self._get_logger().debug(
            "whale-opinion: 发现工具结果 %d 条: %s",
            len(results), [r["tool"] for r in results][:5])
        topic_key = state.get("topic_key", "")
        added = 0
        for raw in results:
            record = classify(raw["tool"], raw["content"], raw["success"],
                              query=state.get("user_position", "")[:60])
            if record.kind == "unknown":
                continue
            if await asyncio.to_thread(self.repo.add_evidence, session, topic_key, record.to_repo()):
                added += 1
                self._get_logger().info(
                    "whale-opinion: 采纳证据 kind=%s read=%s tool=%s title=%s",
                    record.kind, record.read_level, record.tool_name, record.title[:40])
        if not added:
            return
        await asyncio.to_thread(self.repo.audit, session, topic_key, "evidence",
                                "吸收 %d 条工具结果" % added, 0, added, "")
        await self._recompute(session, self._states[session])

    async def _recompute(self, session: str, state: dict, *, new_info: bool = True) -> dict:
        """拿到新证据后重算立场，并同步到数据库（证据 → 判断那条线）。"""
        cfg = self.config.plugin
        topic_key = state.get("topic_key") or ""
        rows = await asyncio.to_thread(self.repo.list_evidence, session, topic_key, 8)
        current = self._states.get(session)
        if current is not None and (not current.get('active') or current.get('batch_id') != state.get('batch_id')):
            return current
        summary = summarize([r for r in rows if r.get("success")])
        objects = self._candidate_objects(state.get("user_subject") or "", rows)
        prior_state = state.get("stance_state") or ""
        verdict = stance_mod.decide(
            user_position=state.get("user_position", ""),
            user_subject=state.get("user_subject", ""),
            evidence_summary=summary,
            user_criteria=state.get("criteria") or [],
            default_criteria=list(cfg.default_criteria),
            prior_state=prior_state,
            prior_position=state.get("position", ""),
            new_information=new_info,
            evidence_objects=objects)
        changed = (verdict.state != prior_state) or (summary.status != state.get("evidence_status"))
        state = dict(state)
        state.update({
            "stance_state": verdict.state,
            "stance_label": stance_mod.STANCE_LABELS.get(verdict.state, verdict.state),
            "position": verdict.position or state.get("position", ""),
            "main_reason": verdict.main_reason or state.get("main_reason", ""),
            "agreement_points": verdict.agreement_points or state.get("agreement_points", []),
            "evidence_status": summary.status,
            "uncertainties": summary.uncertainties or state.get("uncertainties", []),
            "evidence": [r.get("title") or r.get("url") for r in rows[:3]],
            "need_other_source": any(
                ("未抽取到" in (r.get("limitation") or "")) or ("验证" in (r.get("limitation") or ""))
                for r in rows),
            "changed": bool(verdict.changed or changed),
        })
        self._states[session] = state
        if topic_key:
            await asyncio.to_thread(self.repo.upsert_topic, {
                "session_id": session, "user_key": state.get("user_key", ""),
                "topic_key": topic_key, "event_ref": state.get("event_ref", ""),
                "scope": state.get("scope", ""), "criteria": state.get("criteria") or [],
                "user_position": state.get("user_position", ""),
                "bot_stance": verdict.state, "bot_position": state["position"],
                "main_reason": state["main_reason"],
                "agreement_points": state.get("agreement_points") or [],
                "uncertainties": state.get("uncertainties") or [],
                "evidence_status": summary.status,
                "discussion_state": "closed" if state.get("should_close") else "open",
                "no_new_info_rounds": int(state.get("no_new_info_rounds") or 0),
                "reasons": state.get("reasons") or [],
            })
        if changed:
            await asyncio.to_thread(self.repo.audit, session, topic_key, "stance",
                                    "证据变化后立场=%s（证据=%s）" % (verdict.state, summary.status), 0, 0, "")
            self._get_logger().info(
                "whale-opinion: 立场更新 %s → %s（证据=%s，独立来源=%d）",
                prior_state or "无", verdict.state, summary.status, summary.independent_sources)
        return state

    def _candidate_objects(self, subject: str, rows: list) -> list:
        """从证据里找出被支持的对象；找不到就返回空，交给 uncertain。"""
        if not subject:
            return []
        objects = []
        for row in rows:
            snippet = str(row.get("snippet") or "")
            if subject and subject in snippet:
                objects.append(subject)
                continue
            for match in re.finditer(r"([A-Za-z]{1,6}|[\u4e00-\u9fa5]{1,4})\s*(?:打得更好|更强|更稳|表现更好|发挥更好|拿下|获胜|最佳)", snippet):
                name = match.group(1)
                if name and name not in objects:
                    objects.append(name)
        return objects[:4]

    # ---------- Replyer 钩子 ----------
    @HookHandler(
        "maisaka.replyer.before_request",
        name="opinion_voice",
        description="把本轮立场与表达边界写入 extra_prompt；重试时替换旧块。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=1200,
        error_policy=ErrorPolicy.SKIP,
    )
    async def opinion_voice(self, **kwargs: Any) -> Dict[str, Any]:
        if not self.repo or not self.config.plugin.enabled:
            return {"action": "continue"}
        cfg = self.config.plugin
        if cfg.shadow_mode:
            return {"action": "continue"}
        session = str(kwargs.get("session_id") or "")
        state = self._states.get(session)
        if not state or not state.get('active'):
            original = str(kwargs.get('extra_prompt') or '')
            cleaned = prompt_builder.strip_block(original, STANCE_OPEN, STANCE_CLOSE).strip()
            return ({'action':'continue', 'modified_kwargs':{**kwargs,'extra_prompt':cleaned}}
                    if cleaned != original else {'action':'continue'})
        session2 = session
        message_id = str(kwargs.get("reply_message_id") or "")
        if message_id:
            cached = self._cache.get((session2, message_id))
            if cached and cached.get("user"):
                state = dict(state)
                state["user_key"] = cached["user"]
        if message_id:
            self._cache[(session2, message_id)] = {"session": session2, "user": state.get("user_key", "")}
            while len(self._cache) > 512:
                self._cache.popitem(last=False)
        block = prompt_builder.build_replyer_block(state, cfg.max_prompt_chars)
        if not block:
            return {"action": "continue"}
        extra = prompt_builder.merge_block(str(kwargs.get("extra_prompt") or ""), block,
                                           STANCE_OPEN, STANCE_CLOSE)
        self._stats["blocks"] += 1
        return {"action": "continue", "modified_kwargs": {**kwargs, "extra_prompt": extra}}

    # ---------- 发送提交 ----------
    @HookHandler(
        "send_service.after_send",
        name="opinion_commit",
        description="仅在真实发送成功后提交已表达立场；重试幂等。",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=800,
        error_policy=ErrorPolicy.SKIP,
    )
    async def opinion_commit(self, **kwargs: Any) -> None:
        if not self.repo or not self.config.plugin.enabled:
            return
        if not bool(kwargs.get("sent")):
            return
        try:
            session = str(kwargs.get("session_id") or "")
            message = kwargs.get("message") or {}
            reply_mid = str(kwargs.get("reply_message_id") or "")
            if not session and reply_mid:
                for (sess, mid), _info in self._cache.items():
                    if mid == reply_mid:
                        session = sess
                        break
            if isinstance(message, dict):
                session = session or str(message.get("session_id") or "")
            state = self._states.get(session)
            if not state or not state.get("position"):
                return
            await asyncio.to_thread(self.repo.mark_expressed, session,
                                    state.get("topic_key", ""), state["position"])
            self._stats["sent"] += 1
            self._stats["blocks"] += 0
        except Exception as exc:
            self._stats["errors"] += 1
            self._get_logger().debug("whale-opinion: 发送提交失败 %s", type(exc).__name__)

    # ---------- 状态接口（供 WebUI / 自检） ----------
    async def snapshot(self) -> dict:
        """当前状态快照，供管理页与自检使用。"""
        topics = []
        if self.repo:
            topics = await asyncio.to_thread(self.repo.list_topics, 10)
            stats = await asyncio.to_thread(self.repo.stats)
        else:
            stats = {}
        return {
            "enabled": self.config.plugin.enabled,
            "shadow_mode": self.config.plugin.shadow_mode,
            "scenes": {"private": self.config.plugin.private_user_ids,
                       "group": self.config.plugin.group_ids},
            "active_topics": [
                {"topic_key": t["topic_key"], "stance": t["bot_stance"],
                 "position": t["bot_position"][:80], "explained": t["last_sent_position"][:80],
                 "state": t["discussion_state"], "evidence": t["evidence_status"]}
                for t in topics],
            "runtime": dict(self._stats), "db": stats,
        }


    # ---------- 管理页接口 ----------
    @API("overview", description="运行状态与统计", version="1", public=False)
    async def api_overview(self):
        data = await self.snapshot()
        data["ok"] = True
        return data

    @API("topics", description="最近的讨论话题", version="1", public=False)
    async def api_topics(self):
        rows = await asyncio.to_thread(self.repo.list_topics, 20) if self.repo else []
        items = [{
            "topic_key": r["topic_key"][:40], "scene": r["session_id"][:18],
            "user": r["user_key"][-6:], "stance": r["bot_stance"],
            "state": r["discussion_state"], "evidence": r["evidence_status"],
            "position": (r["bot_position"] or "")[:40],
            "explained": (r["last_sent_position"] or "")[:40],
        } for r in rows]
        return {"ok": True, "items": items}

    @API("audit", description="最近的讨论动作与错误", version="1", public=False)
    async def api_audit(self):
        rows = await asyncio.to_thread(self.repo.recent_audit, 20) if self.repo else []
        items = [{
            "time": time.strftime("%m-%d %H:%M", time.localtime(r["created_utc"])),
            "topic": (r["topic_key"] or "")[:30], "action": r["category"],
            "reason": (r["reason"] or "")[:40], "calls": r["model_calls"],
            "error": r["error_type"],
        } for r in rows]
        return {"ok": True, "items": items}


def create_plugin():
    return WhaleOpinion()
