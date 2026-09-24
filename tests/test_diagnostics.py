import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from llm_backend_toolkit.backends import BackendRegistry
from llm_backend_toolkit.diagnostics import diagnose, aicli_diagnostics
from llm_backend_toolkit.errors import ProviderCallError
from llm_backend_toolkit.providers import provider_from_config
from llm_backend_toolkit.toolkit import Toolkit
from llm_backend_toolkit.transport import open_response


class DiagnosticsTests(unittest.TestCase):
    def test_configuration_diagnostics_never_read_task_or_call_provider(self):
        tool = Toolkit()
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), patch('pathlib.Path.write_text', side_effect=AssertionError('write forbidden')):
            result = diagnose(tool)
        self.assertEqual('zero_write', result['write_mode'])
        for key in ('network_performed','model_invoked','materials_read','job_created'):
            self.assertIs(False, result[key])
        self.assertEqual('not_checked', result['route']['live_acceptance'])
        self.assertEqual('caller_process_only', result['observer_environment']['credential_scope'])
        self.assertRegex(result['registry']['snapshot_sha256'], '^[0-9a-f]{64}$')

    def test_optional_bad_endpoint_is_local_diagnostic_failure(self):
        with patch.dict(os.environ, {'LLM_TOOLKIT_QWEN_BASE_URL': 'http://public-fixture.invalid'}):
            result = diagnose(Toolkit(), 'cloud-qwen-flash')
        self.assertEqual('ok', result['status'])
        self.assertEqual('unavailable', result['route']['state'])
        self.assertEqual('selected_backend_configuration_invalid', result['route']['error'])

    def test_diagnostic_requires_explicit_supported_zero_write_receipt(self):
        for body in ({'schema':'wrong'}, {'schema':'aicli.runtime-diagnostics.v1','write_mode':'write'}, {'schema':'aicli.runtime-diagnostics.v1','write_mode':'zero_write','network_performed':True}):
            with patch('llm_backend_toolkit.diagnostics.AiCliProfileRunner._prefix',return_value=['fixture']), patch('llm_backend_toolkit.diagnostics._bounded_process',side_effect=[(0,json.dumps({'capabilities':{'runtimeDiagnostics':'aicli.runtime-diagnostics.v1'}}),'',1),(0,json.dumps({'diagnostics':body}),'',1)]):
                result = aicli_diagnostics('fixture.ps1')
            self.assertEqual('invalid_diagnostic_receipt',result['state'])

    def test_old_aicli_does_not_receive_an_unsupported_command(self):
        with patch('llm_backend_toolkit.diagnostics.AiCliProfileRunner._prefix',return_value=['fixture']), patch('llm_backend_toolkit.diagnostics._bounded_process',return_value=(0,json.dumps({'capabilities':{}}),'',1)) as command:
            result=aicli_diagnostics('fixture.ps1')
        self.assertEqual('unsupported',result['state'])
        self.assertEqual(1,command.call_count)

    def test_unknown_runtime_and_e2e_survive_valid_install_readback(self):
        body={'schema':'aicli.runtime-diagnostics.v1','write_mode':'zero_write','network_performed':False,'model_invoked':False,'credentials_read':False,'desktop':{'installation_state':'verified','end_to_end':'unknown','running_process_loaded':'unknown'}}
        with patch('llm_backend_toolkit.diagnostics.AiCliProfileRunner._prefix',return_value=['fixture']), patch('llm_backend_toolkit.diagnostics._bounded_process',side_effect=[(0,json.dumps({'capabilities':{'runtimeDiagnostics':'aicli.runtime-diagnostics.v1'}}),'',1),(0,json.dumps({'diagnostics':body}),'',1)]):
            result=aicli_diagnostics('fixture.ps1')
        self.assertEqual('observed',result['state'])
        self.assertEqual('unknown',result['receipt']['desktop']['end_to_end'])

    def test_current_deepseek_vision_payload_preserves_media_and_legacy_route(self):
        registry=BackendRegistry.load()
        old=registry.resolve('deepseek-v4-flash')
        current=registry.resolve('deepseek-flash')
        self.assertEqual(old.backend_id,current.backend_id)
        self.assertEqual('deepseek-flash',current.config['model'])
        self.assertTrue(current.config['supports_vision'])
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'DEEPSEEK_API_KEY':'PUBLIC_PROTOCOL_FIXTURE'}):
            image=Path(temp)/'image.png'
            image.write_bytes(b'\x89PNG\r\n\x1a\nPUBLIC_FIXTURE')
            provider=provider_from_config(current.config)
            def response(req,timeout):
                payload=json.loads(req.data)
                self.assertEqual('deepseek-flash',payload['model'])
                self.assertTrue(payload['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
                self.assertNotIn('Authorization',req.headers)
                self.assertEqual('Bearer PUBLIC_PROTOCOL_FIXTURE',req.get_header('Authorization'))
                return {'model':'deepseek-flash','choices':[{'message':{'content':'OK'},'finish_reason':'stop'}]}
            with patch('llm_backend_toolkit.providers._read_json_response',side_effect=response):
                value=provider.invoke('PUBLIC_FIXTURE',[str(image)],'on')
        self.assertEqual('deepseek-flash',value.model)

    def test_real_local_http_redirect_handler_never_follows_redirect(self):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.path)
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                code=int(self.path.strip('/'))
                self.send_response(code)
                self.send_header('Location','/target')
                self.end_headers()
            def do_GET(self):
                seen.append(self.path)
                self.send_response(200); self.end_headers()
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            for code in (301,302,303,307,308):
                req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/{code}',data=b'PUBLIC_FIXTURE',method='POST')
                req.add_unredirected_header('Authorization','Bearer PUBLIC_FIXTURE')
                with self.assertRaises(ProviderCallError):
                    open_response(req,3)
            self.assertNotIn('/target',seen)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)
