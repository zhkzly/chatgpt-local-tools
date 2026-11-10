"""Local fixture tests; no accounts, real tunnel, or external network access."""
import contextlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

MODULE = Path(os.environ.get('CONNECT_SCRIPT', str(Path(__file__).resolve().parents[1] / 'skills/connect-local-tools/scripts/connect.py')))
spec = importlib.util.spec_from_file_location('connect', MODULE)
connect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connect)

STDIO_SERVER = r'''
import json, os, sys
from pathlib import Path
for line in sys.stdin:
    x=json.loads(line)
    if os.environ.get('EVENTS'):
        with open(os.environ['EVENTS'],'a') as f:
            f.write(json.dumps({'method':x.get('method'),'argv':sys.argv[1:],'runtime_key_leaked':'OPENAI_TUNNEL_KEY' in os.environ,'unlisted_key_leaked':'OTHER_SECRET' in os.environ,'app_token_ok':os.environ.get('APP_TOKEN')=='fixture-secret'})+'\n')
    if 'id' not in x:
        continue
    if x['method']=='initialize':
        result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
    elif x['method']=='tools/list':
        result={'tools':[{'name':'fixture_search','inputSchema':{'type':'object'}}]}
    else:
        print(json.dumps({'jsonrpc':'2.0','id':x['id'],'error':{'code':-32601,'message':'unexpected'}}),flush=True)
        continue
    print(json.dumps({'jsonrpc':'2.0','id':x['id'],'result':result}),flush=True)
'''

FAKE_CLIENT = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root=Path(os.environ['TUNNEL_CLIENT_STATE_DIR']); root.mkdir(parents=True,exist_ok=True)
p=root/'fixture-state.json'
state=json.loads(p.read_text()) if p.exists() else {}
a=sys.argv[1:]
with (root/'calls.jsonl').open('a') as f:
    f.write(json.dumps({'argv':a,'headers':os.environ.get('MCP_EXTRA_HEADERS'),'discovery_headers':os.environ.get('MCP_DISCOVERY_EXTRA_HEADERS'),'key_present':'OPENAI_TUNNEL_KEY' in os.environ})+'\n')
assert a[0]=='runtimes'
action=a[1]
if action=='list':
    print(json.dumps({'aliases':list(state.values()),'state_root':str(root)}))
elif action=='connect':
    alias=a[a.index('--alias')+1]
    tunnel=a[a.index('--tunnel-id')+1]
    ref=a[a.index('--runtime-api-key')+1]
    assert ref=='env:OPENAI_TUNNEL_KEY'
    profile=Path(os.environ['TUNNEL_CLIENT_PROFILE_DIR'])/(alias+'.yaml'); profile.parent.mkdir(parents=True,exist_ok=True)
    profile.write_text('control_plane:\n  api_key: env:OPENAI_TUNNEL_KEY\n')
    state[alias]={'alias':alias,'tunnel_id':tunnel,'process_running':True,'healthy':True,'ready':True,'profile_path':str(profile),'health_url':'http://127.0.0.1:12345','logs':'not-for-output'}
    p.write_text(json.dumps(state)); print(json.dumps(state[alias]))
elif action=='status':
    if a[2] not in state: sys.exit(2)
    print(json.dumps(state[a[2]]))
elif action=='stop':
    state[a[2]]['process_running']=False;state[a[2]]['ready']=False
    p.write_text(json.dumps(state)); print(json.dumps(state[a[2]]))
else:
    sys.exit(9)
'''

class Handler(http.server.BaseHTTPRequestHandler):
    events = []
    def log_message(self,*args): pass
    def do_DELETE(self):
        self.__class__.events.append({'method':'DELETE','session':self.headers.get('Mcp-Session-Id')})
        self.send_response(200);self.end_headers()
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.__class__.events.append({'method':payload.get('method'),'session':self.headers.get('Mcp-Session-Id'),'protocol':self.headers.get('MCP-Protocol-Version'),'auth':self.headers.get('Authorization')})
        if self.path=='/slow-headers':
            try:
                self.wfile.write(b'HTTP/1.0 200 OK\r\nX-Trace: ');self.wfile.flush()
                for _ in range(100):
                    self.wfile.write(b'x');self.wfile.flush();time.sleep(0.06)
                self.wfile.write(b'\r\n\r\n')
            except (BrokenPipeError,ConnectionResetError): pass
            return
        if self.path=='/redirect':
            self.send_response(307); self.send_header('Location','http://127.0.0.1:9/steal');self.end_headers();return
        if self.path=='/forbidden':
            self.send_response(403);self.end_headers();self.wfile.write(b'super-secret-in-remote-error');return
        if payload['method']=='notifications/initialized':
            self.send_response(202);self.end_headers();return
        if payload['method']=='initialize':
            result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'http-fixture','version':'1'}}
        else:
            result={'tools':[{'name':'http_search','inputSchema':{'type':'object'}}]}
        body=json.dumps({'jsonrpc':'2.0','id':payload['id'],'result':result}).encode()
        if self.path in ('/slow-body','/slow-sse'):
            self.send_response(200)
            self.send_header('Content-Type','application/json' if self.path=='/slow-body' else 'text/event-stream')
            self.end_headers()
            if self.path=='/slow-sse':body=b'data: '+body+b'\n\n'
            try:
                for byte in body:
                    self.wfile.write(bytes([byte]));self.wfile.flush();time.sleep(0.06)
            except (BrokenPipeError,ConnectionResetError):pass
            return
        self.send_response(200)
        self.send_header('Mcp-Session-Id','fixture-session')
        if self.path=='/sse':
            self.send_header('Content-Type','text/event-stream');self.end_headers()
            self.wfile.write(b': keepalive\n\ndata: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\n\ndata: '+body+b'\n\n')
        else:
            self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)

@contextlib.contextmanager
def http_fixture():
    Handler.events=[]
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try: yield 'http://127.0.0.1:'+str(server.server_port)
    finally: server.shutdown();server.server_close();worker.join()

class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.fixture=self.root/'server with spaces.py';self.fixture.write_text(STDIO_SERVER)
        self.config_path=self.root/'config file.json'
        self.server={'transport':'stdio','command':sys.executable,'args':[str(self.fixture),'two words','$(never-executed)'],'env':{'EVENTS':str(self.root/'events.jsonl')},'env_from':{'APP_TOKEN':'APP_TOKEN_SOURCE'},'tunnel_id':'tunnel_'+'a'*32}
        self.config={'version':1,'servers':{'fixture':self.server}}
        self.config_path.write_text(json.dumps(self.config))
        self.environment=mock.patch.dict(os.environ,{'APP_TOKEN_SOURCE':'fixture-secret','OPENAI_TUNNEL_KEY':'runtime-secret','OTHER_SECRET':'unlisted-secret'})
        self.environment.start();self.addCleanup(self.environment.stop)
    def test_stdio_probe_does_not_call_business_tools_and_limits_environment(self):
        result=connect.probe(self.server,self.config_path)
        self.assertEqual(result['tools'],['fixture_search'])
        self.assertFalse(result['business_tools_called'])
        events=[json.loads(s) for s in (self.root/'events.jsonl').read_text().splitlines()]
        self.assertEqual([e['method'] for e in events],['initialize','notifications/initialized','tools/list'])
        self.assertTrue(all(e['argv']==['two words','$(never-executed)'] for e in events))
        self.assertTrue(all(not e['runtime_key_leaked'] and not e['unlisted_key_leaked'] and e['app_token_ok'] for e in events))
    def test_internal_stdio_exec_preserves_protocol(self):
        req=json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{}})+'\n'
        result=subprocess.run([sys.executable,str(MODULE),'_stdio','--config',str(self.config_path),'--server','fixture'],input=req,text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['id'],1)
        self.assertNotIn('runtime-secret',result.stdout+result.stderr)
    def test_unknown_fields_and_plaintext_credentials_rejected(self):
        self.server['env']['API_TOKEN']='do-not-write'
        with self.assertRaises(connect.SetupError): connect.validate_config(self.config)
        del self.server['env']['API_TOKEN'];self.server['typo']='value'
        with self.assertRaises(connect.SetupError): connect.validate_config(self.config)
    def test_runtime_key_cannot_be_forwarded(self):
        self.server['env_from']={'APP_TOKEN':'OPENAI_TUNNEL_KEY'}
        with self.assertRaises(connect.SetupError): connect.validate_config(self.config)
    def test_runtime_key_reserved_names_rejected_at_both_layers(self):
        for name in ('HTTP_PROXY','hTTP_PROXY','PATH','LC_ALL','MCP_EXTRA_HEADERS','PYTHONPATH','CONTROL_PLANE_API_KEY'):
            with self.subTest(name=name):
                server=dict(self.server,runtime_key_env=name)
                with self.assertRaises(connect.SetupError):
                    connect.validate_config({'version':1,'servers':{'fixture':server}})
                with self.assertRaises(connect.SetupError):
                    connect.process_environment(server,self.config_path)
                with self.assertRaises(connect.SetupError):
                    connect.native_environment(self.root,server,'runtime-secret')
    def test_import_rejects_inline_credential_flags_and_urls_without_echo(self):
        cases=[['--api-key','sensitive-fixture'],['--api-key=sensitive-fixture'],['--token','sensitive-fixture'],['--password=sensitive-fixture'],['--authorization','sensitive-fixture'],['--endpoint','https://user:sensitive-fixture@example.com/mcp'],['--endpoint=https://example.com/mcp?api_key=sensitive-fixture'],['https://example.com/mcp?access_token=sensitive-fixture'],['-H','Authorization: Bearer sensitive-fixture'],['postgresql://user:sensitive-fixture@example.com/database']]
        for args in cases:
            with self.subTest(args=args):
                source=self.root/'inline.json';output=self.root/'rejected.json'
                source.write_text(json.dumps({'mcpServers':{'fixture':{'command':'node','args':args}}}))
                with self.assertRaises(connect.SetupError) as caught:
                    connect.import_mcp(source,output)
                self.assertNotIn('sensitive-fixture',str(caught.exception))
                self.assertFalse(output.exists())
    def test_import_strips_secret_values_and_refuses_overwrite(self):
        original=self.root/'old.json';output=self.root/'imported.json'
        original.write_text(json.dumps({'mcpServers':{'foo':{'command':'node','args':['server.js'],'env':{'API_TOKEN':'literal-secret','BASE_URL':'http://127.0.0.1:1234','CUSTOM':'${SOURCE_ENV}'}}}}))
        result=connect.import_mcp(original,output)
        data=json.loads(output.read_text())
        self.assertNotIn('literal-secret',output.read_text()+json.dumps(result))
        self.assertEqual(data['servers']['foo']['env_from'],{'API_TOKEN':'API_TOKEN','BASE_URL':'BASE_URL','CUSTOM':'SOURCE_ENV'})
        self.assertEqual(data['servers']['foo']['cwd'],str(self.root))
        self.assertEqual(output.stat().st_mode&0o777,0o600)
        with self.assertRaises(connect.SetupError):connect.import_mcp(original,output)
    def test_inspect_does_not_execute(self):
        result=connect.inspect_config(self.config)
        self.assertFalse((self.root/'events.jsonl').exists())
        self.assertEqual(result['servers']['fixture']['required_environment'],['APP_TOKEN_SOURCE'])
    def test_http_json_sse_auth_session_and_cleanup(self):
        with mock.patch.dict(os.environ,{'FIXTURE_AUTH':'Bearer test-token','HTTP_PROXY':'http://127.0.0.1:9','http_proxy':'http://127.0.0.1:9'}),http_fixture() as base:
            for path in ('/mcp','/sse'):
                Handler.events=[]
                result=connect.probe({'transport':'http','url':base+path,'headers_from_env':{'Authorization':'FIXTURE_AUTH'}},self.config_path)
                self.assertEqual(result['tools'],['http_search'])
                self.assertEqual([x['method'] for x in Handler.events],['initialize','notifications/initialized','tools/list','DELETE'])
                self.assertEqual(Handler.events[2]['session'],'fixture-session')
                self.assertEqual(Handler.events[2]['protocol'],'2024-11-05')
                self.assertEqual(Handler.events[2]['auth'],'Bearer test-token')
    def test_http_redirect_and_error_body_not_exposed(self):
        with http_fixture() as base:
            for suffix in ('/redirect','/forbidden'):
                with self.assertRaises(connect.SetupError) as caught:
                    connect.probe({'transport':'http','url':base+suffix},self.config_path)
                self.assertNotIn('super-secret',str(caught.exception))
            self.assertEqual(len(Handler.events),2)
    def test_http_wall_clock_timeout_covers_trickled_headers_and_bodies(self):
        with http_fixture() as base:
            for suffix in ('/slow-headers','/slow-body','/slow-sse'):
                with self.subTest(suffix=suffix):
                    began=time.monotonic()
                    with self.assertRaisesRegex(connect.SetupError,'wall-clock timeout'):
                        connect.probe({'transport':'http','url':base+suffix,'timeout_seconds':1},self.config_path)
                    self.assertLess(time.monotonic()-began,2.5)
    def test_relative_path_is_resolved_in_target_cwd_before_execution(self):
        launcher=self.root/'launcher';target=self.root/'target'
        for directory,label in ((launcher,'launcher'),(target,'target')):
            (directory/'bin').mkdir(parents=True)
            program=directory/'bin/fixture'
            program.write_text('#!/bin/sh\nprintf '+label+'\n');program.chmod(0o755)
        server={'transport':'stdio','command':'fixture','cwd':str(target),'env':{'PATH':'./bin'}}
        previous=Path.cwd()
        try:
            os.chdir(launcher)
            argv,cwd,env=connect.process_spec(server,self.config_path)
            self.assertEqual(argv[0],str(target/'bin/fixture'))
            self.assertEqual(env['PATH'],str(target/'bin'))
            result=subprocess.run(argv,cwd=cwd,env=env,capture_output=True,text=True,timeout=3)
            self.assertEqual(result.stdout,'target')
        finally:os.chdir(previous)
    def test_url_restrictions(self):
        for bad in ('http://example.com/mcp','http://localhost:123/mcp','https://u:secret@example.com/mcp','https://example.com/mcp?token=secret','https://example.com/mcp#secret'):
            with self.assertRaises(connect.SetupError):connect.validate_url(bad)
        self.assertTrue(connect.validate_url('http://127.0.0.1:123/mcp'))
        self.assertFalse(connect.validate_url('https://internal.example/mcp'))
    def fake_client(self):
        path=self.root/'fake-client';path.write_text(FAKE_CLIENT);path.chmod(0o755);return path
    def test_native_runtime_sequence_receipt_and_stop(self):
        client=self.fake_client();data=self.root/'state-dir'
        result=connect.start(client,data,'fixture',self.server,self.config_path)
        self.assertFalse(result['chatgpt_end_to_end_verified'])
        self.assertTrue(result['runtime']['process_running'])
        calls=[json.loads(x) for x in (data/'state/calls.jsonl').read_text().splitlines()]
        self.assertEqual([x['argv'][1] for x in calls],['list','connect','status'])
        self.assertIn('env:OPENAI_TUNNEL_KEY',calls[1]['argv'])
        self.assertNotIn('runtime-secret',json.dumps(calls))
        cmd=calls[1]['argv'][calls[1]['argv'].index('--mcp-command')+1]
        import shlex
        self.assertEqual(shlex.split(cmd),[sys.executable,str(MODULE),'_stdio','--config',str(self.config_path),'--server','fixture'])
        self.assertTrue(connect.start(client,data,'fixture',self.server,self.config_path)['already_running'])
        stopped=connect.native_json(client,['stop','fixture'],connect.native_environment(data))
        self.assertFalse(stopped['process_running'])
    def test_repeated_start_reuses_runtime_without_probing_or_credentials(self):
        client=self.fake_client();data=self.root/'state-dir'
        connect.start(client,data,'fixture',self.server,self.config_path)
        with mock.patch.dict(os.environ):
            os.environ.pop('OPENAI_TUNNEL_KEY',None)
            os.environ.pop('APP_TOKEN_SOURCE',None)
            with mock.patch.object(connect,'probe',side_effect=AssertionError('must not start another exclusive MCP')):
                result=connect.start(client,data,'fixture',self.server,self.config_path)
        self.assertTrue(result['already_running'])
    def test_partial_native_start_keeps_owned_receipt(self):
        client=self.fake_client();data=self.root/'state-dir'
        with mock.patch.object(connect,'native_json',side_effect=[[],connect.SetupError('connect failed after spawning')]):
            with self.assertRaises(connect.SetupError):
                connect.start(client,data,'fixture',self.server,self.config_path)
        record=json.loads((data/'receipts/fixture.json').read_text())
        self.assertEqual(record['identity']['server'],'fixture')
        self.assertNotIn('runtime-secret',json.dumps(record))
    def test_profile_change_not_overwritten(self):
        client=self.fake_client();data=self.root/'state-dir'
        connect.start(client,data,'fixture',self.server,self.config_path)
        profile=data/'profiles/fixture.yaml';profile.write_text('changed outside launcher')
        with self.assertRaises(connect.SetupError):connect.start(client,data,'fixture',self.server,self.config_path)
        self.assertEqual(profile.read_text(),'changed outside launcher')
    def test_preexisting_unknown_profile_not_overwritten(self):
        client=self.fake_client();data=self.root/'state-dir'
        profile=data/'profiles/fixture.yaml';profile.parent.mkdir(parents=True)
        profile.write_text('manual profile')
        with self.assertRaises(connect.SetupError):
            connect.start(client,data,'fixture',self.server,self.config_path)
        self.assertEqual(profile.read_text(),'manual profile')
    def test_unknown_alias_not_overwritten_and_same_tunnel_rejected(self):
        client=self.fake_client();data=self.root/'state-dir'
        connect.start(client,data,'fixture',self.server,self.config_path)
        with self.assertRaises(connect.SetupError):connect.start(client,data,'second',self.server,self.config_path)
        (data/'receipts/fixture.json').unlink()
        with self.assertRaises(connect.SetupError):connect.start(client,data,'fixture',self.server,self.config_path)
    def test_download_hash_mismatch_never_installs(self):
        class Response(io.BytesIO):
            def geturl(self):return 'https://github.com/openai/tunnel-client/releases/download/v0.0.20/archive.zip'
        with mock.patch.object(connect.platform,'system',return_value='Linux'),mock.patch.object(connect.platform,'machine',return_value='x86_64'),mock.patch.object(connect.urllib.request,'urlopen',return_value=Response(b'not-real-archive')):
            with self.assertRaisesRegex(connect.SetupError,'SHA-256'):
                connect.install_client(self.root/'client-data')
        self.assertFalse((self.root/'client-data/bin/tunnel-client-v0.0.20').exists())
    def test_native_http_credentials_are_references(self):
        with mock.patch.dict(os.environ,{'AUTH_ONE':'Bearer secret-one','AUTH_TWO':'secret-two'}):
            env=connect.native_environment(self.root,{'transport':'http','url':'https://example.com/mcp','headers_from_env':{'Authorization':'AUTH_ONE','X-API-Key':'AUTH_TWO'}},'runtime-secret')
        self.assertEqual(env['MCP_EXTRA_HEADERS'],'Authorization: env:AUTH_ONE,X-API-Key: env:AUTH_TWO')
        self.assertEqual(env['MCP_DISCOVERY_EXTRA_HEADERS'],env['MCP_EXTRA_HEADERS'])
        self.assertNotIn('secret-one',env['MCP_EXTRA_HEADERS'])

if __name__=='__main__':unittest.main()
