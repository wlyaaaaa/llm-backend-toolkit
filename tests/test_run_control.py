import json
import tempfile
import threading
import unittest
from pathlib import Path
from llm_backend_toolkit.jobs import JobStore
from llm_backend_toolkit.run_control import CooperativeRunControl, runtime_cleanup_pending


class NativeControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(self.temp.name, spawner=lambda *_: None)
        self.job = self.store.submit({'task': {'goal': 'PUBLIC_FIXTURE'}})['job_id']
        self.store.claim(self.job)
        self.store.begin_execution(self.job)
        self.context = self.store.runtime_control_context(self.job)
        self.expected = {'profile': 'fixture-profile', 'model': 'fixture-model', 'workspace': str(Path(self.temp.name).resolve())}
        self.id = 'a' * 32
        self.path = self.context['path']
        self.metadata = {'schema': 'aicli.run-control.v1', 'run_id': self.id, 'profile_id': 'fixture-profile',
                         'model': 'fixture-model', 'workspace': self.expected['workspace']}
        self.path.write_text(json.dumps(self.metadata), encoding='utf-8')
        self.control = CooperativeRunControl(self.path, self.expected, self.context)
        self.addCleanup(self.control.close)

    def envelope(self, *, cleanup=True, gpu=True):
        return {'run': {'recoveryRunId': self.id, 'limitUsage': {'cleanupConfirmed': cleanup},
                        'localGpuBrokerSessionSummary': {'state': 'released' if gpu else 'active', 'activeRequests': 0}},
                'recovery': {'runId': self.id, 'status': 'aborted', 'attempts': 1}}

    def test_one_exact_native_abort_then_verified_process_and_gpu_cleanup(self):
        called = threading.Event()
        ids = []
        def abort(run):
            ids.append(run); called.set()
            return {'recovery': {'runId': run, 'status': 'abort_requested'}}
        self.control.start(abort)
        self.assertEqual('accepted', self.store.cancel(self.job)['status'])
        self.assertTrue(called.wait(3))
        self.control.close()
        self.assertEqual([self.id], ids)
        self.assertEqual('cancellation_requested', self.store.inspect(self.job)['job_status'])
        self.store.complete(self.job, {'status': 'ok', 'output': 'must_not_be_published'})
        self.assertEqual('cancellation_requested', self.store.inspect(self.job)['job_status'])
        self.control.observe(self.envelope(), requires_gpu=True)
        self.store.complete(self.job, {'status': 'failed'})
        self.assertEqual('cancelled', self.store.inspect(self.job)['job_status'])
        self.assertFalse((self.path.parent / 'result.json').exists())
        self.assertTrue(self.store.inspect(self.job)['runtime_control']['cleanup_confirmed'])

    def test_incomplete_cleanup_never_promotes_cancellation(self):
        self.control.start(lambda _: {})
        self.control.close()
        self.store.cancel(self.job)
        for cleanup, gpu in ((False, True), (True, False), ('true', True)):
            self.control.observe(self.envelope(cleanup=cleanup, gpu=gpu), requires_gpu=True)
            self.store.complete(self.job, {'status': 'failed'})
            result = self.store.inspect(self.job)
            self.assertEqual('cancellation_requested', result['job_status'])
            self.assertTrue(runtime_cleanup_pending(self.store._read_state(self.job)))

    def test_wrong_model_never_dispatches_to_a_different_run(self):
        self.metadata['model'] = 'different-model'
        self.path.write_text(json.dumps(self.metadata), encoding='utf-8')
        calls = []
        reported = threading.Event()
        publish = self.context['publish']
        self.context['publish'] = lambda value: (publish(value), reported.set())
        self.control.start(lambda run: calls.append(run))
        self.store.cancel(self.job)
        self.assertTrue(reported.wait(3))
        self.control.close()
        self.assertEqual([], calls)
        self.assertEqual('unconfirmed', self.store.inspect(self.job)['runtime_control']['abort_delivery'])

    def test_ambiguous_abort_response_is_not_retried(self):
        calls = []
        called = threading.Event()
        def failed(run):
            calls.append(run); called.set()
            raise TimeoutError('fixture')
        self.control.start(failed)
        self.store.cancel(self.job)
        self.assertTrue(called.wait(3))
        self.control.close()
        self.store.cancel(self.job)
        self.assertEqual([self.id], calls)
        self.assertEqual('unconfirmed', self.store.inspect(self.job)['runtime_control']['abort_delivery'])

    def test_cloud_run_requires_process_cleanup_without_claiming_gpu_use(self):
        self.control.start(lambda _: {})
        self.control.close()
        self.control.observe(self.envelope(gpu=False), requires_gpu=False)
        self.assertTrue(self.store.inspect(self.job)['runtime_control']['cleanup_confirmed'])
        self.assertEqual('not_applicable', self.store.inspect(self.job)['runtime_control']['gpu_lease'])

    def test_changed_control_run_id_cannot_confirm_cleanup(self):
        self.control.start(lambda _: {})
        self.control.close()
        envelope = self.envelope()
        envelope['run']['recoveryRunId'] = 'b' * 32
        self.control.observe(envelope, requires_gpu=True)
        self.assertFalse(self.store.inspect(self.job)['runtime_control']['cleanup_confirmed'])

    def test_failed_execution_without_cleanup_is_not_a_terminal_result(self):
        self.control.start(lambda _: {})
        self.control.close()
        self.store.complete(self.job, {'status': 'failed', 'output': 'must_not_publish'})
        observed=self.store.inspect(self.job)
        self.assertEqual('cleanup_unconfirmed',observed['job_status'])
        self.assertEqual('runtime_cleanup_unconfirmed',observed['error']['category'])
        self.assertFalse(observed['cache_result_eligible'])
        self.assertFalse((self.path.parent/'result.json').exists())
        self.assertEqual(self.id,observed['runtime_control']['run_id'])
        self.assertEqual('blocked',self.store.cleanup_inputs(self.job)['status'])
