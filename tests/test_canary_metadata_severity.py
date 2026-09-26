"""Failure severity is tested with synthetic HTTP exceptions, never real requests."""
from pathlib import Path
import tempfile,sys,unittest
from unittest.mock import patch
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
import canary_acquire as a

class MetadataSeverity(unittest.TestCase):
    def attempt(self,name,exc,warning_target=True):
        with tempfile.TemporaryDirectory() as d:
            records,errors,warnings=[],[],[]
            asset={'size':1,'browser_download_url':'https://github.com/yt-feng/poly/releases/download/capture-v2-1-1/'+name}
            with patch.object(a,'request',side_effect=exc):
                n=a._download(output=Path(d),release={'tag_name':'capture-v2-1-1'},assets={name:asset},name=name,
                    bucket='research',records=records,errors=errors,metadata_warnings=warnings if warning_target else None)
            self.assertEqual(n,0);self.assertEqual(records,[])
            self.assertEqual(list(Path(d).rglob('*')),[])
            return errors,warnings
    def http(self,code):return HTTPError('https://github.com/example',code,'synthetic',{},None)
    def test_status_manifest_500_is_visible_warning(self):
        e,w=self.attempt('manifest.json',self.http(500));self.assertFalse(e);self.assertEqual(w[0]['http_status'],500)
    def test_status_health_404_is_visible_warning(self):
        e,w=self.attempt('health.json',self.http(404));self.assertFalse(e);self.assertEqual(len(w),1)
    def test_archive_500_stays_fatal(self):
        e,w=self.attempt('snapshots-1.jsonl.gz',self.http(500));self.assertTrue(e);self.assertFalse(w)
    def test_label_404_stays_fatal(self):
        e,w=self.attempt('labels-1.jsonl.gz',self.http(404));self.assertTrue(e);self.assertFalse(w)
    def test_market_rules_json_not_optional(self):
        e,w=self.attempt('rules.json',self.http(500));self.assertTrue(e);self.assertFalse(w)
    def test_access_and_rate_restrictions_stay_fatal(self):
        for code in (401,403,418,429,451):
            e,w=self.attempt('manifest.json',self.http(code));self.assertTrue(e);self.assertFalse(w)
    def test_checksum_error_never_optional(self):
        e,w=self.attempt('manifest.json',ValueError('SHA256 mismatch'));self.assertTrue(e);self.assertFalse(w)
    def test_byte_cap_error_never_optional(self):
        e,w=self.attempt('health.json',ValueError('Response exceeds download budget'));self.assertTrue(e);self.assertFalse(w)
    def test_no_warning_sink_preserves_legacy_failure(self):
        e,w=self.attempt('manifest.json',self.http(500),False);self.assertTrue(e);self.assertFalse(w)
    def test_warning_cannot_claim_signal_evidence(self):
        e,w=self.attempt('manifest.json',self.http(503));self.assertFalse(w[0]['used_for_signals_or_labels'])

if __name__=='__main__':unittest.main()
