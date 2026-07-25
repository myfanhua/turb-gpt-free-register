import unittest
from core.post_register_workflow import *

class Fake:
    def __init__(self, streams): self.streams=iter(streams); self.sent=[]
    def send(self, **kwargs): self.sent.append(kwargs); return next(self.streams)

class WorkflowTests(unittest.TestCase):
    def test_disabled(self): self.assertEqual(run_workflow(enabled=False).status, "skipped")
    def test_config(self):
        self.assertEqual(parse_messages('["a", "b"]', 1), ["a"])
        with self.assertRaises(WorkflowConfigError): parse_messages('["a"]', 2)
    def test_chunk_done_orders_messages(self):
        fake=Fake([[b"data: {\"x\":1}\n\n", b"data: [DO", b"NE]\n\n"], ["data: [DONE]\n\n"]])
        r=run_workflow(enabled=True, message_list=["a","b"], message_count=2, transport=fake)
        self.assertEqual((r.status,r.completed_count), ("success",2)); self.assertEqual([x["message"] for x in fake.sent],["a","b"])
    def test_stream_error_is_partial(self):
        r=run_workflow(enabled=True,message_list=["a","b"],message_count=2,transport=Fake([["data: [DONE]\n\n"],["data: {\"error\":\"bad\"}\n\n"]]))
        self.assertEqual((r.status,r.completed_count),("partial",1))
    def test_no_contract_fails(self):
        self.assertEqual(run_workflow(enabled=True,message_list=["a"],message_count=1).status,"failed")

if __name__ == '__main__': unittest.main()
