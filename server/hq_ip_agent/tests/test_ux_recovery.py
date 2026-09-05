"""Regression cases from the mobile v4 report; no network or paid actions."""
import copy
import concurrent.futures
import unittest
from unittest.mock import patch
from agent import state as profiles
from agent.v4 import main_agent, state, subagent, protocol


class UXRecovery(unittest.TestCase):
    def setUp(self):
        self.sid = self.id()
        state.reset(self.sid)
        profiles.reset(self.sid)

    def quote(self):
        q = {"capability": "collect", "inputs_hash": "frozen-inputs", "quote_token": "test-only-token",
             "inputs": {"keyword": "demo"}, "cost": 1}
        state.save_subagent(self.sid, "collect", pending_quote=q,
                            last_result=protocol.make("needs_approval", "待确认"))
        return subagent.approval_id(q)

    def test_old_profile_survives_history_window_without_rewriting_history(self):
        facts = {"basic.name": "测试用户", "career.current_job": "运营", "style.personality": "随和",
                 "story.comeback": "没有，跳过", "audience.target": "初学者"}
        profiles.update_profile(self.sid, facts)
        messages = [{"role": "system", "content": "system"}]
        for i in range(40):
            messages += [{"role": "user", "content": str(i)}, {"role": "assistant", "content": "reply"}]
        original = copy.deepcopy(messages)
        ctx = main_agent._trim_history(self.sid, messages)
        text = str(ctx)
        for value in facts.values():
            self.assertIn(value, text)
        self.assertIn("不再询问", text)
        self.assertEqual(messages, original)
        self.assertLess(len(ctx), 30)

    def test_profile_snapshot_is_present_on_short_and_seed_only_history(self):
        profiles.update_profile(self.sid, {"business.goal": "获客"})
        for messages in ([], [{"role": "system", "content": "seed"}],
                         [{"role": "system", "content": "seed"}, {"role": "user", "content": "hi"}]):
            self.assertIn("获客", str(main_agent._trim_history(self.sid, messages)))

    def test_stale_card_cannot_confirm_new_quote(self):
        self.quote()
        with patch.object(subagent, "_run_subagent_turn_locked") as run:
            res, _ = subagent.respond_to_approval(self.sid, "collect", "old-quote", "confirm")
            run.assert_not_called()
            self.assertIn("已经更新", res["summary"])

    def test_duplicate_confirmations_continue_once_then_return_original_result(self):
        quote_id = self.quote()
        def finish(*_):
            state.clear_pending_quote(self.sid, "collect")
            result = protocol.make("running", "原任务 123 已提交", result={"job_id": 123})
            state.save_subagent(self.sid, "collect", last_result=result)
            return result, []
        with patch.object(subagent, "_run_subagent_turn_locked", side_effect=finish) as run:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda _: subagent.respond_to_approval(
                    self.sid, "collect", quote_id, "confirm"), range(3)))
            self.assertEqual(run.call_count, 1)
            self.assertTrue(all(r[0]["result"]["job_id"] == 123 for r in results))

    def test_cancel_does_not_invoke_model_or_business_request(self):
        qid = self.quote()
        with patch.object(subagent, "_run_subagent_turn_locked") as run:
            res, _ = subagent.respond_to_approval(self.sid, "collect", qid, "cancel")
            run.assert_not_called()
        self.assertEqual(res["state"], "cancelled")
        self.assertFalse(state.get_subagent(self.sid, "collect")["pending_quote"])

    def test_confirmation_cannot_requote_even_if_model_requests_it(self):
        qid = self.quote()
        def attempt_requote(sid, domain, task):
            sess = state.get_subagent(sid, domain)
            result = subagent._hq_run_with_file('collect', {'keyword': 'demo'}, False,
                None, None, None, None, sess, sid, domain)
            self.assertFalse(result['ok'])
            return protocol.make('needs_approval', result['error']), []
        with patch.object(subagent, '_run_subagent_turn_locked', side_effect=attempt_requote), \
             patch.object(subagent.livecaps, 'cost_kind', return_value='server_quote'), \
             patch.object(subagent.hq_cli, 'run') as execute:
            subagent.respond_to_approval(self.sid, 'collect', qid, 'confirm')
        execute.assert_not_called()
        self.assertIsNone(getattr(subagent._APPROVAL_SCOPE, 'quote', None))

    def test_plain_confirmation_after_completed_collect_does_not_start_again(self):
        state.save_subagent(self.sid, "collect", last_result=protocol.make("completed", "原采集已完成"))
        with patch.object(main_agent, "build_system_prompt", return_value="test"), \
             patch.object(subagent, "llm_turn") as llm:
            reply, _, _ = main_agent.run_turn(self.sid, "确认")
            llm.assert_not_called()
        self.assertIn("不会重复提交", reply)

    def test_http_card_carries_exact_quote_identity_to_background_turn(self):
        import app
        qid = self.quote()
        choice = {"domain": "collect", "quote_id": qid, "decision": "confirm"}
        with patch.object(app, "_spawn_turn", return_value=123) as spawn:
            response = app.app.test_client().post('/api/v4/chat', json={
                "session_id": self.sid, "message": "确认", "approval": choice})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(spawn.call_args.kwargs['approval'], choice)
        public = app._v4_delegations(self.sid)['collect']
        self.assertEqual(public['quote_id'], qid)
        self.assertNotIn('test-only-token', str(public))

    def test_ambiguous_plain_confirmation_does_not_guess_a_quote(self):
        self.quote()
        state.save_subagent(self.sid, 'image', pending_quote={"capability": "image", "quote_token": "other"})
        with patch.object(main_agent, 'build_system_prompt', return_value='test'), \
             patch.object(subagent, 'respond_to_approval') as respond:
            reply, _, _ = main_agent.run_turn(self.sid, '确认')
        respond.assert_not_called()
        self.assertIn('多张', reply)

    def submit_quote(self, response):
        inputs = {'keyword': 'demo'}
        self.quote()
        sess = state.get_subagent(self.sid, 'collect')
        sess['pending_quote']['inputs_hash'] = subagent._inputs_hash('collect', inputs)
        state.save_subagent(self.sid, 'collect', pending_quote=sess['pending_quote'])
        with patch.object(subagent.livecaps, 'cost_kind', return_value='server_quote'), \
             patch.object(subagent.livecaps, 'confirmation', return_value=''), \
             patch.object(subagent.hq_cli, 'run', return_value=response) as execute:
            out = subagent._hq_run_with_file('collect', inputs, True,
                None, None, None, None, sess, self.sid, 'collect')
            execute.assert_called_once()
        return out

    def test_cli_success_consumes_quote_and_publishes_progress_before_model_finish(self):
        import app
        out = self.submit_quote({'exit_code': 0, 'data': {'result': {'job_id': 123}}})
        self.assertTrue(out['ok'])
        sess = state.get_subagent(self.sid, 'collect')
        self.assertFalse(sess['pending_quote'])
        self.assertEqual(sess['last_result']['state'], 'running')
        self.assertEqual(sess['last_result']['result']['job_id'], 123)
        public = app.app.test_client().get('/api/v4/state/' + self.sid).get_json()['collect']
        self.assertEqual(public['state'], 'running')
        self.assertEqual(public['quote_id'], '')
        self.assertNotIn('test-only-token', str(public))

    def test_late_model_finish_cannot_resurrect_consumed_quote(self):
        self.submit_quote({'exit_code': 0, 'data': {'result': {'job_id': 123}}})
        result = subagent._finish({'state': 'needs_approval', 'summary': '请再次确认',
            'quote': {'cost': 1}}, self.sid, 'collect')['result']
        self.assertEqual(result['state'], 'running')
        self.assertEqual(result['result']['job_id'], 123)
        self.assertFalse(result['quote'])

    def test_failed_or_ambiguous_cli_submit_preserves_original_quote(self):
        for response in ({'exit_code': 1, 'stderr': 'timeout'},
                         {'exit_code': 0, 'data': {'error': 'upstream', 'message': 'unknown'}}):
            with self.subTest(response=response):
                self.assertFalse(self.submit_quote(response)['ok'])
                sess = state.get_subagent(self.sid, 'collect')
                self.assertEqual(sess['pending_quote']['quote_token'], 'test-only-token')
                self.assertEqual(sess['last_result']['state'], 'needs_approval')

    def test_public_price_comes_from_current_quote_not_previous_model_summary(self):
        import app
        self.submit_quote({'exit_code': 0, 'data': {'result': {'job_id': 123}}})
        q2 = {'capability': 'collect-video', 'inputs_hash': 'next-inputs',
              'quote_token': 'new-token', 'cost': 3, 'points': 1937, 'expires_in': 300}
        state.save_subagent(self.sid, 'collect', pending_quote=q2,
            last_result=protocol.make('needs_approval', '旧搜索报价 1 点', quote={'cost': 1}))
        public = app._v4_delegations(self.sid)['collect']
        self.assertEqual(public['quote']['cost'], 3)
        self.assertEqual(public['quote_id'], subagent.approval_id(q2))
        self.assertNotIn('new-token', str(public))

    def test_missing_quote_finish_cannot_claim_a_new_approval_exists(self):
        result = subagent._finish({'state': 'needs_approval', 'summary': '请确认'},
                                 self.sid, 'collect')['result']
        self.assertNotEqual(result['state'], 'needs_approval')

    def test_concurrent_clicks_use_real_submit_lifecycle_once(self):
        self.quote()
        q = state.get_subagent(self.sid, 'collect')['pending_quote']
        q['inputs_hash'] = subagent._inputs_hash('collect', q['inputs'])
        state.save_subagent(self.sid, 'collect', pending_quote=q)
        qid = subagent.approval_id(q)
        def submit(sid, domain, task):
            sess = state.get_subagent(sid, domain)
            subagent._hq_run_with_file('collect', q['inputs'], True,
                None, None, None, None, sess, sid, domain)
            return state.get_subagent(sid, domain)['last_result'], []
        with patch.object(subagent, '_run_subagent_turn_locked', side_effect=submit), \
             patch.object(subagent.livecaps, 'cost_kind', return_value='server_quote'), \
             patch.object(subagent.livecaps, 'confirmation', return_value=''), \
             patch.object(subagent.hq_cli, 'run', return_value={
                 'exit_code': 0, 'data': {'result': {'job_id': 123}}}) as execute:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda _: subagent.respond_to_approval(
                    self.sid, 'collect', qid, 'confirm'), range(3)))
        execute.assert_called_once()
        self.assertTrue(all(r[0]['result']['job_id'] == 123 for r in results))

    def test_next_real_quote_can_still_be_confirmed_after_previous_submit(self):
        self.submit_quote({'exit_code': 0, 'data': {'result': {'job_id': 123}}})
        q2 = {'capability': 'collect-video', 'inputs_hash': 'next-inputs',
              'quote_token': 'new-token', 'cost': 3}
        state.save_subagent(self.sid, 'collect', pending_quote=q2)
        result = subagent._finish({'state': 'needs_approval', 'summary': '视频采集报价',
            'quote': {'cost': 1}}, self.sid, 'collect')['result']
        self.assertEqual(result['state'], 'needs_approval')
        self.assertEqual(result['quote']['cost'], 3)

    def test_sync_completion_still_finishes_normally(self):
        self.submit_quote({'exit_code': 0, 'data': {'result': {'items': ['demo']}}})
        result = subagent._finish({'state': 'completed', 'summary': '采集完成',
            'result': {'items': ['demo']}}, self.sid, 'collect')['result']
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['result'], {'items': ['demo']})

    def test_next_quote_is_published_with_its_own_summary_before_model_finish(self):
        import app
        self.quote()
        old = state.get_subagent(self.sid, 'collect')
        with patch.object(subagent.livecaps, 'cost_kind', return_value='server_quote'), \
             patch.object(subagent.hq_cli, 'run', return_value={'exit_code': 0, 'data': {'result': {
                 'quote_token': 'next-token', 'confirmation_required': True, 'cost': 3}}}) as execute:
            subagent._hq_run_with_file('collect-video', {'url': 'test-only'}, False,
                None, None, None, None, old, self.sid, 'collect')
        execute.assert_called_once()
        public = app._v4_delegations(self.sid)['collect']
        self.assertEqual(public['state'], 'needs_approval')
        self.assertEqual(public['quote']['cost'], 3)
        self.assertIn('collect-video', public['summary'])
        self.assertNotIn('next-token', str(public))


if __name__ == "__main__":
    unittest.main()
