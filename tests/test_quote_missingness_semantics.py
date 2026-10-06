import unittest
from analysis import quote_missingness_semantics as audit


def row(**changes):
    r={'sell_up_cents':'','sell_up_size':'','level_count_bid_up':'0','bid_depth_up_5':'','mid_up_cents':'','spread_up_cents':''}
    r.update(changes);return r


class MissingnessSemanticsTests(unittest.TestCase):
    def test_compatible_empty_parse_never_claims_venue_empty(self):
        label,fields=audit.signature(row(),'up')
        self.assertEqual(label,'compatible_with_no_parsed_bid_levels_ambiguous_cause')
        self.assertEqual(fields['levels'],'zero')

    def test_missing_columns_not_treated_as_observed_zero_levels(self):
        self.assertEqual(audit.signature({},'up')[0],'bid_and_metadata_blank_ambiguous_generation')

    def test_zero_bid_and_size_are_not_blank(self):
        label,fields=audit.signature(row(sell_up_cents='0.00',sell_up_size='0.00'),'up')
        self.assertEqual(label,'bid_zero');self.assertEqual(fields['size'],'zero')

    def test_nonfinite_invalid_and_empty_are_distinct(self):
        self.assertEqual([audit.cell_kind(x) for x in ('nan','bad','',None,'0','-1','2')],
                         ['nonfinite_or_unparseable','nonfinite_or_unparseable','blank','blank','zero','negative','positive'])

    def test_positive_metadata_with_blank_bid_is_not_empty_parse_signature(self):
        self.assertEqual(audit.signature(row(level_count_bid_up='1'),'up')[0],'blank_bid_with_other_metadata')

    def test_adjacent_recovery_preserves_gaps_and_no_interpolation(self):
        rows=[{'ts':t,'values':(51,b,49,48)} for t,b in ((1,40),(3,None),(5,None),(10,41))]
        sources={('s',t*1000000,'up'):{'kind':'blank'} for t in (3,5)}
        e=audit.adjacent_episodes(rows,'s','up',sources)[0]
        self.assertEqual((e['unavailable_rows'],e['previous_bid'],e['next_bid']),(2,40,41))
        self.assertEqual((e['previous_gap_seconds'],e['next_gap_seconds']),(2,5))
        self.assertFalse(e['is_tail_of_recorded_window'])

    def test_whole_recorded_window_unavailable_is_not_market_closed(self):
        rows=[{'ts':t,'values':(51,None,49,48)} for t in (1,2)]
        sources={('s',t*1000000,'up'):{'kind':'blank'} for t in (1,2)}
        e=audit.adjacent_episodes(rows,'s','up',sources)[0]
        self.assertTrue(e['is_entire_recorded_window']);self.assertIsNone(e['next_bid'])
        self.assertNotIn('market_closed',e)

    def test_unknown_source_evidence_fails_instead_of_inventing_blank(self):
        with self.assertRaises(KeyError):audit.adjacent_episodes([{'ts':1,'values':(51,None,49,48)}],'s','up',{})


if __name__=='__main__':unittest.main()
