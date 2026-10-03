"""对话编排回归测试；合成数据只用于验证，不进入应用。"""
from copy import deepcopy
import json
import socket
from threading import Event
from unittest import TestCase, main
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from conversation import (Conversation, DialoguePlan, Operation, accept, apply_plan,
    can_execute, conflicts, describe, dismiss, request_signature, respond, rule_plan,
    sync_manual, result_reply)
from data import DataClient, FetchStats
from parse import rule_query
from screen import run_screen
from selftest_input_flow import fixture


def metric(state, name, pending=False):
    r = state.pending if pending else state.request
    return [c for c in r.conditions if c.metric == name]


class ConversationTests(TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('测试不允许联网'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_multi_turn_add_update_delete_undo(self):
        s = respond(Conversation(), '银行股，PE低于10')
        pe_id = metric(s, 'pe_ttm')[0].id
        s = respond(s, '再加一个波动小')
        self.assertTrue(s.awaiting)
        self.assertFalse(can_execute(s))
        self.assertEqual(len(s.request.conditions), 1)
        s = respond(s, '25%')
        self.assertTrue(can_execute(s))
        self.assertEqual(metric(s, 'vol_252')[0].threshold, .25)
        self.assertEqual(metric(s, 'vol_252')[0].type, 'hard')
        s = respond(s, 'PE改为8')
        self.assertEqual(metric(s, 'pe_ttm')[0].threshold, 8)
        self.assertEqual(metric(s, 'pe_ttm')[0].id, pe_id)
        self.assertEqual(s.request.include_industries, ['银行'])
        self.assertEqual(len(s.request.conditions), 2)
        self.assertFalse(s.warnings)
        s = respond(s, '删除波动率条件')
        self.assertEqual(len(s.request.conditions), 1)
        s = respond(s, '撤销上一步')
        self.assertEqual(len(s.request.conditions), 2)
        s = respond(s, '只看半导体')
        self.assertEqual(s.request.include_industries, ['半导体'])
        s = respond(s, '不要半导体')
        self.assertEqual(s.request.include_industries, [])
        self.assertEqual(s.request.exclude_industries, ['半导体'])

    def test_range_update_retains_other_bound(self):
        s = respond(Conversation(), 'PB在1到2之间')
        s = respond(s, 'PB上限改为3以下')
        self.assertEqual({(c.op, c.threshold) for c in s.request.conditions}, {('>=', 1), ('<=', 3)})
        s = respond(s, '删除PB条件')
        self.assertTrue(s.awaiting)
        self.assertFalse(s.confirmable)
        s = respond(s, '删除PB上限')
        self.assertEqual([(c.op, c.threshold) for c in s.request.conditions], [('>=', 1)])

    def test_pending_is_not_accepted_by_unrelated_update(self):
        s = respond(Conversation(), '银行，波动小，毛利率高')
        self.assertTrue(s.awaiting)
        s = respond(s, '毛利率大于45%')
        self.assertTrue(s.awaiting)
        self.assertEqual(metric(s, 'vol_252', True)[0].threshold, .30)
        s = accept(s)
        self.assertTrue(can_execute(s))
        self.assertEqual(metric(s, 'gross_margin')[0].threshold, 45)

    def test_vague_theme_and_reference_require_answer(self):
        s = respond(Conversation(), '科技股，PE低于20')
        self.assertTrue(s.awaiting)
        self.assertFalse(s.confirmable)
        self.assertIsNone(accept(s).request)
        s = respond(s, '毛利率大于40%')
        self.assertTrue(s.awaiting)
        self.assertFalse(s.confirmable)
        s = respond(s, '只看半导体')
        self.assertTrue(can_execute(s))
        self.assertEqual(s.request.include_industries, ['半导体'])
        old = request_signature(s.request)
        s = respond(s, '再稳一点')
        self.assertTrue(s.awaiting)
        self.assertIsNone(s.pending)
        self.assertEqual(request_signature(s.request), old)
        self.assertEqual(request_signature(dismiss(s).request), old)

    def test_conflicts_block_execution(self):
        s = respond(Conversation(), 'PE大于20，PE小于10')
        self.assertTrue(s.awaiting)
        self.assertFalse(s.confirmable)
        self.assertIsNone(accept(s).request)
        s = respond(s, 'PE上限改为30以下')
        self.assertTrue(can_execute(s))
        self.assertFalse(conflicts(s.request))
        s = respond(Conversation(), 'PE小于0')
        self.assertFalse(can_execute(s))

    def test_no_old_defaults_and_unsupported(self):
        s = respond(Conversation(), '股价低于10，机构重仓')
        self.assertEqual([c.metric for c in s.request.conditions], ['price'])
        self.assertTrue(any('机构重仓' in w for w in s.warnings))
        unknown = respond(Conversation(), '我想要可爱的公司')
        self.assertIsNone(unknown.request)
        self.assertFalse(can_execute(unknown))
        unsupported = respond(Conversation(), '资产负债率低')
        self.assertFalse(can_execute(unsupported))
        self.assertIsNone(unsupported.request)

    def test_model_patch_and_server_only_key(self):
        s = respond(Conversation(), '银行股，PE低于10，市值100亿以上')
        target = metric(s, 'pe_ttm')[0].id
        def completion(base, key, model, messages):
            self.assertEqual(key, 'private-test-key')
            self.assertNotIn(key, json.dumps(messages))
            self.assertIn(target, messages[0]['content'])
            self.assertIn('银行股', json.dumps(messages, ensure_ascii=False))
            return json.dumps({'actions': [{'kind': 'update', 'target_id': target,
                'condition': {'threshold': 8, 'phrase': '改成8', 'confidence': .95}}]})
        with patch('conversation._completion', side_effect=completion) as tool:
            s = respond(s, 'PE改成8', api_key='private-test-key', base_url='https://example.invalid', model='test')
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(s.mode, '模型对话')
        self.assertEqual(metric(s, 'pe_ttm')[0].threshold, 8)
        self.assertEqual(metric(s, 'total_mv_yi')[0].threshold, 100)
        self.assertEqual(s.request.include_industries, ['银行'])
        self.assertNotIn('private-test-key', repr(s))

    def test_bad_model_output_falls_back_without_losing_conditions(self):
        s = respond(Conversation(), '股价低于10')
        original = request_signature(s.request)
        bad = {'actions': [{'kind': 'add', 'condition': dict(metric='debt_ratio', phrase='负债率低', op='<=', threshold=20)}]}
        with patch('conversation._completion', return_value=json.dumps(bad)):
            got = respond(s, '负债率低', api_key='test', base_url='test', model='test')
        self.assertEqual(request_signature(got.request), original)
        bad['actions'][0]['condition']['metric'] = 'pe_ttm'
        with patch('conversation._completion', return_value=json.dumps(bad)):
            got = respond(s, '负债率低', api_key='test', base_url='test', model='test')
        self.assertEqual(request_signature(got.request), original)
        self.assertFalse(any(c.metric=='pe_ttm' for c in got.request.conditions))
        with patch('conversation._completion', side_effect=RuntimeError('private key must not escape')):
            got = respond(s, 'PE小于15', api_key='test', base_url='test', model='test')
        self.assertEqual(len(got.request.conditions), 2)
        self.assertNotIn('private key must not escape', repr(got))

    def test_manual_edit_and_session_isolation(self):
        a = respond(Conversation(), 'PE低于10')
        b = respond(Conversation(), '股价低于5')
        a = respond(a, '毛利率高')
        edited = a.request.model_copy(deep=True)
        edited.conditions[0].threshold = 8.0
        sync_manual(a, edited)
        self.assertFalse(a.awaiting)
        self.assertEqual(metric(a, 'pe_ttm')[0].threshold, 8)
        self.assertEqual([c.metric for c in b.request.conditions], ['price'])
        for _ in range(25): a = respond(a, '当前条件')
        self.assertLessEqual(len(a.messages), 40)
        self.assertEqual(metric(a, 'pe_ttm')[0].threshold, 8)
        cleared = respond(a, '重新开始')
        self.assertIsNone(cleared.request)
        self.assertEqual(metric(a, 'pe_ttm')[0].threshold, 8)

    def test_existing_screen_and_explanation_parity(self):
        s = respond(Conversation(), '银行股，PE低于10')
        s = respond(s, '再加股价低于10')
        original = rule_query('银行股，PE低于10，股价低于10')
        one = run_screen(s.request, DataClient(universe=fixture(), logger=None))
        two = run_screen(original, DataClient(universe=fixture(), logger=None))
        pd.testing.assert_frame_equal(one.rows[['code6','match_score']], two.rows[['code6','match_score']])
        self.assertEqual(one.funnel.remaining.tolist(), two.funnel.remaining.tolist())
        answer = result_reply({'result': one}, '测试样本为什么入选')
        self.assertIn('测试样本', answer)
        self.assertIn('9.00', answer)
        self.assertIn('8.00', answer)

    def test_accepted_suggestion_is_not_asked_again(self):
        s = accept(respond(Conversation(), '波动小'))
        s = respond(s, '毛利率高')
        self.assertEqual(len(s.pending_ids), 1)
        s = respond(s, '45%')
        self.assertTrue(can_execute(s))
        self.assertEqual(metric(s, 'gross_margin')[0].threshold, 45.)
        self.assertEqual(metric(s, 'vol_252')[0].threshold, .30)

    def test_model_explicit_comparator_and_soft_guard(self):
        raw = {'actions':[{'kind':'add','condition':dict(metric='pe_ttm',phrase='PE低于10',op='<=',threshold=100,type='soft',confidence=.99)}]}
        with patch('conversation._completion',return_value=json.dumps(raw)):
            s = respond(Conversation(),'PE低于10',api_key='test',base_url='test',model='test')
        c = s.request.conditions[0]
        self.assertEqual((c.op,c.threshold,c.type),('<',10.,'hard'))
        raw = {'actions':[{'kind':'add','condition':dict(metric='vol_252',phrase='波动小',op='<=',threshold=.30,type='soft',weight=1.,confidence=.99)}]}
        with patch('conversation._completion',return_value=json.dumps(raw)):
            s = respond(Conversation(),'波动小',api_key='test',base_url='test',model='test')
        self.assertTrue(s.awaiting)
        self.assertEqual(s.pending.conditions[0].weight,.8)

    def test_inline_card_manual_proposal_and_followup(self):
        with patch('conversation._completion',side_effect=RuntimeError('规则测试')):
            app=AppTest.from_file('app.py',default_timeout=10).run()
            self.assertFalse(app.radio)
            self.assertFalse(app.text_area)
            app.chat_input[0].set_value('银行股，波动小').run()
            self.assertFalse(app.exception)
            self.assertIsNone(app.session_state.conversation.request)
            self.assertTrue(app.button(key='run').disabled)
            app.button(key='chat_edit').click().run()
            self.assertFalse(app.exception)
            key=next(n.key for n in app.number_input if n.key.endswith('_threshold'))
            app.number_input(key=key).set_value(25.).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state.conversation.pending.conditions[0].threshold,.25)
            self.assertIsNone(app.session_state.conversation.request)
            app.button(key='chat_accept').click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.button(key='run').disabled)
            self.assertEqual(app.session_state.ir['conditions'][0]['threshold'],.25)
            app.chat_input[0].set_value('再加PE小于10').run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.session_state.ir['conditions']),2)
            self.assertEqual(app.session_state.ir['include_industries'],['银行'])
            self.assertFalse(any('条件工作台' in x.value for x in app.markdown))
            app.button(key='chat_edit').click().run()
            key=next(n.key for n in app.number_input if 'pe_ttm' in n.key and n.key.endswith('_threshold'))
            app.number_input(key=key).set_value(8.).run()
            self.assertEqual(metric(app.session_state.conversation,'pe_ttm')[0].threshold,8.)
            app.chat_input[0].set_value('删除波动率条件').run()
            self.assertEqual([c['metric'] for c in app.session_state.ir['conditions']],['pe_ttm'])
            self.assertEqual(app.session_state.ir['conditions'][0]['threshold'],8.)

    def test_streamlit_dialogue_flow(self):
        calls = []
        release, entered = Event(), Event()
        release.set()
        class Client:
            def __init__(self, **kwargs):
                assert kwargs['force_refresh']
                self.stats = FetchStats()
                self.warnings = []
                self.pinned_universe = None
            def build_universe(self):
                if self.pinned_universe is not None: return self.pinned_universe
                calls.append('fetch');entered.set()
                assert release.wait(8)
                return fixture()
            def load_klines(self, *a, **kw): raise AssertionError('不需要日线')
        def completion(*args):
            raise RuntimeError('test uses deterministic fallback')
        with patch('data.DataClient', Client), patch('conversation._completion', side_effect=completion):
            app = AppTest.from_file('app.py', default_timeout=10).run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.chat_input), 1)
            self.assertFalse(app.dataframe)
            self.assertFalse(calls)
            app.chat_input[0].set_value('银行股，PE低于10').run()
            self.assertFalse(app.exception)
            self.assertFalse(app.button(key='run').disabled)
            app.chat_input[0].set_value('再加波动小').run()
            self.assertTrue(app.button(key='run').disabled)
            self.assertFalse(app.button(key='chat_accept').disabled)
            app.button(key='nav_insights').click().run()
            self.assertTrue(any('市场行情概览' in m.value for m in app.markdown))
            app.button(key='nav_workspace').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.button(key='run').disabled)
            app.button(key='chat_dismiss').click().run()
            self.assertFalse(app.button(key='run').disabled)
            self.assertEqual(len(app.session_state.ir['conditions']), 1)
            app.chat_input[0].set_value('加上股价低于10').run()
            self.assertEqual(len(app.session_state.ir['conditions']), 2)
            # No stock fetch until execution.
            self.assertFalse(calls)
            release.clear();entered.clear()
            app.button(key='run').click().run()
            self.assertTrue(entered.wait(2))
            app.button(key='nav_insights').click().run()
            self.assertTrue(any('正在执行筛选' in m.value for m in app.markdown))
            release.set();app.session_state.screening_job._thread.join(8);app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state.screening_job.snapshot()['status'], 'succeeded', app.session_state.screening_job.snapshot()['error'])
            self.assertEqual(calls, ['fetch'])
            self.assertEqual(len(app.session_state.last_screening['result'].rows), 1)
            self.assertTrue(any('市场行情概览' in m.value for m in app.markdown))
            app.button(key='nav_workspace').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any('筛选已完成' in m['content'] for m in app.session_state.conversation.messages))
            app.button(key='new_conversation').click().run()
            self.assertFalse(app.exception)
            self.assertIsNone(app.session_state.conversation.request)
            self.assertEqual(app.session_state.conversation.messages, [])
            other = AppTest.from_file('app.py').run()
            self.assertIsNone(other.session_state.conversation.request)
            self.assertNotIn('last_screening', other.session_state)
            self.assertEqual(calls, ['fetch'])


if __name__ == '__main__':
    main(verbosity=2)
