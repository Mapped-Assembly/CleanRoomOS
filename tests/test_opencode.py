"""Exercise actual HTTP polling, error bounds, denied permissions and cleanup."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest

from cleanroom_os.opencode import AgentCallError, OpenCodeTransport

ROLE='cleanroom-requirements'


class OpenCodeTests(unittest.TestCase):
    def setUp(self):
        self.requests=[]
        self.behavior='normal'
        self.config={'permission':{'*':'deny'},'agent':{ROLE:{'mode':'subagent','permission':{'*':'deny'},'steps':1}}}
        self.output='{"output":{},"assumptions":[],"gaps":[]}'
        test=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self): self.respond()
            def do_POST(self): self.respond()
            def respond(self):
                path=self.path.split('?')[0]
                length=int(self.headers.get('Content-Length',0))
                body=json.loads(self.rfile.read(length)) if length else None
                test.requests.append((self.command,path,body,self.headers.get('Authorization')))
                status=200
                if path=='/config': value=test.config
                elif path=='/session': value={'id':'ses_test'}
                elif path.endswith('/prompt_async'): value=None;status=204
                elif path.endswith('/abort'): value=True
                elif path.endswith('/message'):
                    info=dict(role='assistant',agent=ROLE,providerID='test',modelID='model',finish='stop',time={'completed':1})
                    parts=[{'type':'text','text':test.output}]
                    if test.behavior=='pending': info['time']={}
                    if test.behavior=='error': info['error']={'name':'APIError','data':{'message':'secret provider body'}}
                    if test.behavior=='tool': parts.append({'type':'tool','tool':'bash'})
                    if test.behavior=='wrong_model': info['modelID']='fallback'
                    if test.behavior=='wrong_agent': info['agent']='build'
                    if test.behavior=='truncated': info['finish']='length'
                    value=[{'info':info,'parts':parts}]
                else: value={};status=404
                if test.behavior=='http_error': status=503;value={'secret':'must not appear in error'}
                self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers()
                if value is not None:self.wfile.write(json.dumps(value).encode())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.transport=OpenCodeTransport(base_url=f'http://127.0.0.1:{self.server.server_port}',model='test/model',timeout=1,poll_interval=.01,password='test-secret')

    def test_exact_role_model_and_denied_session_permissions(self):
        self.assertEqual(self.transport.complete(ROLE,{'source':'data'},{'type':'object'}),self.output)
        created=next(r for r in self.requests if r[1]=='/session')
        self.assertEqual(created[2]['permission'],[{'permission':'*','pattern':'*','action':'deny'}])
        prompt=next(r for r in self.requests if r[1].endswith('/prompt_async'))[2]
        self.assertEqual(prompt['agent'],ROLE)
        self.assertEqual(prompt['model'],{'providerID':'test','modelID':'model'})
        self.assertEqual(self.requests[-1][1],'/session/ses_test/abort')
        self.assertTrue(all(r[3].startswith('Basic ') for r in self.requests))

    def test_failures_abort_without_tool_execution_or_model_fallback(self):
        for behavior in ['error','tool','wrong_model','wrong_agent','truncated']:
            self.behavior=behavior;self.requests.clear()
            with self.subTest(behavior=behavior),self.assertRaises(AgentCallError) as caught:
                self.transport.complete(ROLE,{}, {})
            self.assertNotIn('secret',str(caught.exception))
            self.assertEqual(self.requests[-1][1],'/session/ses_test/abort')
            self.assertEqual(len([r for r in self.requests if r[1].endswith('/prompt_async')]),1)

    def test_deadline_aborts_owned_session(self):
        self.behavior='pending'
        with self.assertRaisesRegex(AgentCallError,'deadline'):self.transport.complete(ROLE,{}, {})
        self.assertEqual(self.requests[-1][1],'/session/ses_test/abort')

    def test_fences_prose_arrays_and_bad_json_rejected(self):
        for text in ['approved','```json\n{}\n```','[]','{']:
            self.output=text
            with self.subTest(text=text),self.assertRaises(AgentCallError):self.transport.complete(ROLE,{}, {})

    def test_permission_override_prevents_session_creation(self):
        self.config['agent'][ROLE]['permission']['bash']='allow'
        with self.assertRaisesRegex(AgentCallError,'configuration'):self.transport.complete(ROLE,{}, {})
        self.assertEqual([r[1] for r in self.requests],['/config'])

    def test_http_failure_does_not_leak_body_or_retry(self):
        self.behavior='http_error'
        with self.assertRaisesRegex(AgentCallError,'503') as caught:self.transport.complete(ROLE,{}, {})
        self.assertNotIn('secret',str(caught.exception));self.assertEqual(len(self.requests),1)

    def test_missing_model_and_unsupported_role_never_contact_server(self):
        self.transport.model=''
        with self.assertRaisesRegex(AgentCallError,'OPENCODE_MODEL'):self.transport.complete(ROLE,{}, {})
        self.transport.model='test/model'
        with self.assertRaisesRegex(AgentCallError,'Unsupported'):self.transport.complete('build',{}, {})
        self.assertEqual(self.requests,[])


if __name__=='__main__':unittest.main()
