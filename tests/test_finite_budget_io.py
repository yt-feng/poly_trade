"""Sealed CLI integration with ephemeral SYNTHETIC identities only."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import research_vault as vault
ROOT=Path(__file__).resolve().parents[1]
TOOL=ROOT/'tools'/'finite_budget_math.py'

class SealedBudgetIO(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.d=Path(self.tmp.name)
        recipient,bundle,recovery=vault.make_identity('synthetic-fixture-only')
        self.recipient=recipient
        self.private=vault.unlock_identity(bundle,recovery,'synthetic-fixture-only')
        self.pub=self.d/'public.json';self.pub.write_bytes(vault.canonical(recipient))
        self.inp=self.d/'scenario.json'
        self.payload={'equity':10,'available_cash':10,'minimum_stake':1,'all_in_cap':3,
          'legal_stakes':[1,2], 'net_costs_included':True,
          'states':[{'probability':.26,'net_return':3,'cash_cycle_seconds':30},
                    {'probability':.74,'net_return':-1,'cash_cycle_seconds':30}],
          'private_fixture_marker':'DO_NOT_ECHO_SYNTHETIC_MARKER'}
        self.inp.write_text(json.dumps(self.payload))
        self.out=self.d/'result.vault'
    def tearDown(self):self.tmp.cleanup()
    def run_cli(self,**kw):
        args={'input':str(self.inp),'recipient':str(self.pub),'recipient-id':self.recipient['recipient_id'],'output':str(self.out)}
        args.update(kw)
        cmd=[sys.executable,str(TOOL)]
        for k,v in args.items():cmd.extend(['--'+k,str(v)])
        return subprocess.run(cmd,cwd=ROOT,text=True,capture_output=True,timeout=10)
    def test_sealed_roundtrip(self):
        r=self.run_cli();self.assertEqual(r.returncode,0,r.stderr)
        result=json.loads(vault.unseal(self.out.read_bytes(),self.private))
        self.assertEqual(result['argmax_stake_in_supplied_scenarios'],0)
        self.assertFalse(result['orders_enabled'])
    def test_no_private_input_echo(self):
        r=self.run_cli();self.assertEqual(r.returncode,0)
        self.assertNotIn('DO_NOT_ECHO_SYNTHETIC_MARKER',r.stdout+r.stderr)
        self.assertNotIn('DO_NOT_ECHO_SYNTHETIC_MARKER',self.out.read_text())
    def test_missing_recipient_no_output(self):
        r=self.run_cli(recipient=self.d/'absent.json')
        self.assertNotEqual(r.returncode,0);self.assertFalse(self.out.exists())
    def test_wrong_fingerprint_no_output(self):
        r=self.run_cli(**{'recipient-id':'0'*64})
        self.assertNotEqual(r.returncode,0);self.assertFalse(self.out.exists())
    def test_existing_output_not_replaced(self):
        self.out.write_bytes(b'existing-fixture')
        self.assertNotEqual(self.run_cli().returncode,0)
        self.assertEqual(self.out.read_bytes(),b'existing-fixture')
    def test_no_plaintext_extension(self):
        out=self.d/'result.json';r=self.run_cli(output=out)
        self.assertNotEqual(r.returncode,0);self.assertFalse(out.exists())
    def test_net_cost_convention_required(self):
        self.payload['net_costs_included']=False;self.inp.write_text(json.dumps(self.payload))
        self.assertNotEqual(self.run_cli().returncode,0);self.assertFalse(self.out.exists())
    def test_unknown_exit_not_zero(self):
        self.payload['states'][1]['net_return']=None;self.inp.write_text(json.dumps(self.payload))
        self.assertNotEqual(self.run_cli().returncode,0);self.assertFalse(self.out.exists())
    def test_plaintext_within_repo_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as t:
            p=Path(t)/'synthetic.json';p.write_text(json.dumps(self.payload))
            self.assertNotEqual(self.run_cli(input=p).returncode,0)
            self.assertFalse(self.out.exists())

if __name__=='__main__':unittest.main()
