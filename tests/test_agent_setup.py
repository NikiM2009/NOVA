import contextlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
import psutil

from nova.agent import AgentSetupError, check_central, register_agent, run_agent


class AgentSetupTests(unittest.TestCase):
    def test_paired_agent_sends_heartbeat_with_windows_compatible_disk_path(self):
        original_client = httpx.Client
        original_disk_usage = psutil.disk_usage
        handlers = {}
        heartbeats = []
        disk_paths = []
        def windows_disk_usage(path):
            # The Windows psutil extension rejects pathlib objects.
            if not isinstance(path,str):
                raise TypeError('argument 1 must be str, not WindowsPath')
            disk_paths.append(path)
            return original_disk_usage(path)
        def handler(request):
            if request.url.path=='/health':
                return httpx.Response(200,json={'status':'ok','demo':False})
            self.assertEqual(request.headers['Authorization'],'Bearer test-agent-key')
            if request.url.path=='/api/agent/claim':
                return httpx.Response(200,json={'job':None})
            if request.url.path=='/api/agent/heartbeat':
                heartbeats.append(json.loads(request.content))
                handlers[signal.SIGTERM](None,None)
                return httpx.Response(200,json={'ok':True})
            self.fail('Unexpected agent request: '+request.url.path)
        def client(**kwargs):
            return original_client(**kwargs,transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as directory:
            connection=Path(directory)/'connection.json'
            saved=dict(id='test-agent',token='test-agent-key',url='http://127.0.0.1:8000')
            connection.write_text(json.dumps(saved),encoding='utf-8')
            with patch('nova.agent.httpx.Client',side_effect=client), \
                 patch('nova.agent.psutil.disk_usage',side_effect=windows_disk_usage), \
                 patch('nova.agent.signal.signal',side_effect=lambda number,callback:handlers.update({number:callback})), \
                 patch('getpass.getpass') as prompt,contextlib.redirect_stdout(io.StringIO()):
                run_agent(saved['url'],directory)
            prompt.assert_not_called()
            self.assertEqual(json.loads(connection.read_text(encoding='utf-8')),saved)
            self.assertEqual(disk_paths,[os.fspath(Path(directory).resolve())])
        self.assertEqual(len(heartbeats),1)
        self.assertEqual(heartbeats[0]['servers'],{})
        self.assertGreaterEqual(heartbeats[0]['metrics']['disk'],0)
        self.assertLessEqual(heartbeats[0]['metrics']['disk'],100)

    def test_refused_connection_does_not_ask_for_code_or_save_credentials(self):
        original_client = httpx.Client
        def handler(request):
            self.assertEqual(request.url.path,'/health')
            raise httpx.ConnectError('[Errno 111] Connection refused',request=request)
        def client(**kwargs):
            return original_client(**kwargs,transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as directory:
            with patch('nova.agent.httpx.Client',side_effect=client), patch('getpass.getpass') as prompt:
                with self.assertRaises(AgentSetupError) as error:
                    run_agent('http://127.0.0.1:8000',directory)
                self.assertIn('nicht erreichbar',str(error.exception))
                self.assertIn('anderen Rechner',str(error.exception))
                prompt.assert_not_called()
                self.assertFalse((Path(directory)/'connection.json').exists())

    def test_demo_and_wrong_service_are_rejected_before_pairing(self):
        replies=[httpx.Response(200,json={'status':'ok','demo':True}),
                 httpx.Response(200,text='<html>Something else</html>'),
                 httpx.Response(200,json={'status':'ok'}),
                 httpx.Response(404,json={'detail':'Not found'})]
        for response in replies:
            with self.subTest(response=response):
                with httpx.Client(base_url='https://nova.example',transport=httpx.MockTransport(lambda _:response)) as client:
                    with self.assertRaises(AgentSetupError):
                        check_central(client,'https://nova.example')

    def test_healthy_central_and_successful_registration(self):
        methods=[]
        def handler(request):
            methods.append((request.method,request.url.path))
            if request.url.path=='/health':
                return httpx.Response(200,json={'status':'ok','demo':False})
            return httpx.Response(200,json={'id':'agent-1','token':'test-agent-key'})
        with httpx.Client(base_url='https://nova.example',transport=httpx.MockTransport(handler)) as client:
            check_central(client,'https://nova.example')
            config=register_agent(client,'https://nova.example/','test-code','Pi3','pi3.local')
            self.assertEqual(config,dict(id='agent-1',token='test-agent-key',url='https://nova.example'))
        self.assertEqual(methods,[('GET','/health'),('POST','/api/agent/register')])

    def test_invalid_code_has_actionable_error_without_echoing_code(self):
        with httpx.Client(base_url='https://nova.example',transport=httpx.MockTransport(lambda _:httpx.Response(401))) as client:
            with self.assertRaises(AgentSetupError) as error:
                register_agent(client,'https://nova.example','test-private-code','Pi3','pi3.local')
            self.assertIn('neuen Code',str(error.exception))
            self.assertNotIn('test-private-code',str(error.exception))

    def test_connection_loss_during_registration_has_clean_error(self):
        def handler(request):
            raise httpx.ConnectError('Connection refused',request=request)
        with httpx.Client(base_url='https://nova.example',transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(AgentSetupError) as error:
                register_agent(client,'https://nova.example','test-private-code','Pi3','pi3.local')
            self.assertIn('Zentrale',str(error.exception))
            self.assertNotIn('test-private-code',str(error.exception))

    def test_cli_setup_failure_has_no_traceback(self):
        from nova.__main__ import main
        output=io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            argv=['nova','agent','--url','http://127.0.0.1:8000','--data',directory]
            with patch.object(sys,'argv',argv),patch('nova.agent.run_agent',side_effect=AgentSetupError('Zentrale nicht erreichbar')):
                with contextlib.redirect_stderr(output),self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(str(error.exception),'NOVA Agent: Zentrale nicht erreichbar')
                self.assertNotIn('Traceback',output.getvalue())

    def test_cli_real_refused_connection_exits_without_prompt_or_traceback(self):
        # Keep the port reserved but not listening, reproducing ECONNREFUSED.
        with tempfile.TemporaryDirectory() as directory,socket.socket() as port:
            port.bind(('127.0.0.1',0))
            url=f'http://127.0.0.1:{port.getsockname()[1]}'
            environment=dict(os.environ)
            environment['NO_PROXY']=environment.get('NO_PROXY','')+',127.0.0.1'
            result=subprocess.run([sys.executable,'-m','nova','agent','--url',url,'--data',directory],
                                  capture_output=True,text=True,timeout=15,env=environment)
            self.assertEqual(result.returncode,1)
            self.assertIn('nicht erreichbar',result.stderr)
            self.assertIn('anderen Rechner',result.stderr)
            self.assertNotIn('Traceback',result.stderr)
            self.assertNotIn('Kopplungscode aus NOVA einfügen',result.stdout+result.stderr)
            self.assertFalse((Path(directory)/'connection.json').exists())

    def test_installer_preserves_child_exit_status_without_second_traceback(self):
        import install
        result=subprocess.CompletedProcess([],1)
        with patch.object(sys,'argv',['install.py','--agent','--url','http://127.0.0.1:8000']), \
             patch('install.subprocess.run',return_value=result) as run, \
             patch('install.venv.create'),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(install.main(),1)
            self.assertFalse(run.call_args.kwargs['check'])


if __name__=='__main__':
    unittest.main()
