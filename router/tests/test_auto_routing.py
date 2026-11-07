"""Adaptive routes keep tool contracts and truthful selection evidence."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from ai_router import auto_routing, candidate_evidence, claude, registry, request_evidence
from ai_router.catalogue import merge
from ai_router.providers import select
from tests.candidate_fixtures import proof
from tests.fakes import FakeUpstream
from tests.harness import post
from tests.model_route_fixtures import RouteHarness, payload, success
from tools.claude_probe_report import identity


class AutoRoutingTests(unittest.TestCase):
    def test_switch_into_auto_ignores_carried_effort_but_fixed_models_stay_strict(self):
        harness = RouteHarness(self)
        fixed = 'deepseek/deepseek-v4.1-flash-1017'
        status, _, _, _ = harness.turn(payload(fixed, 'responses', 'low'),
                                       'responses', [success(fixed, 'responses')])
        self.assertEqual(status, 200)
        for slug in ('openrouter/auto', 'openrouter/auto-code'):
            for effort in ('low', 'medium', 'high', 'xhigh', 'max'):
                with self.subTest(slug=slug, carried_effort=effort):
                    status, _, sends, _ = harness.turn(payload(slug, 'responses', effort),
                        'responses', [success('synthetic/selected-model', 'responses')])
                    self.assertEqual(status, 200)
                    self.assertEqual(len(sends), 1)
                    self.assertNotIn('reasoning', json.loads(sends[0][3]))
        status, _, sends, _ = harness.turn(payload(fixed, 'responses', 'medium'),
                                           'responses', [], sent=False)
        self.assertEqual(status, 400)
        self.assertEqual(sends, [])

    def test_managed_count_tokens_uses_the_same_approved_pool(self):
        harness = RouteHarness(self)
        approved = 'deepseek/deepseek-v4.1-flash-1017'
        for slug in ('openrouter/auto', 'openrouter/auto-code'):
            harness.server.state['allowed_models'] = [slug, approved]
            harness.responses = iter([FakeUpstream(200, b'{"input_tokens":3}', [])])
            harness.sends.clear()
            response = post(harness.server.server_port, '/test-token/v1/messages/count_tokens',
                            {'model': slug, 'messages': [{'role': 'user', 'content': 'Synthetic'}]})
            self.assertEqual(response.status, 200)
            response.read()
            response.close()
            self.assertEqual(json.loads(harness.sends[0][3])['plugins'], [
                {'id': 'auto-router', 'allowed_models': [approved]}])

    def test_managed_routes_restrict_upstream_pool_and_refuse_empty_pool(self):
        harness = RouteHarness(self)
        approved = 'deepseek/deepseek-v4.1-flash-1017'
        for slug in ('openrouter/auto', 'openrouter/auto-code'):
            for protocol in ('responses', 'messages'):
                with self.subTest(slug=slug, protocol=protocol):
                    harness.server.state['allowed_models'] = [slug, approved, 'gpt-6-luna']
                    body = payload(slug, protocol, 'none')
                    body['plugins'] = [{'id': 'auto-router', 'allowed_models': ['*']},
                                       {'id': 'auto-router', 'allowed_models': ['openai/*']}]
                    original = deepcopy(body)
                    status, _, sends, _ = harness.turn(body, protocol, [success(approved, protocol)])
                    self.assertEqual(status, 200)
                    self.assertEqual(body, original)
                    sent = json.loads(sends[0][3])
                    self.assertEqual(sent['plugins'], [
                        {'id': 'auto-router', 'allowed_models': [approved]}])
                    harness.server.state['allowed_models'] = [slug, 'gpt-6-luna']
                    status, _, sends, _ = harness.turn(body, protocol, [], sent=False, projected=False)
                    self.assertEqual(status, 400)
                    self.assertEqual(sends, [])

    def test_both_protocols_keep_tools_and_external_credentials(self):
        for slug in ('openrouter/auto', 'openrouter/auto-code'):
            credentials = SimpleNamespace(key_for=lambda *_: 'external-test-key')
            responses = {'model': slug, 'input': [], 'reasoning': {'effort': 'none'}}
            provider = select(slug, '/responses', [])
            prepared = provider.prepare(slug, '/responses', responses, b'',
                                        {'Authorization': 'Bearer subscription-test'}, credentials)
            self.assertNotIn('reasoning', json.loads(prepared.body))
            self.assertEqual(prepared.headers['Authorization'], 'Bearer external-test-key')
            facts = request_evidence.Attempt(1, 'initial', prepared,
                                             {'model': slug, 'reasoning_effort': 'none'}, 30)
            facts.report('model', 'synthetic/selected-model', 'response.model')
            facts.report('effort', 'high', 'response.reasoning.effort')
            self.assertEqual(facts['model_status'], 'provider_reported_selection')
            self.assertEqual(facts['effort_status'], 'provider_reported_selection')
            tool = {'name': 'echo_probe', 'input_schema': {'type': 'object'}}
            messages = {'model': slug, 'messages': [{'role': 'user', 'content': 'Synthetic'}],
                        'max_tokens': 2048, 'tools': [tool],
                        'output_config': {'effort': 'high'}, 'thinking': {'type': 'adaptive'}}
            provider = claude.select(slug, '/v1/messages')
            prepared = provider.prepare(slug, '/v1/messages', messages,
                                        json.dumps(messages).encode(), {}, credentials)
            body = json.loads(prepared.body)
            self.assertNotIn('output_config', body)
            self.assertNotIn('thinking', body)
            self.assertEqual(body['tools'], [tool])
            self.assertEqual(prepared.headers['Authorization'], 'Bearer external-test-key')

    def test_only_the_two_explicit_routes_are_adaptive(self):
        for slug in ('openrouter/auto', 'openrouter/auto-code'):
            self.assertTrue(auto_routing.is_auto(slug))
        for slug in ('openai/gpt-6-astra', 'openrouter/auto-beta', None):
            self.assertFalse(auto_routing.is_auto(slug))

    def test_automatic_effort_does_not_disable_upstream_reasoning(self):
        for field in ('reasoning', 'output_config'):
            payload = {field: {'effort': 'none'}, 'thinking': {'type': 'disabled'},
                       'model': 'openrouter/auto-code', 'tools': [{'name': 'read'}]}
            original = deepcopy(payload)
            projected = auto_routing.project(payload, payload['model'])
            self.assertNotIn(field, projected)
            self.assertNotIn('thinking', projected)
            self.assertEqual(projected['tools'], payload['tools'])
            self.assertEqual(payload, original)
            self.assertIs(auto_routing.project(payload, 'fixed/model'), payload)

    def test_selected_models_pass_only_for_adaptive_route_evidence(self):
        for slug in ('openrouter/auto', 'openrouter/auto-code'):
            row = registry.by_slug(slug)
            record = proof(row)
            for route in record['routes']:
                route['requested_effort'] = 'medium'  # inherited; the adaptive route omits it
                route['served_models'] = [{'value': 'synthetic/selected-model',
                                           'source': 'response.model'}]
                route['served_efforts'] = [{'value': 'high',
                                            'source': 'response.reasoning.effort'}]
            self.assertEqual(candidate_evidence.problems(row, record), [])
            self.assertEqual(identity(slug, {'served': ['synthetic/selected-model']}),
                             'routed')
        fixed = registry.MODELS[0]
        record = proof(fixed)
        record['routes'][0]['served_models'][0]['value'] = 'synthetic/selected-model'
        self.assertTrue(candidate_evidence.problems(fixed, record))
        self.assertEqual(identity(fixed.slug, {'served': ['synthetic/selected-model']}),
                         'mismatched')

    def test_picker_additions_preserve_subscription_rows_and_show_auto_effort(self):
        native = {'slug': 'gpt-6-astra', 'display_name': 'Native subscription',
                  'priority': 1, 'default_reasoning_level': 'max'}
        result = {row['slug']: row for row in merge({'models': [native]})['models']}
        self.assertEqual(result['gpt-6-astra']['default_reasoning_level'], 'max')
        for slug in ('openrouter/auto', 'openrouter/auto-code'):
            row = result[slug]
            self.assertEqual(row['default_reasoning_level'], 'none')
            self.assertIn('Automatic', row['supported_reasoning_levels'][0]['description'])
            self.assertGreater(row['priority'], result['gpt-6-astra']['priority'])
