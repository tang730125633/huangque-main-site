"""Full subagent-turn fault injection; all model and CLI calls are mocked."""
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from agent.v4 import subagent, state, protocol


def tool(name, args):
    return NS(content='', tool_calls=[NS(id='test-call', function=NS(
        name=name, arguments=json.dumps(args)))])


class RuntimeTruth(unittest.TestCase):
    def setUp(self):
        self.sid = self.id()
        state.reset(self.sid)
        self.inputs = {'keyword': '教育'}
        self.quote = {'capability': 'collect', 'inputs': self.inputs,
            'inputs_hash': subagent._inputs_hash('collect', self.inputs),
            'quote_token': 'mock-only', 'cost': 1}
        state.save_subagent(self.sid, 'collect', pending_quote=self.quote,
            last_result=protocol.make('needs_approval', '待确认', quote=self.quote))

    def run_turn(self, tail, *, max_steps=14, clock=None, cli_tail=()):
        submit = tool('hq_run', {'capability_id': 'collect', 'inputs': self.inputs, 'confirm': True})
        responses = [{'exit_code': 0, 'data': {'result': {'job_id': 123, 'status': 'queued'}}}, *cli_tail]
        with patch.object(subagent.config, 'LLM_MODE', 'openai'), \
             patch.object(subagent, 'build_system_prompt', return_value='offline test'), \
             patch.object(subagent, 'MAX_STEPS', max_steps), \
             patch.object(subagent.livecaps, 'cost_kind', return_value='server_quote'), \
             patch.object(subagent.livecaps, 'confirmation', return_value=''), \
             patch.object(subagent, 'llm_turn', side_effect=[submit, *tail]), \
             patch.object(subagent.hq_cli, 'run', side_effect=responses) as cli:
            if clock:
                with patch.object(subagent.time, 'monotonic', side_effect=clock):
                    result, _ = subagent.run_subagent_turn(self.sid, 'collect', '确认当前报价')
            else:
                result, _ = subagent.run_subagent_turn(self.sid, 'collect', '确认当前报价')
        self.assertEqual(cli.call_count, len(responses))
        return result

    def assert_receipt(self, result, expected='running'):
        self.assertEqual(result['state'], expected)
        self.assertEqual(result['result']['job_id'], 123)
        self.assertFalse(state.get_subagent(self.sid, 'collect')['pending_quote'])
        self.assertEqual(state.get_subagent(self.sid, 'collect')['last_result'], result)

    def test_text_finish_keeps_queued_job(self):
        self.assert_receipt(self.run_turn([NS(content='已提交任务，稍后查询。', tool_calls=None)]))

    def test_model_exception_keeps_queued_job(self):
        self.assert_receipt(self.run_turn([RuntimeError('injected model failure')]))

    def test_budget_timeout_keeps_queued_job(self):
        self.assert_receipt(self.run_turn([], clock=[0, 0, 999999]))

    def test_step_limit_keeps_queued_job(self):
        self.assert_receipt(self.run_turn([], max_steps=1))

    def test_explicit_finish_cannot_invent_terminal_evidence(self):
        for claimed in ('completed', 'failed', 'cancelled'):
            with self.subTest(state=claimed):
                self.setUp()
                self.assert_receipt(self.run_turn([tool('finish', {
                    'state': claimed, 'summary': '模型自称终态', 'result': {'job_id': 999}})]))

    def test_matching_task_query_can_publish_true_terminal_state(self):
        for status, expected in (('ready', 'completed'), ('error', 'failed'), ('cancelled', 'cancelled')):
            with self.subTest(status=status):
                self.setUp()
                result = self.run_turn([
                    tool('hq_run', {'capability_id': 'task', 'inputs': {'job_id': 123}}),
                    NS(content='本轮状态已查到', tool_calls=None)],
                    cli_tail=[{'exit_code': 0, 'data': {'result': {'job_id': 123, 'status': status}}}])
                self.assert_receipt(result, expected)

    def test_unrelated_task_query_cannot_complete_original_job(self):
        result = self.run_turn([
            tool('hq_run', {'capability_id': 'task', 'inputs': {'job_id': 456}}),
            tool('finish', {'state': 'completed', 'summary': '完成'})],
            cli_tail=[{'exit_code': 0, 'data': {'result': {'job_id': 456, 'status': 'ready'}}}])
        self.assert_receipt(result)

    def test_later_turn_still_has_original_receipt(self):
        self.run_turn([NS(content='稍后查询', tool_calls=None)])
        with patch.object(subagent.config, 'LLM_MODE', 'openai'), \
             patch.object(subagent, 'llm_turn', side_effect=RuntimeError('injected next turn')):
            result, _ = subagent.run_subagent_turn(self.sid, 'collect', '任务怎么样了')
        self.assert_receipt(result)

    def test_same_price_new_input_never_uses_old_description(self):
        import app
        old = dict(self.quote, inputs={'keyword': '美妆'}, quote_token='old-only')
        old['inputs_hash'] = subagent._inputs_hash('collect', old['inputs'])
        result = subagent._finish({'state': 'needs_approval',
            'summary': '搜索美妆，扣 1 点。请确认。', 'quote': old}, self.sid, 'collect')['result']
        public = app._v4_delegations(self.sid)['collect']
        for summary in (result['summary'], public['summary']):
            self.assertIn('教育', summary)
            self.assertNotIn('美妆', summary)
        self.assertEqual(public['quote_id'], subagent.approval_id(self.quote))
        self.assertNotIn('mock-only', str(public))

    def test_legacy_state_projection_also_uses_frozen_input(self):
        import app
        state.save_subagent(self.sid, 'collect', last_result=protocol.make(
            'needs_approval', '搜索美妆', quote={'cost': 1}))
        public = app._v4_delegations(self.sid)['collect']
        self.assertIn('教育', public['summary'])
        self.assertNotIn('美妆', public['summary'])

    def test_terminal_receipt_keeps_real_result_not_model_fabrication(self):
        result = self.run_turn([
            tool('hq_run', {'capability_id': 'task', 'inputs': {'job_id': 123}}),
            tool('finish', {'state': 'completed', 'summary': '完成', 'result': {'url': 'wrong'}})],
            cli_tail=[{'exit_code': 0, 'data': {'result': {'status': 'ready',
                'url': 'https://example.invalid/real.mp4', 'items': ['actual']}}}])
        self.assert_receipt(result, 'completed')
        self.assertEqual(result['result']['url'], 'https://example.invalid/real.mp4')
        self.assertEqual(result['result']['items'], ['actual'])

    def test_next_quote_and_cross_domain_query_preserve_original_receipt(self):
        self.run_turn([NS(content='稍后查询', tool_calls=None)])
        response = {'exit_code': 0, 'data': {'result': {
            'quote_token': 'next-mock', 'confirmation_required': True, 'cost': 3}}}
        with patch.object(subagent.livecaps, 'cost_kind', return_value='server_quote'), \
             patch.object(subagent.hq_cli, 'run', return_value=response):
            subagent._hq_run_with_file('collect-video', {'url': 'https://example.invalid/v'}, False,
                None, None, None, None, state.get_subagent(self.sid, 'collect'), self.sid, 'collect')
        subagent._observe_task_query(self.sid, {'job_id': 123}, {'status': 'ready', 'items': ['actual']})
        sess = state.get_subagent(self.sid, 'collect')
        self.assertEqual(sess['last_result']['state'], 'needs_approval')
        self.assertEqual(sess['pending_quote']['quote_token'], 'next-mock')
        self.assertEqual(sess['last_result']['_runtime_jobs'][0]['state'], 'completed')
        self.assertEqual(sess['last_result']['_runtime_jobs'][0]['output']['items'], ['actual'])

    def test_multiple_submissions_keep_every_unfinished_job(self):
        self.run_turn([NS(content='稍后查询', tool_calls=None)])
        subagent._observe_job(self.sid, 'collect', {'job_id': 124}, submit=True)
        subagent._observe_task_query(self.sid, {'job_id': 124}, {'status': 'ready'})
        immediate = state.get_subagent(self.sid, 'collect')['last_result']
        self.assertEqual(immediate['state'], 'running')
        self.assertEqual(immediate['result']['job_id'], 123)
        result = subagent._save_outcome(self.sid, 'collect', protocol.make('completed', '模型声称全部完成'))
        self.assert_receipt(result)
        self.assertEqual({j['job_id'] for j in result['result']['submitted_jobs']}, {123, 124})

    def test_real_cli_phase_done_contract_completes_with_content(self):
        result = self.run_turn([
            tool('hq_run', {'capability_id': 'task', 'inputs': {'job_id': 123}}),
            NS(content='结果已完成', tool_calls=None)],
            cli_tail=[{'exit_code': 0, 'data': {'result': {
                'phase': 'done', 'kind': 'collect', 'result': {'title': '实际内容'}}}}])
        self.assert_receipt(result, 'completed')
        self.assertEqual(result['result']['result']['title'], '实际内容')

    def test_finalizer_does_not_erase_new_quote_or_another_job(self):
        from agent.v4 import delivery, media
        self.run_turn([NS(content='稍后查询', tool_calls=None)])
        subagent._observe_job(self.sid, 'collect', {'job_id': 124}, submit=True)
        state.save_subagent(self.sid, 'collect', pending_quote=self.quote)
        with patch.object(media, 'extract_image_urls', return_value=[]), \
             patch.object(state, 'persist') as persist, \
             patch.object(delivery.streaming, 'emit'), patch.object(delivery, '_STATUS_BUST'):
            delivery.finalize_collect(self.sid, 123, {'title': '已完成内容'})
        last = state.get_subagent(self.sid, 'collect')['last_result']
        self.assertEqual(last['state'], 'needs_approval')
        self.assertEqual(last['quote']['quote_token'], 'mock-only')
        self.assertEqual({j['result']['job_id']:j['state'] for j in last['_runtime_jobs']},
                         {123:'completed', 124:'running'})
        persist.assert_called_once()

    def test_background_poll_does_not_erase_concurrent_quote(self):
        from agent.v4 import delivery
        self.run_turn([NS(content='稍后查询', tool_calls=None)])
        def reply(*args, **kwargs):
            state.save_subagent(self.sid, 'collect', pending_quote=self.quote)
            return {'exit_code': 0, 'data': {'result': {'phase': 'done', 'result': {'title':'实际内容'}}}}
        with patch.object(delivery.hq_cli, 'run', side_effect=reply), \
             patch.object(state, 'persist'), patch.object(delivery, 'maybe_spawn_finalize'):
            delivery.resume_stale_jobs(self.sid)
        last = state.get_subagent(self.sid, 'collect')['last_result']
        self.assertEqual(last['state'], 'needs_approval')
        self.assertEqual(last['quote']['quote_token'], 'mock-only')
        self.assertEqual(last['_runtime_jobs'][0]['state'], 'completed')

    def test_background_poll_finds_original_job_behind_new_quote(self):
        from agent.v4 import delivery
        self.run_turn([NS(content='稍后查询', tool_calls=None)])
        state.save_subagent(self.sid, 'collect', pending_quote=self.quote)
        subagent._finish({'state':'needs_approval','summary':'新报价'}, self.sid, 'collect')
        with patch.object(delivery.hq_cli, 'run', return_value={'exit_code':0,'data':{'result':{
                'phase':'failed','error':'offline fixture'}}}) as run, \
             patch.object(state, 'persist'):
            delivery.resume_stale_jobs(self.sid)
        run.assert_called_once_with('task', {'job_id':123})
        last = state.get_subagent(self.sid, 'collect')['last_result']
        self.assertEqual(last['state'], 'needs_approval')
        self.assertEqual(last['_runtime_jobs'][0]['state'], 'failed')

    def test_old_running_response_cannot_undo_terminal_but_reconciled_success_can(self):
        self.run_turn([NS(content='稍后查询', tool_calls=None)])
        subagent._observe_task_query(self.sid, {'job_id':123}, {'phase':'error'})
        subagent._observe_task_query(self.sid, {'job_id':123}, {'phase':'running'})
        self.assertEqual(state.get_subagent(self.sid, 'collect')['last_result']['state'], 'failed')
        subagent._observe_task_query(self.sid, {'job_id':123}, {'phase':'done','result':{'title':'补回结果'}})
        last = state.get_subagent(self.sid, 'collect')['last_result']
        self.assertEqual(last['state'], 'completed')
        self.assertEqual(last['result']['result']['title'], '补回结果')

    def test_delivery_job_id_matches_receipt_with_additional_request_id(self):
        state.clear_pending_quote(self.sid, 'collect')
        subagent._observe_job(self.sid, 'collect', {'job_id':123,'request_id':'original-request'}, submit=True)
        subagent._observe_job(self.sid, 'collect', {'job_id':123,'status':'done'}, protocol.COMPLETED)
        last = state.get_subagent(self.sid, 'collect')['last_result']
        self.assertEqual(last['state'], 'completed')
        self.assertEqual(len(last['_runtime_jobs']), 1)
        self.assertEqual(last['result']['request_id'], 'original-request')

    def test_receipt_survives_persist_and_restore(self):
        import tempfile
        self.run_turn([NS(content='稍后查询', tool_calls=None)])
        with tempfile.TemporaryDirectory() as directory, patch.object(state, 'SESSION_DIR', directory):
            state.persist(self.sid)
            state.reset(self.sid)
            self.assertTrue(state.restore(self.sid))
        result = subagent._save_outcome(self.sid, 'collect', protocol.make('failed', '模型失败'))
        self.assert_receipt(result)


if __name__ == '__main__':
    unittest.main()
