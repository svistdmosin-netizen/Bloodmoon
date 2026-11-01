import unittest
from preservation_audit import analyze

class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.source = 'local function foo()\n return 7\nend\n'
        self.ast = {'type':'AstStatBlock','body':[{'type':'AstStatLocalFunction','name':{'name':'foo'},'location':'0,0 - 2,3','func':{'type':'AstExprFunction'}}]}
    def test_exact_copy(self):
        result = analyze(self.source,self.ast,self.source)
        self.assertTrue(result['source_equals_output'])
        self.assertEqual(result['named_functions_exact_text_found'],1)
    def test_changed_not_claimed_exact(self):
        result = analyze(self.source,self.ast,self.source.replace('7','8'))
        self.assertFalse(result['source_equals_output'])
        self.assertEqual(result['named_functions_exact_text_found'],0)
    def test_missing_output_is_unknown(self):
        result = analyze(self.source,self.ast)
        self.assertIsNone(result['named_functions_exact_text_found'])
        self.assertIsNone(result['functions'][0]['exact_source_found_in_output'])
    def test_detect_external_call(self):
        self.ast['body'].append({'type':'AstExprCall','location':'3,0 - 3,3','func':{'type':'AstExprGlobal','global':'loadstring'}})
        self.assertEqual(analyze(self.source,self.ast)['external_calls'][0]['call'],'loadstring')
    def test_unicode_luau_byte_columns(self):
        src='-- тест\nlocal function foo() return 7 end\n'
        ast={'type':'AstStatBlock','body':[{'type':'AstStatLocalFunction','name':{'name':'foo'},'location':'1,0 - 1,33'}]}
        self.assertEqual(analyze(src,ast,src)['named_functions_exact_text_found'],1)

if __name__ == '__main__': unittest.main()
