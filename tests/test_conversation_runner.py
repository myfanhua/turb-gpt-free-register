import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from core import conversation_manager as m
from core.conversation_runner import *

class Fake:
 def __init__(self,fail=None):self.sent=[];self.fail=fail
 def create_conversation(self,**_):return 'c1'
 def send_message(self,**kw):self.sent.append(kw['message']);return kw['message']
 def await_completion(self,event,**_):
  if event==self.fail:raise RuntimeError('bad')
  return True
class Tests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.p=Path(self.t.name)/'pool.json';self.x=patch.object(m,'_PATH',self.p);self.x.start();m.put_template('t','Name',['a','b'])
 def tearDown(self):self.x.stop();self.t.cleanup()
 def test_template_safe_list_and_crud(self):
  t=[x for x in m.list_templates() if x['id']=='t'];self.assertEqual(t[0]['message_count'],2);self.assertNotIn('messages',t[0]);self.assertEqual(m.get_template('t')['name'],'Name');m.delete_template('t');self.assertIsNone(m.get_template('t'))
 def test_bind_claim_and_completed_idempotence(self):
  b=m.bind(1,'t');self.assertEqual(b['status'],'queued');self.assertIsNotNone(m.claim(1,'t'));self.assertIsNone(m.claim(1,'t'));m.checkpoint(1,'t',status='completed');self.assertIsNone(m.claim(1,'t'))
 def test_runner_checkpoint_and_resume(self):
  m.bind(2,'t');r=run_binding(2,'t',Fake('b'));self.assertEqual(r['status'],'partial');self.assertEqual(r['current_index'],1);self.assertIsNone(run_binding(2,'t',Fake()));r=run_binding(2,'t',Fake(),retry=True);self.assertEqual(r['status'],'completed')
 def test_disabled_and_har(self):
  self.assertIn('url',validate_capture_contract({}));self.assertEqual(len(validate_capture_contract({k:1 for k in HAR_REQUIRED})),0)
  with self.assertRaises(RuntimeError):ProtocolChatGPTWebTransport().create_conversation(account_id=1)
 def test_retry_attempt(self):
  m.bind(3,'t');m.claim(3,'t');m.checkpoint(3,'t',status='failed');self.assertEqual(m.retry(3,'t')['attempt'],1)
 def test_put_updates_timestamp(self):
  a=m.get_template('t')['created_at'];m.put_template('t','New',['x']);self.assertEqual(m.get_template('t')['created_at'],a);self.assertEqual(m.get_template('t')['name'],'New')
 def test_list_and_get_binding(self):
  m.bind(4,'t');self.assertEqual(m.get_binding(4,'t')['account_id'],4);self.assertEqual(len(m.list_bindings(4)),1)
 def test_failed_requires_explicit_retry(self):
  m.bind(5,'t');m.claim(5,'t');m.checkpoint(5,'t',status='failed');self.assertIsNone(m.claim(5,'t'));self.assertEqual(m.claim(5,'t',retry=True)['attempt'],1)
 def test_running_rejected(self):
  m.bind(6,'t');m.claim(6,'t');self.assertIsNone(m.retry(6,'t'))
 def test_completion_must_be_explicit(self):
  class NoDone(Fake):
   def await_completion(self,*_,**__):return False
  m.bind(7,'t');self.assertEqual(run_binding(7,'t',NoDone())['status'],'failed')
 def test_protocol_failure_records_redacted_diagnostics(self):
  from core.chatgpt_conversation_protocol import ConversationProtocolError
  class TurnstileRequired(Fake):
   def send_message(self,**_):raise ConversationProtocolError('authorization=secret-token',stage='chat_requirements',http_status=403)
  m.bind(70,'t');row=run_binding(70,'t',TurnstileRequired())
  self.assertEqual(row['status'],'failed');self.assertEqual(row['stage'],'chat_requirements');self.assertEqual(row['http_status'],403)
  self.assertIn('[redacted]',row['reason']);self.assertNotIn('secret-token',row['reason'])
 def test_turnstile_requires_explicit_browser_verification_then_retry(self):
  from core.chatgpt_conversation_protocol import ConversationProtocolError
  class TurnstileRequired(Fake):
   def send_message(self,**_):raise ConversationProtocolError('需要 Turnstile；当前登录态缺少已验证 token',stage='chat_requirements')
  m.bind(71,'t');row=run_binding(71,'t',TurnstileRequired())
  self.assertEqual(row['status'],'needs_browser_verification');self.assertIsNone(run_binding(71,'t',Fake()))
  self.assertEqual(m.retry(71,'t')['attempt'],1)
 def test_new_conversation_stored(self):
  m.bind(8,'t');self.assertEqual(run_binding(8,'t',Fake())['conversation_id'],'c1')
 def test_attempt_starts_zero(self): self.assertEqual(m.bind(9,'t')['attempt'],0)
 def test_template_invalid(self):
  with self.assertRaises(ValueError):m.put_template('bad','',[])
 def test_unknown_bind(self):
  with self.assertRaises(KeyError):m.bind(1,'none')
