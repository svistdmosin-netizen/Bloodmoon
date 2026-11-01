import unittest
from chunked_planner import split_source
class FunctionBoundaryTests(unittest.TestCase):
 def test_function_boundary(self):
  src='-- '+('a'*68)+'\n'+'function foo()\n'+'-- '+('b'*48)+'\n'+'end\n'+'-- '+('c'*68)+'\n'
  ast={'type':'AstExprFunction','location':'1,0 - 3,3'}
  out=split_source(src,limit=100,ast=ast)
  self.assertEqual(''.join(c['source'] for c in out),src)
  self.assertEqual(sum(c['ends_inside_function'] for c in out),0)
  self.assertTrue(any('function foo()' in c['source'] and '\nend\n' in c['source'] for c in out))
 def test_oversized_explicit(self):
  src='function foo()\n'+'-- '+('x'*47)+'\n'*1+'-- '+('y'*47)+'\n'+'end\n'
  ast={'type':'AstExprFunction','location':'0,0 - 3,3'}
  out=split_source(src,limit=100,ast=ast)
  self.assertEqual(''.join(c['source'] for c in out),src)
  self.assertTrue(any(c['ends_inside_function'] for c in out))
if __name__=='__main__': unittest.main()
