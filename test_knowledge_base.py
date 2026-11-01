import unittest
from knowledge_base import retrieve
class KnowledgeTests(unittest.TestCase):
 def test_basics_always_present(self):
  self.assertIn('hub-contract', [x['id'] for x in retrieve('local x=1')])
 def test_quaternion_detected(self):
  self.assertIn('quaternion', [x['id'] for x in retrieve('function clerp(a,b,t) end')])
 def test_no_search_or_source_execution(self):
  r=retrieve('loadstring(game:GetObjects("x"))')
  self.assertIn('external-code',[x['id'] for x in r])
 def test_bound(self):
  self.assertLessEqual(sum(len(x['text']) for x in retrieve('clerp effects',max_chars=300)),300)
if __name__=='__main__':unittest.main()
