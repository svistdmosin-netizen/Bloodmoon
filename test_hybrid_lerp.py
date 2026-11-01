import unittest
import re
from hybrid_moveset_converter import build_module, analyze
class HybridLerpTests(unittest.TestCase):
    def test_builder_has_independent_virtual_joint_state_and_euler_lerp(self):
        plan={'name':'example','description':'test','states':{'idle':{'body':'lerpPoseEuler("neck", "C1", CFrame.new(0,1,0), 0.1)'}},'abilities':[]}
        out=build_module(plan)
        self.assertIn('local function lerpPoseEuler(name, property, target, alpha)',out)
        self.assertIn('current[property] = CFrame.new(p)',out)
        self.assertIn('table.clear(state.jointPose)',out)
        self.assertIn('lerpPoseEuler("neck", "C1", CFrame.new(0,1,0), 0.1)',out)
    def test_mouse_button_name_not_regex_match(self):
        inv=analyze('mouse.Button1Down:connect(function() end)','test.lua')
        self.assertEqual([x['value'] for x in inv.controls],['MouseButton1'])
    def test_effect_updates_not_skipped_when_attacking(self):
        out=build_module({'name':'t','description':'','states':{},'abilities':[]})
        self.assertIn('if fn and not state.attacking then fn(state) end',out)
        self.assertNotIn('if state.attacking then return end\n        local hs=',out)
if __name__ == '__main__':unittest.main()
