import unittest
from chunked_planner import split_source, merge_plans
class ChunkTests(unittest.TestCase):
    def test_preserve_all_input(self):
        src=('local x = 1234567890\n'*30)+'-- ñ\r\n'
        chunks=split_source(src,100)
        self.assertEqual(''.join(x['source'] for x in chunks),src)
        self.assertEqual(chunks[0]['start_line'],1)
    def test_long_line_fails_closed(self):
        with self.assertRaises(ValueError):split_source('a'*101,100)
    def test_evidence_and_zero_pose(self):
        quote='local this_is_original_source = 100'
        c={'source':quote,'start_line':1,'end_line':1}
        p={'states':{'idle':{'original_quote':quote,'body':"pose('root', CFrame.new(0,0,0), CFrame.new(0,0,0))"},'walk':{'original_quote':'not found in source file','body':'pose("root", CFrame.new(1,0,0), CFrame.new())'}},'abilities':[]}
        result=merge_plans([(c,p)])
        self.assertEqual(result['states'],{})
        self.assertEqual(len(result['warnings']),2)
    def test_verified_quote_not_proof(self):
        quote='local actual_original_expression = 12'
        c={'source':quote,'start_line':1,'end_line':1}
        p={'states':{},'abilities':[{'name':'attack','original_quote':quote,'body':'effect()'}]}
        result=merge_plans([(c,p)])
        self.assertEqual(len(result['abilities']),1)
if __name__ == '__main__': unittest.main()
