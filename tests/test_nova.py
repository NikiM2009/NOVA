import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import httpx

from fastapi.testclient import TestClient

from nova.agent import Engine, FileManager
from nova.app import create_app
from nova import catalog
from nova.locking import instance_lock


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name,admin_password='a-long-test-password')
        self.client = TestClient(self.app)
        self.store = self.app.state.store

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def login(self):
        self.assertEqual(self.client.post('/api/login',json={'password':'a-long-test-password'}).status_code,200)

    def pair(self, name='Atlas'):
        code = self.client.post('/api/pairing').json()['token']
        result = self.client.post('/api/agent/register',json=dict(token=code,name=name,address='10.0.0.11',platform='Linux')).json()
        return result, {'Authorization':'Bearer '+result['token']}

    def install(self, agent):
        result = self.client.post('/api/servers',json=dict(name='Survival',agent_id=agent['id'],software='paper',version='1.21.4',accept_eula=True,port=25566))
        self.assertEqual(result.status_code,200,result.text)
        return result.json()

    def test_auth_cookie_logout_and_origin(self):
        self.assertEqual(self.client.get('/api/overview').status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'password':'wrong'}).status_code,401)
        self.login()
        self.assertTrue(self.client.get('/api/session').json()['authenticated'])
        self.assertNotIn('a-long-test-password',self.store.setting('password'))
        self.assertEqual(self.client.post('/api/pairing',headers={'Origin':'https://attacker.example'}).status_code,403)
        self.assertEqual(self.client.post('/api/logout').status_code,200)
        self.assertEqual(self.client.get('/api/overview').status_code,401)

    def test_pair_code_consumed_once_and_keys_not_exposed(self):
        self.login()
        code=self.client.post('/api/pairing').json()['token']
        payload=dict(token=code,name='Atlas',address='10.0.0.11',platform='Linux')
        response=self.client.post('/api/agent/register',json=payload)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.post('/api/agent/register',json=payload).status_code,401)
        overview=self.client.get('/api/overview').json()
        self.assertNotIn('token',overview['agents'][0])
        self.assertNotEqual(response.json()['token'],self.store.one('SELECT token FROM agents')['token'])

    def test_expired_pairing_and_agent_auth(self):
        self.login()
        code=self.client.post('/api/pairing').json()['token']
        self.store.execute('UPDATE pairing SET expires=0')
        self.assertEqual(self.client.post('/api/agent/register',json=dict(token=code,name='Pi',address='pi.local',platform='Linux')).status_code,401)
        self.assertEqual(self.client.post('/api/agent/claim',headers={'Authorization':'Bearer forged'}).status_code,401)

    def test_install_claim_result_and_heartbeat(self):
        self.login()
        agent,headers=self.pair()
        install=self.install(agent)
        claimed=self.client.post('/api/agent/claim',headers=headers).json()['job']
        self.assertEqual(claimed['id'],install['job_id'])
        self.assertEqual(claimed['payload']['version'],'1.21.4')
        self.assertIsNone(self.client.post('/api/agent/claim',headers=headers).json()['job'])
        other,other_headers=self.pair('Orion')
        result=dict(ok=True,data={'build':232})
        self.assertEqual(self.client.post('/api/agent/jobs/'+claimed['id'],headers=other_headers,json=result).status_code,404)
        self.assertEqual(self.client.post('/api/agent/jobs/'+claimed['id'],headers=headers,json=result).status_code,200)
        self.assertEqual(self.client.get('/api/jobs/'+claimed['id']).json()['status'],'done')
        self.assertEqual(self.client.post('/api/agent/jobs/'+claimed['id'],headers=headers,json=result).status_code,404)
        self.client.post('/api/agent/heartbeat',headers=headers,json=dict(metrics={'cpu':25,'ram_used':4,'ram_total':8},servers={install['server_id']:{'status':'running','metrics':{'players':12,'ram':1.5}}}))
        overview=self.client.get('/api/overview').json()
        self.assertEqual(overview['players'],12)
        self.assertEqual(overview['ram_used'],4)

    def test_stale_agent_hides_metrics_and_blocks_actions(self):
        self.login()
        agent,headers=self.pair()
        install=self.install(agent)
        self.store.execute("UPDATE servers SET status='running',metrics=?", (json.dumps({'players':42}),))
        self.store.execute('UPDATE agents SET last_seen=0')
        overview=self.client.get('/api/overview').json()
        self.assertFalse(overview['agents'][0]['online'])
        self.assertEqual(overview['servers'][0]['status'],'unknown')
        self.assertEqual(overview['players'],0)
        self.store.execute("UPDATE servers SET status='stopped'")
        self.assertEqual(self.client.post(f'/api/servers/{install["server_id"]}/actions/start').status_code,409)

    def test_failed_velocity_install_keeps_original_error_and_can_retry(self):
        self.login()
        agent,headers=self.pair()
        created=self.client.post('/api/servers',json=dict(name='Velocity',agent_id=agent['id'],software='velocity',version='3.4.0',port=25565)).json()
        id_=created['server_id']
        install=self.client.post('/api/agent/claim',headers=headers).json()['job']
        error='Java 21 oder neuer ist erforderlich.'
        self.client.post('/api/agent/jobs/'+install['id'],headers=headers,json=dict(ok=False,error=error))
        state=self.client.get('/api/overview').json()['servers'][0]
        self.assertEqual(state['installation']['error'],error)
        start=self.client.post(f'/api/servers/{id_}/actions/start')
        self.assertEqual(start.status_code,409)
        self.assertIn(error,start.json()['detail'])
        self.assertIsNone(self.client.post('/api/agent/claim',headers=headers).json()['job'])
        retry=self.client.post(f'/api/servers/{id_}/install')
        self.assertEqual(retry.status_code,200)
        self.assertEqual(self.client.post(f'/api/servers/{id_}/install').status_code,409)
        claimed=self.client.post('/api/agent/claim',headers=headers).json()['job']
        self.assertEqual(claimed['server_id'],id_)
        self.assertTrue(claimed['payload']['recover_existing'])
        self.assertEqual(claimed['payload']['software'],'velocity')
        self.client.post('/api/agent/jobs/'+claimed['id'],headers=headers,json=dict(ok=True,data={}))
        self.assertEqual(self.client.post(f'/api/servers/{id_}/actions/start').status_code,200)
        self.assertEqual(len(self.client.get('/api/overview').json()['servers']),1)

    def test_agent_inventory_reports_missing_instance_and_keeps_pending_install(self):
        self.login()
        agent,headers=self.pair()
        created=self.install(agent)
        id_=created['server_id']
        # An empty inventory during an installation is expected.
        body=dict(metrics={'data_dir':'/srv/nova/agent'},servers={})
        self.client.post('/api/agent/heartbeat',headers=headers,json=body)
        self.assertEqual(self.client.get('/api/overview').json()['servers'][0]['status'],'installing')
        claimed=self.client.post('/api/agent/claim',headers=headers).json()['job']
        self.client.post('/api/agent/jobs/'+claimed['id'],headers=headers,json={'ok':True})
        self.client.post('/api/agent/heartbeat',headers=headers,json=body)
        overview=self.client.get('/api/overview').json()
        self.assertEqual(overview['servers'][0]['status'],'missing')
        self.assertEqual(overview['agents'][0]['metrics']['data_dir'],'/srv/nova/agent')
        start=self.client.post(f'/api/servers/{id_}/actions/start')
        self.assertEqual(start.status_code,409)
        self.assertIn('Datenverzeichnis',start.json()['detail'])
        self.assertEqual(self.client.post(f'/api/servers/{id_}/install').status_code,200)
        self.client.post('/api/agent/heartbeat',headers=headers,json=body)
        self.assertEqual(self.client.get('/api/overview').json()['servers'][0]['status'],'installing')

    def test_existing_installation_failure_is_backfilled_and_survives_job_cleanup(self):
        self.login()
        agent,headers=self.pair()
        created=self.install(agent)
        claimed=self.client.post('/api/agent/claim',headers=headers).json()['job']
        self.client.post('/api/agent/jobs/'+claimed['id'],headers=headers,json=dict(ok=False,error='Download nicht erreichbar'))
        self.store.execute('DELETE FROM installations')
        from nova.store import Store
        Store(self.store.path)
        self.store.execute('DELETE FROM jobs')
        Store(self.store.path)
        state=self.client.get('/api/overview').json()['servers'][0]
        self.assertEqual(state['installation']['status'],'failed')
        self.assertEqual(state['installation']['error'],'Download nicht erreichbar')

    def test_validation_and_file_paths(self):
        self.login()
        agent,_=self.pair()
        body=dict(name='Survival',agent_id=agent['id'],software='paper',version='1.21.4',port=25566)
        self.assertEqual(self.client.post('/api/servers',json=body).status_code,400)
        body['accept_eula']=True
        result=self.client.post('/api/servers',json=body).json()
        self.assertEqual(self.client.post('/api/servers',json=body).status_code,409)
        for path in ['../connection.json','/etc/passwd','C:\\Users\\secret','a/../../b']:
            self.assertEqual(self.client.post(f'/api/servers/{result["server_id"]}/files/read',json={'path':path}).status_code,400)

    def test_invalid_telemetry_rejected_and_removal_waits_for_agent(self):
        self.login()
        agent,headers=self.pair()
        install=self.install(agent)
        self.assertEqual(self.client.post('/api/agent/heartbeat',headers=headers,json={'metrics':{'cpu':'broken'}}).status_code,422)
        self.assertEqual(self.client.post('/api/agent/heartbeat',headers=headers,json={'metrics':{'cpu':101}}).status_code,422)
        claimed=self.client.post('/api/agent/claim',headers=headers).json()['job']
        self.client.post('/api/agent/jobs/'+claimed['id'],headers=headers,json={'ok':True})
        result=self.client.delete('/api/servers/'+install['server_id'])
        self.assertEqual(result.status_code,200)
        self.assertIsNotNone(self.store.one('SELECT id FROM servers WHERE id=?',(install['server_id'],)))
        removal=self.client.post('/api/agent/claim',headers=headers).json()['job']
        self.client.post('/api/agent/jobs/'+removal['id'],headers=headers,json={'ok':True})
        self.assertIsNone(self.store.one('SELECT id FROM servers WHERE id=?',(install['server_id'],)))

    def test_network_sync_requires_stopped_and_proxy(self):
        self.login()
        agent,headers=self.pair()
        install=self.install(agent)
        self.assertEqual(self.client.post('/api/network/sync').status_code,409)
        proxy=self.client.post('/api/servers',json=dict(name='Proxy',agent_id=agent['id'],software='velocity',version='3.4.0',port=25565)).json()
        self.assertEqual(self.client.post('/api/network/sync').status_code,409)
        self.store.execute("UPDATE servers SET status='stopped'")
        result=self.client.post('/api/network/sync')
        self.assertEqual(result.status_code,200)
        self.assertEqual(len(result.json()['jobs']),2)
        for id_ in result.json()['jobs']:
            payload=json.loads(self.store.one('SELECT payload FROM jobs WHERE id=?',(id_,))['payload'])
            self.assertEqual(payload['routes'],{'Survival':'10.0.0.11:25566'})
            self.assertGreater(len(payload['secret']),32)

    def test_demo_isolated_with_working_editor_and_routines(self):
        demo=create_app(self.temp.name,demo=True)
        with TestClient(demo) as client:
            self.assertTrue(client.get('/api/session').json()['authenticated'])
            self.assertEqual(len(client.get('/api/overview').json()['servers']),6)
            self.assertEqual(client.post('/api/agent/register',json=dict(token='x',name='X',address='localhost',platform='Linux')).status_code,403)
            result=client.post('/api/servers/s2/files/write',json={'path':'note.txt','content':'Hallo NOVA'}).json()
            self.assertEqual(client.get('/api/jobs/'+result['job_id']).json()['status'],'done')
            read=client.post('/api/servers/s2/files/read',json={'path':'note.txt'}).json()
            self.assertEqual(client.get('/api/jobs/'+read['job_id']).json()['result']['data']['content'],'Hallo NOVA')
            routine=client.post('/api/routines',json=dict(name='Nightly',server_id='s2',action='restart',interval_minutes=1440))
            self.assertEqual(routine.status_code,200)
            self.assertEqual(len(client.get('/api/overview').json()['routines']),1)
            client.delete('/api/routines/'+routine.json()['id'])
            self.assertEqual(client.get('/api/overview').json()['routines'],[])
            self.assertEqual(self.store.all('SELECT * FROM servers'),[])

    def test_due_routine_runs_from_background_scheduler(self):
        demo=create_app(self.temp.name,demo=True)
        with TestClient(demo) as client:
            result=client.post('/api/routines',json=dict(name='Scheduled build start',server_id='s6',action='start',interval_minutes=5))
            routine_id=result.json()['id']
            # Move the persisted schedule into the past rather than waiting five minutes.
            demo.state.store.execute('UPDATE routines SET next_run=? WHERE id=?',(time.time()-1,routine_id))
            deadline=time.monotonic()+14
            while time.monotonic()<deadline:
                status=demo.state.store.one('SELECT status FROM servers WHERE id=?',('s6',))['status']
                if status=='running':
                    break
                time.sleep(.1)
            self.assertEqual(status,'running')
            self.assertGreater(demo.state.store.one('SELECT next_run FROM routines WHERE id=?',(routine_id,))['next_run'],time.time())
            events=client.get('/api/overview').json()['events']
            self.assertTrue(any(e['title']=='Routine ausgeführt' for e in events))


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'server'
        self.root.mkdir()
        self.fs=FileManager(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_editor_upload_directory_and_delete(self):
        self.fs.perform('mkdir',{'path':'config'})
        self.fs.perform('write',{'path':'config/paper.yml','content':'motd: Willkommen 🌌\n'})
        self.assertEqual(self.fs.perform('read',{'path':'config/paper.yml'})['content'],'motd: Willkommen 🌌\n')
        self.assertTrue(self.fs.perform('list',{'path':''})['entries'][0]['directory'])
        raw=b'\x00\xffjar-bytes'
        self.fs.perform('write',{'path':'plugin.jar','content':base64.b64encode(raw).decode(),'encoding':'base64'})
        self.assertEqual((self.root/'plugin.jar').read_bytes(),raw)
        with self.assertRaises(ValueError):
            self.fs.perform('read',{'path':'plugin.jar'})
        with self.assertRaises(OSError):
            self.fs.perform('delete',{'path':'config'})
        self.fs.perform('delete',{'path':'config/paper.yml'})
        self.fs.perform('delete',{'path':'config'})
        self.assertFalse((self.root/'config').exists())

    def test_traversal_symlinks_and_root_protection(self):
        outside=Path(self.temp.name)/'secret'
        outside.write_text('do not read')
        for path in ['../secret','/etc/passwd','C:\\secret','nested/../../secret']:
            with self.assertRaises(ValueError):
                self.fs.perform('read',{'path':path})
        try:
            (self.root/'link').symlink_to(outside)
        except OSError:
            pass
        else:
            with self.assertRaises(ValueError):
                self.fs.perform('read',{'path':'link'})
        for op in ['write','delete','mkdir']:
            with self.assertRaises(ValueError):
                self.fs.perform(op,{'path':'','content':'x'})
        self.assertEqual(outside.read_text(),'do not read')

    def test_oversize_write_preserves_original(self):
        (self.root/'note').write_text('original')
        with self.assertRaises(ValueError):
            self.fs.perform('write',{'path':'note','content':'x'*1_000_001})
        self.assertEqual((self.root/'note').read_text(),'original')

    def test_new_file_does_not_overwrite_existing_content(self):
        self.fs.perform('write',{'path':'note','content':'original','overwrite':False})
        with self.assertRaises(FileExistsError):
            self.fs.perform('write',{'path':'note','content':'','overwrite':False})
        self.assertEqual((self.root/'note').read_text(),'original')

    def test_exclusive_data_directory_lock(self):
        with instance_lock(self.root,'agent'):
            with self.assertRaises(RuntimeError):
                with instance_lock(self.root,'agent'):
                    pass
        with instance_lock(self.root,'agent'):
            pass


class DownloadTests(unittest.TestCase):
    def test_verified_artifact_and_mismatch_preserves_original(self):
        artifact=b'controlled test artifact'
        checksum=hashlib.sha256(artifact).hexdigest()
        client_class=httpx.Client
        def transport(request):
            if request.url.path.endswith('/builds'):
                return httpx.Response(200,json=[{'id':123,'channel':'STABLE','downloads':{'server:default':{'url':'https://fill-data.papermc.io/test.jar','checksums':{'sha256':checksum}}}}])
            return httpx.Response(200,content=artifact)
        def client(**kwargs):
            return client_class(**kwargs,transport=httpx.MockTransport(transport))
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'server.jar'
            with patch('nova.catalog.httpx.Client',side_effect=client):
                self.assertEqual(catalog.download('paper','1.21.4',target),123)
                self.assertEqual(target.read_bytes(),artifact)
                checksum='0'*64
                with self.assertRaises(ValueError):
                    catalog.download('paper','1.21.4',target)
                self.assertEqual(target.read_bytes(),artifact)
                self.assertFalse(target.with_suffix('.download').exists())


@unittest.skipIf(os.name=='nt','POSIX executable fixture; actual engine itself is cross-platform')
class AgentProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        self.java=root/'test-java'
        self.java.write_text('''#!/usr/bin/env python3
import sys
if '-version' in sys.argv:
 print('openjdk version "21.0.6"', file=sys.stderr)
 sys.exit(0)
print('Ready for commands', flush=True)
for line in sys.stdin:
 command=line.strip()
 if command in {'stop','shutdown'}:
  print('Stopped cleanly',flush=True)
  break
 print('Received: '+command,flush=True)
''')
        self.java.chmod(0o700)
        self.engine=Engine(root/'agent',str(self.java))
        self.spec=dict(name='Test',software='paper',version='1.21.4',port=25566,memory=512,accept_eula=True)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def install(self):
        def fixture_download(software,version,target):
            target.write_bytes(b'test fixture, not a real Minecraft jar')
            return 123
        with patch('nova.agent.catalog.download',side_effect=fixture_download):
            return self.engine.execute(dict(server_id='test',kind='install',payload=self.spec))

    def test_install_start_console_stop_backup_and_persistence(self):
        self.assertEqual(self.install()['build'],123)
        self.assertTrue((self.engine.folder('test')/'eula.txt').exists())
        self.engine.start('test')
        self.assertTrue(self.engine.running('test'))
        self.assertEqual(self.engine.snapshot()['test']['status'],'running')
        self.engine.command('test','say Hello NOVA')
        for _ in range(30):
            if any('Received: say Hello NOVA' in line for line in self.engine.logs['test']):
                break
            time.sleep(.02)
        self.assertTrue(any('Received: say Hello NOVA' in line for line in self.engine.logs['test']))
        with self.assertRaises(ValueError):
            self.engine.backup('test')
        self.engine.stop('test')
        self.assertFalse(self.engine.running('test'))
        self.assertEqual(self.engine.snapshot()['test']['status'],'stopped')
        self.assertTrue(any('Stopped cleanly' in line for line in self.engine.logs['test']))
        backup=self.engine.backup('test')
        self.assertTrue(Path(backup['archive']).is_file())
        self.assertGreater(backup['size'],0)
        self.assertIn('test',Engine(self.engine.root,str(self.java)).servers)

    def test_network_configuration_preserves_existing_paper_keys(self):
        self.install()
        root=self.engine.folder('test')
        (root/'config').mkdir()
        (root/'config'/'paper-global.yml').write_text('misc:\n  keep-me: true\n')
        self.engine.configure_network('test',{'secret':'a'*64,'routes':{'Test':'10.0.0.11:25566'}})
        import yaml
        config=yaml.safe_load((root/'config'/'paper-global.yml').read_text())
        self.assertTrue(config['misc']['keep-me'])
        self.assertEqual(config['proxies']['velocity']['secret'],'a'*64)
        self.assertIn('online-mode=false',(root/'server.properties').read_text())
        self.assertTrue((root/'server.properties.nova-before-network').exists())

    def test_network_configuration_rejects_symlink_before_any_write(self):
        self.install()
        root=self.engine.folder('test')
        original=(root/'server.properties').read_text()
        outside=Path(self.temp.name)/'outside'
        outside.mkdir()
        (root/'config').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):
            self.engine.configure_network('test',{'secret':'a'*64,'routes':{'Test':'10.0.0.11:25566'}})
        self.assertEqual((root/'server.properties').read_text(),original)
        self.assertFalse((outside/'paper-global.yml').exists())


if __name__=='__main__':
    unittest.main()
