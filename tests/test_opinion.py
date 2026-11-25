"""离线验收：用固定虚构资料覆盖方案 §18 的关键约束。

运行：
    python -X utf8 -m unittest discover -s instance/plugins/whale-opinion/tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import discussion
import evidence as evidence_mod
import prompt_builder
import stance
import topic_router
from claim_parser import parse_llm_result, parse_rules
from repository import Repository

DEFAULT_CRITERIA = ["稳定性", "关键决策", "团队贡献", "精彩操作"]


def record(title: str, snippet: str, kind: str = evidence_mod.KIND_VERIFIED,
           read_level: str = "full", url: str = "https://example.test/match", success: bool = True):
    return evidence_mod.EvidenceRecord(
        kind=kind, tool_name="read_webpage", url=url, host="example.test",
        title=title, snippet=snippet, read_level=read_level, success=success,
        source_group="%s|%s" % ("example.test", title[:18]))


def summary_of(*records):
    return evidence_mod.summarize(list(records))


class RouterTests(unittest.TestCase):
    def test_t09_preference_not_debated(self):
        result = topic_router.route("我最喜欢看A玩这个英雄")
        self.assertEqual(result.kind, "preference")
        self.assertFalse(result.needs_discussion)

    def test_t10_emotion_not_debated(self):
        result = topic_router.route("气死了，这把怎么输了")
        self.assertEqual(result.kind, "emotion")
        self.assertFalse(result.needs_discussion)

    def test_evaluation_triggers_discussion(self):
        result = topic_router.route("今天这场A打得最好，因为关键团都是他开的")
        self.assertEqual(result.kind, "evaluation")
        self.assertTrue(result.needs_discussion)

    def test_bare_best_word_alone_does_not_debate(self):
        result = topic_router.route("A最好")
        self.assertFalse(result.needs_discussion and not result.asks_question)

    def test_pressure_detected(self):
        self.assertTrue(topic_router.looks_like_pressure("你必须承认我才是对的"))

    def test_stop_detected(self):
        self.assertTrue(topic_router.looks_like_stop("算了不讨论这个了"))


class StanceTests(unittest.TestCase):
    def test_t01_agrees_when_evidence_supports_user(self):
        summary = summary_of(record("比赛详情", "A 这场发挥最好，关键团决策到位"))
        result = stance.decide(
            user_position="A打得最好", user_subject="A", evidence_summary=summary,
            user_criteria=DEFAULT_CRITERIA, default_criteria=DEFAULT_CRITERIA,
            evidence_objects=["A"])
        self.assertEqual(result.state, stance.AGREE_ADD)
        self.assertNotEqual(result.state, stance.DISAGREE)

    def test_t02_disagrees_with_evidence_for_other_object(self):
        summary = summary_of(record("比赛详情", "B 两次关键决策影响更大，整场更稳"))
        result = stance.decide(
            user_position="A打得最好", user_subject="A", evidence_summary=summary,
            user_criteria=DEFAULT_CRITERIA, default_criteria=DEFAULT_CRITERIA,
            evidence_objects=["B"])
        self.assertEqual(result.state, stance.DISAGREE)
        self.assertIn("B", result.position)

    def test_t03_verdict_does_not_follow_user_stance(self):
        summary = summary_of(record("比赛详情", "B 的关键决策影响更大"))
        for user_position, user_subject in (("A最好", "A"), ("B最好", "B")):
            result = stance.decide(
                user_position=user_position, user_subject=user_subject,
                evidence_summary=summary, user_criteria=DEFAULT_CRITERIA,
                default_criteria=DEFAULT_CRITERIA, evidence_objects=["B"])
            # 同一个证据口径下，对不同用户立场的结论都不该无依据翻转
            self.assertIn(result.state, (stance.AGREE_ADD, stance.AGREE, stance.DISAGREE))

    def test_t06_revises_with_new_evidence(self):
        summary = summary_of(record("补充回放", "A 前期把劣势打回来，后期决策也更关键"))
        result = stance.decide(
            user_position="A打得最好", user_subject="A", evidence_summary=summary,
            user_criteria=DEFAULT_CRITERIA, default_criteria=DEFAULT_CRITERIA,
            prior_state=stance.DISAGREE, prior_position="我更偏向B",
            new_information=True, evidence_objects=["A"])
        self.assertEqual(result.state, stance.REVISED)
        self.assertTrue(result.changed)

    def test_t11_uncertain_without_evidence(self):
        result = stance.decide(
            user_position="A打得最好", user_subject="A",
            evidence_summary=evidence_mod.summarize([]), user_criteria=[],
            default_criteria=DEFAULT_CRITERIA)
        self.assertEqual(result.state, stance.UNCERTAIN)
        self.assertFalse(result.changed)

    def test_t12_different_criteria(self):
        result = stance.decide(
            user_position="A最好", user_subject="A",
            evidence_summary=summary_of(record("资料", "A 表现不错")),
            user_criteria=["数据表现"], default_criteria=DEFAULT_CRITERIA,
            evidence_objects=["A"])
        self.assertEqual(result.state, stance.DIFF_CRITERIA)

    def test_no_flip_without_evidence(self):
        keep = stance.keeps_verdict_without_new_evidence
        self.assertFalse(keep(stance.DISAGREE, stance.DISAGREE))
        self.assertTrue(keep(stance.AGREE, stance.DISAGREE))
        self.assertFalse(keep(stance.AGREE, stance.UNCERTAIN))


class EvidenceTests(unittest.TestCase):
    def test_t14_summary_only_is_partial(self):
        summary = summary_of(record("搜索摘要", "A 与 B 的比赛结果", kind=evidence_mod.KIND_REPORTED,
                                    read_level="summary"))
        self.assertEqual(summary.status, "partial")
        self.assertFalse(summary.has_full_text)
        self.assertTrue(any("未读正文" in u for u in summary.uncertainties))

    def test_t13_same_source_group_counted_once(self):
        a = record("同一篇报道", "内容一致", url="https://a.example/x")
        b = record("同一篇报道", "内容一致", url="https://b.example/x")
        summary = summary_of(a, b)
        self.assertEqual(summary.independent_sources, 1)

    def test_failed_tool_is_not_evidence(self):
        summary = summary_of(record("失败", "", read_level="none", success=False))
        self.assertEqual(summary.status, "none")
        self.assertTrue(any("失败" in u for u in summary.uncertainties))

    def test_extract_tool_results_from_items(self):
        items = [
            {"kind": "tool_result", "tool": "read_webpage", "success": True,
             "content": "正文" * 400 + " https://example.test/a"},
            {"kind": "message", "content": "普通消息"},
            json.dumps({"kind": "tool_result", "tool": "internet_search", "success": False,
                        "content": "403"}),
        ]
        results = evidence_mod.extract_tool_results(items)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["tool"], "read_webpage")
        self.assertFalse(results[1]["success"])

    def test_unified_web_tool_names_are_accepted(self):
        fetched = evidence_mod.classify("web_fetch", "正文" * 400 + " https://example.test/a", True)
        self.assertEqual(fetched.kind, evidence_mod.KIND_VERIFIED)
        self.assertEqual(fetched.read_level, "full")

        searched = evidence_mod.classify("web_search", json.dumps({"results": [{
            "title": "公开结果", "url": "https://example.test/b", "snippet": "搜索摘要",
        }]}, ensure_ascii=False), True)
        self.assertEqual(searched.kind, evidence_mod.KIND_REPORTED)
        self.assertEqual(searched.read_level, "summary")

    def test_user_report_is_unverified(self):
        summary = summary_of(evidence_mod.user_report("A 拿了 MVP"))
        self.assertEqual(summary.status, "none")
        self.assertTrue(any("用户补充" in u for u in summary.uncertainties))


class DiscussionTests(unittest.TestCase):
    def test_t08_pressure_not_evidence(self):
        decision = discussion.decide_action(discussion.TurnContext(
            needs_discussion=True, pressure=True, new_information=False,
            prior_state=stance.DISAGREE, evidence_status="full"))
        self.assertTrue(decision.repeat_suppressed)
        self.assertEqual(decision.action, discussion.ACTION_ANSWER)

    def test_repeat_reason_suppressed(self):
        decision = discussion.decide_action(discussion.TurnContext(
            needs_discussion=True, new_reason="关键团都是他开的",
            prior_reasons=["关键团都是A开的"], new_information=False,
            prior_state=stance.DISAGREE, evidence_status="full", no_info_limit=2))
        self.assertTrue(decision.repeat_suppressed)

    def test_t15_stop_closes(self):
        decision = discussion.decide_action(discussion.TurnContext(
            needs_discussion=True, stop_requested=True))
        self.assertEqual(decision.action, discussion.ACTION_CLOSE)

    def test_no_info_limit_closes(self):
        decision = discussion.decide_action(discussion.TurnContext(
            needs_discussion=True, rounds_without_info=1, no_info_limit=2,
            prior_state=stance.DISAGREE, evidence_status="full"))
        self.assertEqual(decision.action, discussion.ACTION_CLOSE)

    def test_needs_search_without_evidence(self):
        decision = discussion.decide_action(discussion.TurnContext(
            needs_discussion=True, evidence_status="none"))
        self.assertEqual(decision.action, discussion.ACTION_SEARCH)

    def test_t20_batch_id_is_stable_and_stale_detected(self):
        one = discussion.batch_id("s", ["1", "2"])
        two = discussion.batch_id("s", ["2", "1"])
        self.assertEqual(one, two)
        self.assertTrue(discussion.is_stale(1, 2))
        self.assertFalse(discussion.is_stale(2, 2))


class PromptTests(unittest.TestCase):
    def test_long_state_keeps_wrappers_and_does_not_force_agreement_preface(self):
        block = prompt_builder.build_replyer_block({
            'active': True, 'stance_label': '不同意', 'position': 'B更稳' * 30,
            'main_reason': '关键决策' * 40, 'agreement_points': ['A也不错' * 20],
            'uncertainties': ['未核实的细节' * 50], 'tone_hint': '自然' * 30,
        })
        self.assertLessEqual(len(block), 900)
        self.assertTrue(block.endswith(prompt_builder.REPLYER_CLOSE))
        self.assertNotIn('先认可：', block)
        self.assertIn('假设与试演', block)

    def test_block_replacement_does_not_accumulate(self):
        block = prompt_builder.build_replyer_block({
            "active": True, "stance_label": "基本认同", "position": "我同意你的方向",
            "main_reason": "公开资料一致"})
        merged = prompt_builder.merge_block("原提示", block, prompt_builder.REPLYER_OPEN,
                                            prompt_builder.REPLYER_CLOSE)
        again = prompt_builder.merge_block(merged, block, prompt_builder.REPLYER_OPEN,
                                           prompt_builder.REPLYER_CLOSE)
        self.assertEqual(again.count(prompt_builder.REPLYER_OPEN), 1)
        self.assertTrue(again.startswith("原提示"))

    def test_inactive_state_builds_nothing(self):
        self.assertEqual(prompt_builder.build_planner_block({"active": False}), "")
        self.assertEqual(prompt_builder.build_replyer_block({"active": False}), "")

    def test_block_mentions_boundaries(self):
        block = prompt_builder.build_replyer_block({
            "active": True, "stance_label": "有依据的不同看法", "position": "我更偏向B",
            "main_reason": "B两次关键决策影响更大", "uncertainties": ["只看到摘要"]})
        self.assertIn("不要说“我看到/回放里”这类画面细节", block)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.tmp.name) / "opinions.sqlite3")
        self.repo.start()

    def tearDown(self):
        self.tmp.cleanup()

    def test_t18_sessions_are_isolated(self):
        self.repo.upsert_topic({"session_id": "s1", "user_key": "u1", "topic_key": "t",
                                "bot_stance": stance.DISAGREE, "bot_position": "B"})
        self.repo.upsert_topic({"session_id": "s2", "user_key": "u1", "topic_key": "t",
                                "bot_stance": stance.AGREE, "bot_position": "A"})
        first = self.repo.get_topic("s1", "t")
        second = self.repo.get_topic("s2", "t")
        self.assertEqual(first["bot_position"], "B")
        self.assertEqual(second["bot_position"], "A")

    def test_t20_turn_insert_is_idempotent(self):
        turn = {"session_id": "s", "batch_id": "b1", "user_key": "u"}
        self.assertTrue(self.repo.add_turn(turn))
        self.assertFalse(self.repo.add_turn(turn))

    def test_t22_send_failure_not_committed(self):
        self.repo.upsert_topic({"session_id": "s", "user_key": "u", "topic_key": "t",
                                "bot_position": "B"})
        self.repo.mark_turn_sent("s", "b1", False)
        topic = self.repo.get_topic("s", "t")
        self.assertEqual(topic["last_sent_position"], "")

    def test_evidence_dedupe(self):
        record_dict = evidence_mod.classify(
            "read_webpage", ("正文" * 200) + " https://example.test/a", True).to_repo()
        self.assertTrue(self.repo.add_evidence("s", "t", record_dict))
        self.assertFalse(self.repo.add_evidence("s", "t", record_dict))
        self.assertEqual(self.repo.count_evidence("s", "t"), 1)

    def test_revision_increments(self):
        self.repo.upsert_topic({"session_id": "s", "user_key": "u", "topic_key": "t"})
        second = self.repo.upsert_topic({"session_id": "s", "user_key": "u", "topic_key": "t",
                                         "bot_position": "B"})
        self.assertEqual(second["revision"], 2)

    def test_cleanup_removes_expired_topics(self):
        self.repo.upsert_topic({"session_id": "s", "user_key": "u", "topic_key": "old",
                                "discussion_state": "closed"})
        removed = self.repo.cleanup(active_hours=24, closed_days=0, audit_days=0, evidence_days=0)
        self.assertEqual(removed["topics"], 1)
        self.assertIsNone(self.repo.get_topic("s", "old"))


class ClaimParserTests(unittest.TestCase):
    def test_scope_and_criteria(self):
        claim = parse_rules("这场A打得最好，因为后半场关键团开得好", DEFAULT_CRITERIA)
        self.assertEqual(claim.scope, "整场比赛")
        self.assertIn("关键决策", claim.criteria)
        self.assertTrue(claim.reason)

    def test_llm_result_fallback_on_bad_json(self):
        base = parse_rules("A最好", DEFAULT_CRITERIA)
        refined, used = parse_llm_result("抱歉我不能输出", base)
        self.assertFalse(used)
        self.assertEqual(refined.user_position, base.user_position)


if __name__ == "__main__":
    unittest.main(verbosity=2)
