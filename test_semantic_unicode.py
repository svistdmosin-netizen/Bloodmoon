import unittest
import sys
from semantic_moveset_converter import _location_offsets, run_tool
class UnicodeTests(unittest.TestCase):
    def test_byte_columns(self):
        src='local s="кот"; local x=1\n'
        start=len('local s="кот"; '.encode('utf-8'))
        end=len('local s="кот"; local x=1'.encode('utf-8'))
        a,b=_location_offsets(src,f'0,{start} - 0,{end}')
        self.assertEqual(src[a:b], 'local x=1')
    def test_no_split_unicode(self):
        with self.assertRaises(UnicodeDecodeError):_location_offsets('éx\n','0,1 - 0,2')
    def test_compiler_output_non_utf8(self):
        # An executable named luau-compile, not the real compiler, to test decoding.
        import tempfile, pathlib, os
        with tempfile.TemporaryDirectory() as d:
            f=pathlib.Path(d)/'luau-compile';f.write_text('#!/bin/sh\nprintf "\\377"\n');f.chmod(0o755)
            r=run_tool([str(f)]);self.assertTrue(r['ok']);self.assertEqual(r['stdout'],'\ufffd')
if __name__ == '__main__':unittest.main()
