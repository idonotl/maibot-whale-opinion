import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from maibot_sdk import PluginContext, PluginPaths

ROOT=Path(__file__).resolve().parents[1]
pkg=types.ModuleType('opinion_boundary_test');pkg.__path__=[str(ROOT)];sys.modules[pkg.__name__]=pkg
plugin_mod=importlib.import_module(pkg.__name__+'.plugin')
router=importlib.import_module(pkg.__name__+'.topic_router')
repo_mod=importlib.import_module(pkg.__name__+'.repository')
parser=importlib.import_module(pkg.__name__+'.claim_parser')

class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p=plugin_mod.WhaleOpinion()
        self.p._set_context(PluginContext('local.whale-opinion',paths=PluginPaths(data_dir=Path(self.tmp.name),runtime_dir=Path(self.tmp.name))))
        self.p.set_plugin_config({'plugin':{'config_version':'0.1.0','enabled':True,'shadow_mode':False}})
        self.p.repo=repo_mod.Repository(Path(self.tmp.name)/'test.sqlite3');self.p.repo.start()
        self.p._states['s']={'active':True,'topic_key':'旧比赛','user_subject':'A','event_ref':'昨天','user_position':'A打得最好','position':'资料还不够我站边','batch_id':'old'}
        self.p.repo.upsert_topic({'session_id':'s','user_key':'u','topic_key':'旧比赛','user_position':'A打得最好','discussion_state':'open'})

    def message(self,text):
        return {'scene':'private:u','text':text,'user':'u','mid':'new','session':'s'}

    async def test_casual_chat_closes_persisted_topic_and_removes_stale_voice(self):
        await self.p._handle_batch('s',[self.message('肯德基')])
        self.assertFalse(self.p._states['s']['active'])
        self.assertEqual(self.p.repo.get_topic('s','旧比赛')['discussion_state'],'closed')
        result=await self.p.opinion_voice(session_id='s',extra_prompt='MEMORY\n[[OPINION-VOICE]]旧观点[[/OPINION-VOICE]]')
        self.assertEqual(result['modified_kwargs']['extra_prompt'],'MEMORY')

    async def test_repeated_planner_and_tools_do_not_resurrect_topic(self):
        await self.p._handle_batch('s',[self.message('原味鸡香不香？')])
        await self.p._ingest_tools('s',[{'item_type':'FunctionCallOutputItem','tool_name':'read_webpage','output':'旧资料','success':True}])
        self.assertFalse(self.p._states['s']['active'])
        self.assertIsNone(await self.p._restore_state('s'))

    async def test_restart_with_open_old_topic_checks_latest_conversation(self):
        self.p._states.clear()
        self.p._recent=AsyncMock(return_value=[self.message('原味鸡配薯条')])
        self.assertIsNone(await self.p._restore_state('s'))
        self.assertFalse(self.p._states['s']['active'])

    async def test_late_analysis_cannot_reactivate_suspended_state(self):
        old=self.p._states['s'].copy()
        async def generate(**kwargs):
            await self.p._suspend_discussion('s',old,'new')
            return {'response':'{"criteria":["数据表现"]}'}
        self.p.ctx.llm.generate=generate
        await self.p._analyze('s','old','A最好',parser.parse_rules('A最好'),old)
        self.assertFalse(self.p._states['s']['active'])

    async def test_game_evidence_followup_is_preserved(self):
        self.assertTrue(router.continues_discussion('你漏了A前期把劣势打回来的那段，那波也很关键',self.p._states['s']))
        self.assertFalse(router.continues_discussion('我下班准备吃饭了',self.p._states['s']))
        self.p._should_analyze=lambda *args: False
        await self.p._handle_batch('s',[self.message('你漏了A前期把劣势打回来的那段，那波也很关键')])
        self.assertTrue(self.p._states['s']['active'])
        self.assertEqual(self.p._states['s']['topic_key'],'旧比赛')

    async def test_new_discussion_can_start_after_casual_chat(self):
        self.p._should_analyze=lambda *args: False
        await self.p._handle_batch('s',[self.message('肯德基')])
        await self.p._handle_batch('s',[self.message('今天这场A打得最好，因为关键团都是他开的')])
        self.assertTrue(self.p._states['s']['active'])
        self.assertNotEqual(self.p._states['s']['topic_key'],'旧比赛')

    async def test_explicit_topic_change_does_not_repeat_old_verdict(self):
        await self.p._handle_batch('s',[self.message('不聊这个了，今天吃肯德基')])
        self.assertFalse(self.p._states['s']['active'])
        self.assertEqual(self.p.repo.get_topic('s','旧比赛')['discussion_state'],'closed')

    async def test_pressure_does_not_erase_active_verdict(self):
        self.p._should_analyze=lambda *args: False
        await self.p._handle_batch('s',[self.message('你必须承认')])
        self.assertTrue(self.p._states['s']['active'])
        self.p._states.clear()
        self.p._recent=AsyncMock(return_value=[self.message('你必须承认')])
        restored=await self.p._restore_state('s')
        self.assertTrue(restored['active'])

    async def test_preferences_and_objective_food_questions_are_distinct(self):
        for text in ('你觉得原味鸡好不好吃？','原味鸡香不香','哪种口味最好吃？','你选蓝色还是白色？','这个杯子起名字叫啥？','你觉得这件衣服好看吗？'):
            r=router.route(text,recent_discussion=True)
            self.assertFalse(r.needs_discussion,text)
            self.assertEqual(r.kind,'preference',text)
        self.assertNotEqual(router.route('原味鸡的热量是不是很高？').kind,'preference')
        self.assertTrue(router.route('今天这场A打得最好，因为关键团都是他开的').needs_discussion)

    async def test_casual_exam_question_does_not_inherit_match_verdict(self):
        await self.p._handle_batch('s',[self.message('那你觉得谁更能考上？')])
        self.assertFalse(self.p._states['s']['active'])
        result=await self.p.opinion_voice(session_id='s',extra_prompt='MEMORY\n[[OPINION-VOICE]]旧结论[[/OPINION-VOICE]]')
        self.assertEqual(result['modified_kwargs']['extra_prompt'],'MEMORY')

    async def test_formal_followup_and_playful_choice_are_distinct(self):
        for text in ('那你觉得谁更能考上？','你觉得我该选哪个对手？','按梗玩，A和B谁最强？'):
            self.assertFalse(router.route(text,recent_discussion=True).needs_discussion,text)
        self.assertTrue(router.route('你觉得谁能考上？请认真分析备考数据').needs_discussion)
        self.assertTrue(router.route('那你觉得呢？',recent_discussion=True).needs_discussion)
        self.assertFalse(router.route('那你觉得呢？').needs_discussion)

    async def test_uncertain_forced_choice_does_not_require_definite_answer(self):
        builder=importlib.import_module(pkg.__name__+'.prompt_builder')
        text=builder.build_replyer_block({'active':True,'forced_choice':True,'stance_state':'uncertain','position':'资料不足'})
        self.assertNotIn('直接选一个',text)
        self.assertIn('新增依据',text)
        self.assertNotIn('可以这样说',text)

if __name__=='__main__':unittest.main()
