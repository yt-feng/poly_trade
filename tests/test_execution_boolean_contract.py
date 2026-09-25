"""Classification-only tests. No accounts or execution."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
from execution_reason_codes import classify

class BooleanQualification(unittest.TestCase):
    def test_zero_not_boolean_false(self):
        self.assertEqual(classify({'signal_qualified':0})['reason_code'],'SIGNAL_QUALIFICATION_UNKNOWN')
    def test_one_not_boolean_true(self):
        self.assertEqual(classify({'signal_qualified':1})['reason_code'],'SIGNAL_QUALIFICATION_UNKNOWN')

if __name__=='__main__':unittest.main()
