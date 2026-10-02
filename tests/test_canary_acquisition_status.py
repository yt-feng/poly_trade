"""Exercise the actual workflow status block with synthetic manifests only.

No API, artifact download, strategy evaluation, account or credentials are used.
A diagnostic success must not change a failed integrity gate into success.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'analysis'))
import forward_acquire  # fixed local module; not imported from temporary input


def status_source():
    workflow=(ROOT/'.github/workflows/canary-forward-audit.yml').read_text()
    block=workflow.split('      - name: Verify bounded archive coverage and record scope\n',1)[1]
    block=block.split('      - name: Independent raw sample after archive transport errors\n',1)[0]
    code=block.split("          python - <<'PY'\n",1)[1].rsplit('          PY\n',1)[0]
    return textwrap.dedent(code)


class StatusContractTests(unittest.TestCase):
    def manifest(self):
        return {'selection_complete':True,'acquisition_halted':False,
            'releases':[{'eligible_snapshot_label_archives':1}],
            'files':[{'name':'snapshots-fixture.jsonl.gz','bytes':7}],
            'downloaded_bytes':7,'max_bytes':7,
            'errors':[],'skipped':[],'failure_history':[]}

    def failure(self,status=500,attempts=4,refused=False):
        m=self.manifest()
        m.update(selection_complete=False,acquisition_halted=True,releases=[],files=[],
                 downloaded_bytes=0,errors=[{'http_status':status,'attempts':attempts,
                  'retryable':not refused,'access_refused':refused}])
        return m

    def run_status(self,manifest):
        code=compile(status_source(),'<actual-workflow-status>','exec')
        cwd=Path.cwd(); old_path=sys.path[:]; output=io.StringIO(); exit_reason=None
        try:
            with tempfile.TemporaryDirectory() as temporary:
                os.chdir(temporary)
                if manifest is not None:
                    p=Path('forward_inputs/acquisition_manifest.json')
                    p.parent.mkdir();p.write_text(json.dumps(manifest))
                try:
                    with contextlib.redirect_stdout(output):exec(code,{})
                except SystemExit as exc:exit_reason=exc.code
                saved=json.loads(Path('forward_inputs/deployment_status.json').read_text())
        finally:
            os.chdir(cwd);sys.path[:]=old_path
        printed=json.loads(output.getvalue())
        self.assertEqual(saved,printed)
        return printed,exit_reason,output.getvalue()

    def test_valid_manifest_remains_valid_without_trade_approval(self):
        s,e,_=self.run_status(self.manifest())
        self.assertIsNone(e);self.assertTrue(s['bounded_acquisition_complete'])
        self.assertEqual(s['public_failures'],[])
        self.assertFalse(s['strategy_evaluated']);self.assertFalse(s['live_trading_enabled'])

    def test_planning_500_displays_typed_details_and_stays_failed(self):
        s,e,_=self.run_status(self.failure())
        self.assertIsNotNone(e);self.assertFalse(s['bounded_acquisition_complete'])
        self.assertEqual(s['public_failures'],[{'phase':'selection_plan','http_status':500,
            'attempts':4,'retryable':True,'access_refused':False}])

    def test_access_refusal_is_not_relabeled_as_retryable(self):
        s,e,_=self.run_status(self.failure(403,1,True))
        self.assertIsNotNone(e);self.assertTrue(s['public_failures'][0]['access_refused'])
        self.assertFalse(s['public_failures'][0]['retryable'])

    def test_missing_http_code_stays_unknown(self):
        m=self.failure();del m['errors'][0]['http_status']
        self.assertIsNone(self.run_status(m)[0]['public_failures'][0]['http_status'])

    def test_bool_and_out_of_range_codes_not_cast_to_integers(self):
        for value in (True,False,99,600,'500',{'code':500}):
            with self.subTest(value=value):
                s,_,_=self.run_status(self.failure(value))
                self.assertIsNone(s['public_failures'][0]['http_status'])

    def test_unknown_attempt_count_not_invented(self):
        for value in (None,True,0,101,'four'):
            with self.subTest(value=value):
                s,_,_=self.run_status(self.failure(attempts=value))
                self.assertIsNone(s['public_failures'][0]['attempts'])

    def test_free_text_urls_headers_and_identifiers_not_published(self):
        marker='SYNTHETIC_DO_NOT_DISCLOSE'
        m=self.failure();m['errors'][0].update(error=marker,tag=marker,name=marker,
            url='https://example.invalid/'+marker,headers={'Authorization':marker},stage=marker)
        s,e,text=self.run_status(m)
        self.assertNotIn(marker,text);self.assertIsNotNone(e)
        self.assertEqual(s['public_failures'][0]['phase'],'selection_plan')

    def test_string_booleans_not_promoted_to_true(self):
        m=self.failure();m['errors'][0].update(retryable='false',access_refused='true')
        s,_,_=self.run_status(m)
        self.assertIsNone(s['public_failures'][0]['retryable'])
        self.assertIsNone(s['public_failures'][0]['access_refused'])

    def test_archive_transfer_distinguished_from_planning(self):
        m=self.manifest();m['errors']=[{'http_status':503,'attempts':4}]
        s,e,_=self.run_status(m)
        self.assertEqual(s['public_failures'][0]['phase'],'archive_transfer')
        self.assertIsNotNone(e)

    def test_no_manifest_never_looks_like_a_clean_run(self):
        s,e,_=self.run_status(None)
        self.assertFalse(s['source_manifest_present'])
        self.assertFalse(s['bounded_acquisition_complete']);self.assertIsNotNone(e)
        self.assertIsNone(s['selection_complete'])

    def test_detail_cap_preserves_full_failure_count(self):
        m=self.failure();m['errors']=m['errors']*25
        s,e,_=self.run_status(m)
        self.assertEqual(len(s['public_failures']),20)
        self.assertEqual(s['failure_details_omitted'],5)
        self.assertEqual(s['unresolved_error_count'],25);self.assertIsNotNone(e)

    def test_malformed_error_is_unknown_not_a_free_text_dump(self):
        m=self.failure();m['errors']=['SYNTHETIC_DO_NOT_DISCLOSE']
        s,e,text=self.run_status(m)
        self.assertNotIn('SYNTHETIC_DO_NOT_DISCLOSE',text)
        self.assertIsNone(s['public_failures'][0]['http_status']);self.assertIsNotNone(e)

if __name__=='__main__':unittest.main()
