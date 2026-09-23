import json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'analysis'))
from forward_roll_conflict import (
    DEFAULT_REGISTRY,block_records,clustered_bootstrap_lower,enrich_fee_precision,
    exposure_value_cents,fee_round_5,gate,slug_start_ms,
)


class ForwardRegistry(unittest.TestCase):
    def setUp(self):
        self.registry=json.loads(DEFAULT_REGISTRY.read_text())

    def test_candidate_is_single_locked_primary(self):
        c=self.registry['candidate'];r=self.registry['registration']
        self.assertEqual((c['hypothesis'],c['execution_model'],c['hold_seconds']),('roll_conflict','taker',30))
        self.assertFalse(r['candidate_substitution_allowed'])
        self.assertTrue(r['new_mechanism_requires_new_registry_version'])
        self.assertEqual(r['forward_min_window_start_ms'],1790152200000)

    def test_whole_window_boundary(self):
        self.assertLess(slug_start_ms('btc-updown-5m-1790151900'),self.registry['registration']['forward_min_window_start_ms'])
        self.assertEqual(slug_start_ms('btc-updown-5m-1790152200'),self.registry['registration']['forward_min_window_start_ms'])

    def test_checkpoints_are_monitoring_only(self):
        f=self.registry['fixed_checkpoints']
        self.assertTrue(f['checkpoint_results_are_monitoring_only'])
        self.assertEqual(f['minimum_final_valued_attempts'],100)
        self.assertEqual(f['minimum_final_distinct_windows'],300)
        self.assertEqual(f['minimum_final_utc_days'],7)


class FeeAndBlocks(unittest.TestCase):
    def test_fee_rounding_and_net_share_view(self):
        self.assertEqual(fee_round_5(0.0000149),0.00001)
        a={
            'status':'valued','shares':5,'entry_fee_per_share':0.0175,
            'fees_cents_per_share':3.5,'gross_cents_per_share':10.0,
            'net_cents_per_share':6.5,'stress_net_cents_per_share':5.5,
        }
        x=enrich_fee_precision(a)
        self.assertEqual(x['public_net_shares_after_fee'],5)
        self.assertFalse(x['private_net_share_reconciled'])
        self.assertAlmostEqual(x['conservative_net_cents_per_share'],6.5,places=8)

    def test_unknown_exit_enters_worst_case_block_value(self):
        a={'status':'exit_unvalued','shares':5,'entry_ms':1790152201000,'pnl_lower_bound_usd':-2.5}
        self.assertEqual(exposure_value_cents(a),-50.0)
        days,blocks=block_records([a],2)
        self.assertEqual(days[0]['n'],1);self.assertEqual(blocks[0]['mean_cents_per_share'],-50.0)

    def test_positive_cluster_bootstrap_is_positive(self):
        rows=[{'n':2,'sum_cents_per_share':4.0,'mean_cents_per_share':2.0} for _ in range(12)]
        lower=clustered_bootstrap_lower(rows,.05,1000,7,12)
        self.assertGreater(lower,0)

    def test_mixed_cluster_bootstrap_can_reject(self):
        rows=[]
        for i in range(12):
            v=4.0 if i<7 else -8.0
            rows.append({'n':1,'sum_cents_per_share':v,'mean_cents_per_share':v})
        lower=clustered_bootstrap_lower(rows,.05,2000,11,12)
        self.assertLess(lower,0)


class EvidenceGate(unittest.TestCase):
    def setUp(self):
        self.registry=json.loads(DEFAULT_REGISTRY.read_text())
        self.good={
            'valued_attempts':120,'valued_exit_fraction':1.0,'mean_net_cents_per_share':2.0,
            'utc_day_cluster_lower95_cents_per_share':0.5,'time_block_cluster_lower95_cents_per_share':0.4,
            'mean_extra_exit_tick_net_cents_per_share':1.0,'unknown_exit_worst_case_total_usd':12.0,
        }
        self.coverage={'forward_distinct_windows':320,'forward_utc_days':8}

    def test_evidence_can_reach_human_review_but_never_auto_live(self):
        result=gate(dict(self.good),dict(self.good),self.coverage,self.registry)
        self.assertTrue(result['human_review_evidence_ready'])
        self.assertFalse(result['live_canary_eligible'])
        self.assertTrue(result['live_canary_blockers'])

    def test_one_failed_stress_blocks_review(self):
        bad=dict(self.good);bad['mean_net_cents_per_share']=-0.1
        result=gate(dict(self.good),bad,self.coverage,self.registry)
        self.assertFalse(result['human_review_evidence_ready'])
        self.assertIn('stress_3s_mean_positive',result['failed_evidence_checks'])

    def test_small_sample_never_passes(self):
        small=dict(self.good);small['valued_attempts']=99
        result=gate(small,dict(self.good),{'forward_distinct_windows':299,'forward_utc_days':6},self.registry)
        self.assertFalse(result['human_review_evidence_ready'])


if __name__=='__main__':unittest.main()
