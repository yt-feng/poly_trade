from pathlib import Path
import sys,tempfile,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
from receipt_source_audit import audit
class Audit(unittest.TestCase):
    def fixture(self,runner,adapter='def price_text(p):\n return round(p,2)\n'):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);root=Path(t.name);(root/'analysis').mkdir()
        (root/'analysis/live_paper_trader.py').write_text(runner);(root/'analysis/live_execution.py').write_text(adapter);return root
    def test_unconditional_mark(self):
        r=audit(self.fixture('def record_signal(self):\n self.mark_slug_traded("x")\n'))
        self.assertIn('ATTEMPT_MARKED_AS_TRADED',[f['code']for f in r['findings']])
    def test_guarded_mark_not_same_pattern(self):
        r=audit(self.fixture('def record_signal(self):\n if self.confirmed:\n  self.mark_slug_traded("x")\n'))
        self.assertNotIn('ATTEMPT_MARKED_AS_TRADED',[f['code']for f in r['findings']])
    def test_no_execution(self):
        r=audit(self.fixture('raise RuntimeError("NEVER EXECUTE INPUT")\ndef settle_positions(self):\n self.bankroll=self.last_final_by_slug\n'))
        self.assertFalse(r['execution_enabled']);self.assertFalse(r['account_cash_verified'])
    def test_tick_pattern(self):
        r=audit(self.fixture('x=0\n','def price_text(p):\n return p.quantize(Decimal("0.01"))\n'))
        self.assertIn('HARDCODED_PRICE_GRID',[f['code']for f in r['findings']])
    def test_pending_identity(self):
        r=audit(self.fixture('def process_pending(self,snap):\n return snap["price"]\n'))
        self.assertIn('NO_PENDING_SNAPSHOT_MARKET_CHECK',[f['code']for f in r['findings']])
    def test_guarded_pending(self):
        r=audit(self.fixture('def process_pending(self,snap):\n if snap["slug"] != self.slug: return\n'))
        self.assertNotIn('NO_PENDING_SNAPSHOT_MARKET_CHECK',[f['code']for f in r['findings']])
    def test_no_sell_not_certification(self):
        r=audit(self.fixture('x=0\n','def sell_placeholder(): pass\n'))
        self.assertFalse(r['live_098_implementation_verified'])
if __name__=='__main__':unittest.main()
