"""Deterministic audit regressions. No cloud or model requests."""
import io
import json
import os
import tempfile
import unittest
import urllib.request
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from llm_backend_toolkit.backends import BackendRegistry
from llm_backend_toolkit.cli import main
from llm_backend_toolkit.errors import ProviderCallError
from llm_backend_toolkit.jobs import JobStore
from llm_backend_toolkit.providers import OpenAIChatProvider, OllamaProvider
from llm_backend_toolkit.toolkit import Toolkit
from llm_backend_toolkit.transport import RejectRedirects, normalize_endpoint, open_response


def registry(vision=False):
    return BackendRegistry.from_dict({
        "schema": "llm-backend-toolkit.backends.v1", "default_backend": "local",
        "backends": {
            "local": {"adapter": "ollama", "model": "fixture-model", "cloud": False,
                      "supports_vision": vision, "base_url_env": "LLM_TOOLKIT_OLLAMA_BASE_URL", "base_url_default": "http://127.0.0.1:32100"},
            "optional": {"adapter": "openai-chat", "model": "cloud-fixture", "cloud": True,
                         "base_url_env": "AUDIT_OPTIONAL_ENDPOINT",
                         "base_url_default": "https://provider.invalid", "api_key_env": "AUDIT_MISSING_KEY"}}})


class AuditJobTests(unittest.TestCase):
    def test_poll_reloads_under_completion_lock_and_never_overwrites_terminal(self):
        for terminal in ("completed", "failed", "cancelled"):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as temp:
                store = JobStore(temp, registry=registry(), spawner=lambda *_: None)
                job = store.submit({"task": {"goal": "fixture"}})["job_id"]
                original_lock = store._job_lock
                fired = []
                @contextmanager
                def interleaving(job_id, *, fired=fired, original_lock=original_lock,
                                 store=store, terminal=terminal):
                    if not fired:
                        fired.append(True)
                        with original_lock(job_id):
                            state = store._read_state(job_id)
                            state.update(job_status=terminal, result_status=terminal,
                                         worker_phase=terminal, poll_count=91)
                            store._input_lifecycle.persist_state_locked(job_id, state)
                    with original_lock(job_id):
                        yield
                with patch.object(store, '_job_lock', interleaving):
                    observed = store.get(job)
                self.assertTrue(fired)
                self.assertEqual(terminal, observed['job_status'])
                self.assertEqual(terminal, store._read_state(job)['job_status'])
                self.assertEqual(91, store._read_state(job)['poll_count'])

    def test_inspect_and_list_do_not_touch_state_or_maintenance(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(temp, registry=registry(), spawner=lambda *_: None)
            job = store.submit({'task': {'goal': 'DO_NOT_DISCLOSE_FIXTURE'}})['job_id']
            before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(temp).rglob('*') if p.is_file()}
            with patch.object(store._input_lifecycle, 'recover_if_dead', side_effect=AssertionError('no recovery')), patch.object(store, 'cleanup_inputs', side_effect=AssertionError('no cleanup')), patch.object(store, '_job_lock', side_effect=AssertionError('no lock')):
                receipt = store.inspect(job)
                listing = store.list_jobs()
            self.assertEqual('zero_write', receipt['write_mode'])
            self.assertNotIn('DO_NOT_DISCLOSE_FIXTURE', json.dumps(listing))
            self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(temp).rglob('*') if p.is_file()})

    def test_list_missing_store_does_not_create_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'absent'
            self.assertEqual([], JobStore(root).list_jobs()['jobs'])
            self.assertFalse(root.exists())

    def test_list_is_paginated_and_corrupt_job_is_local_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(temp, registry=registry(), spawner=lambda *_: None)
            ids = [store.submit({'task': {'goal': str(n)}}, force=True)['job_id'] for n in range(3)]
            (Path(temp) / sorted(ids)[1] / 'state.json').write_text('malformed', encoding='utf-8')
            first = store.list_jobs(limit=2)
            self.assertEqual('unavailable', first['jobs'][1]['job_status'])
            second = store.list_jobs(limit=2, cursor=first['next_cursor'])
            self.assertEqual(1, len(second['jobs']))
            self.assertIsNone(second['next_cursor'])
            for limit in (0, 201, True):
                with self.assertRaises(ValueError):
                    store.list_jobs(limit=limit)

    def test_cancel_cli_cancels_queued_job_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(temp, registry=registry(), spawner=lambda *_: None)
            job = store.submit({'task': {'goal': 'fixture'}})['job_id']
            for _ in range(2):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main(['cancel', '--state-dir', temp, '--id', job])
                self.assertEqual(0, code)
                self.assertEqual('cancelled', json.loads(out.getvalue())['job_status'])

    def test_running_cancel_is_only_a_request_until_worker_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(temp, registry=registry(), spawner=lambda *_: None)
            job = store.submit({'task': {'goal': 'fixture'}})['job_id']
            store.claim(job)
            self.assertTrue(store.begin_execution(job))
            receipt = store.cancel(job)
            self.assertEqual('cancellation_requested', receipt['job_status'])
            self.assertEqual('accepted', receipt['status'])
            store.complete(job, {'status': 'ok', 'output': 'late result'})
            self.assertEqual('cancelled', store.inspect(job)['job_status'])
            self.assertFalse((Path(temp) / job / 'result.json').exists())

    def test_missing_identity_result_is_not_reused_from_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(temp, registry=registry(), spawner=lambda *_: None)
            request = {'task': {'goal': 'fixture'}}
            first = store.submit(request)
            store.complete(first['job_id'], {'status': 'ok', 'output': 'OK', 'cache_eligible': False})
            self.assertFalse(store.inspect(first['job_id'])['cache_result_eligible'])
            second = store.submit(request)
            self.assertNotEqual('cache_hit', second['status'])

class AuditProviderTests(unittest.TestCase):
    def test_local_endpoint_cannot_silently_become_remote_or_internal(self):
        for endpoint in ('https://remote.invalid', 'http://127.0.0.1:11434', 'http://127.0.0.1:32101', 'http://localhost:32101', 'http://[::1]:32101'):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {'LLM_TOOLKIT_OLLAMA_BASE_URL': endpoint}):
                tool = Toolkit(registry=registry())
                self.assertEqual('ok', tool.catalog()['status'])
                result = tool.preflight({'task': {'goal': 'fixture'}, 'privacy': {'cloud_allowed': False}})
                self.assertEqual('blocked', result['status'])

    def test_registered_local_migration_is_supported(self):
        p = OllamaProvider(base_url='http://localhost:39991', managed_base_url='http://127.0.0.1:39991')
        self.assertEqual('http://127.0.0.1:39991', p.base_url)
        self.assertFalse(p.cloud)

    def test_invalid_endpoint_details_are_not_echoed(self):
        for endpoint in ('https://USER:PASSWORD_SENTINEL@host.invalid', 'https://host.invalid?token=SECRET_SENTINEL', 'https://host.invalid/#SECRET_SENTINEL', 'file:///secret', 'http://bad:INVALID'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError) as raised:
                normalize_endpoint(endpoint)
            self.assertNotIn('SENTINEL', str(raised.exception))

    def test_redirects_are_rejected_for_all_statuses_and_destinations(self):
        request = urllib.request.Request('https://origin.invalid/v1', data=b'public fixture', method='POST')
        request.add_unredirected_header('Authorization', 'Bearer PUBLIC_FIXTURE')
        for code in (301, 302, 303, 307, 308):
            for url in ('https://origin.invalid/v2', 'https://other.invalid', 'https://origin.invalid:444', 'http://other.invalid'):
                with self.subTest(code=code, url=url), self.assertRaises(ProviderCallError) as raised:
                    RejectRedirects().redirect_request(request, None, code, 'redirect', {}, url)
                self.assertEqual('provider_redirect_blocked', raised.exception.error.category)
                self.assertNotIn('PUBLIC_FIXTURE', str(raised.exception.error))

    def test_local_transport_disables_inherited_proxy_without_global_opener_change(self):
        sentinel = object()
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.return_value = sentinel
            result = open_response(urllib.request.Request('http://127.0.0.1:32100/api/chat'), 3)
            self.assertIs(sentinel, result)
            handlers = build.call_args.args
            self.assertTrue(any(isinstance(h, RejectRedirects) for h in handlers))
            self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers))

    def test_visual_capability_is_identical_in_registry_provider_and_preflight(self):
        for vision in (False, True):
            tool = Toolkit(registry=registry(vision))
            resolved, provider = tool._resolve_provider('local')
            self.assertEqual(vision, provider.supports_vision)
            self.assertEqual(vision, resolved.config['supports_vision'])
            result = tool.preflight({'task': {'goal': 'fixture'}, 'media': {'mode': 'native', 'attachments': [{'id': 'image', 'path': 'not-read.png', 'kind': 'image'}]}})
            self.assertEqual('ok' if vision else 'blocked', result['status'])

    def test_unselected_bad_provider_does_not_break_local_route_or_catalog(self):
        with patch.dict(os.environ, {'AUDIT_OPTIONAL_ENDPOINT': 'http://remote.invalid'}):
            tool = Toolkit(registry=registry())
            self.assertEqual('ok', tool.catalog()['status'])
            self.assertEqual('ok', tool.preflight({'task': {'goal': 'fixture'}})['status'])
            self.assertEqual('blocked', tool.preflight({'backend': 'optional', 'task': {'goal': 'fixture'}, 'privacy': {'cloud_allowed': True}})['status'])

    def test_remote_chat_endpoint_cannot_hide_behind_cloud_false(self):
        provider = OpenAIChatProvider(model='fixture-model', base_url='https://remote.invalid', cloud=False)
        tool = Toolkit(registry=registry(), providers={'local': provider})
        self.assertTrue(provider.cloud)
        self.assertEqual('privacy_block', tool.preflight({'task': {'goal': 'fixture'}})['error']['category'])
        self.assertEqual('privacy_block', tool.invoke({'task': {'goal': 'fixture'}})['error']['category'])

    def test_model_identity_is_reported_not_inferred_or_independently_attested(self):
        for model in (None, '', 'fixture-model', 'alias-or-rerouted-model'):
            provider = OpenAIChatProvider(model='fixture-model', base_url='https://provider.invalid', api_key='PUBLIC_FIXTURE')
            payload = {'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]}
            if model is not None:
                payload['model'] = model
            with patch('llm_backend_toolkit.providers._read_json_response', return_value=payload):
                result = Toolkit(registry=registry(), providers={'local': provider}).invoke({'task': {'goal': 'fixture'}, 'privacy': {'cloud_allowed': True}})
            self.assertEqual('ok', result['status'])
            self.assertEqual(model or None, result['provider']['actual'])
            self.assertFalse(result['provider']['independently_verified'])
            self.assertEqual(model == 'fixture-model', result['cache_eligible'])

    def test_nonstring_model_is_a_sanitized_protocol_error(self):
        provider = OpenAIChatProvider(model='fixture-model', base_url='https://provider.invalid', api_key='PUBLIC_FIXTURE')
        payload = {'model': {'value': 'SENSITIVE_SENTINEL'}, 'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]}
        with patch('llm_backend_toolkit.providers._read_json_response', return_value=payload), self.assertRaises(ProviderCallError) as raised:
            provider.invoke('fixture', [], 'on')
        self.assertNotIn('SENSITIVE_SENTINEL', str(raised.exception.error))
