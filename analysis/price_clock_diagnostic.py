"""Small state-conditioned quote hypothesis; no resolution labels or trading.

New fits use only complete training markets. Legacy fits are read-only controls.
Time means scheduled window time, not authenticated venue resolution time.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path

try:
    import historical_walk_forward as walk
    import quote_robustness as robustness
    from archive_coverage_ledger import connect_readonly, inventory, load_dates, verify_sources, utc_us
    from archive_baseline_audit import write_private
    from quote_factor_diagnostic import fit, predict
except ImportError:
    from analysis import historical_walk_forward as walk, quote_robustness as robustness
    from analysis.archive_coverage_ledger import connect_readonly, inventory, load_dates, verify_sources, utc_us
    from analysis.archive_baseline_audit import write_private
    from analysis.quote_factor_diagnostic import fit, predict


MODELS = {'level': ['spread', 'level'],
          'clock': ['spread', 'level', 'level_clock', 'momentum_boundary_clock']}
TARGETS = ('bid_change_cents', 'ask_change_cents')


def validate_plan(p):
    if (p['decision_offsets'] != [60,120,240] or p['new_models'] != MODELS or
            p['entry_steps'] != [0,1] or p['new_signal_buffer_cents'] != 1 or
            p['training_policy'] != 'all_states_labeled_complete_market' or
            p['selection'] != 'all_registered_no_validation_selection'):
        raise ValueError('unregistered_family_or_timing')
    for offset in p['decision_offsets']:
        old = dict(p['prior_plan'], diagnostic=dict(p['prior_plan']['diagnostic'], decision_offset_seconds=offset))
        walk.validate_plan(old)
    if len(p['folds']) != len(p['prior_plan']['folds']) or p['folds'] != p['prior_plan']['folds']:
        raise ValueError('changed_chronological_folds')
    for c in p['cost_scenarios']:
        if c['historical_actual_cost'] is not False or any(not math.isfinite(c[k]) or c[k]<0 for k in ('fee_cents_per_share_per_leg','slippage_cents_per_share_per_leg')):
            raise ValueError('invalid_assumed_cost')
    if ([c['id'] for c in p['cost_scenarios']] != ['zero','assumed_fee','assumed_fee_slippage'] or
            [(c['fee_cents_per_share_per_leg'],c['slippage_cents_per_share_per_leg']) for c in p['cost_scenarios']] != [(0,0),(.5,0),(.5,.5)]):
        raise ValueError('changed_cost_scenarios')


def state_features(current, lag, side, offset, horizon):
    i = 0 if side == 'up' else 2
    ask,bid = current['values'][i:i+2]
    old_ask,old_bid = lag['values'][i:i+2]
    probability_proxy = (ask+bid)/200
    clock = horizon/(300-offset)
    level = 2*probability_proxy-1
    momentum = (ask+bid-old_ask-old_bid)/2
    return {'spread':ask-bid, 'level':level, 'level_clock':level*clock,
            'momentum_boundary_clock':momentum*4*probability_proxy*(1-probability_proxy)*clock}


def sample(rows, market, start, date, offset, cfg, present=True, price_conflict=False, depth_conflict=False):
    cfg = dict(cfg, decision_offset_seconds=offset)
    original = walk.sample_window(rows,market,start,date,cfg,'depth',[0,20,40,60,80,100],
                                  present,price_conflict,depth_conflict)
    r = {'market':market,'date':date,'start':start,'offset':offset,'decision_time':start+offset,
         'status':original['status'],'feature_eligible':original['features'] is not None,
         'eligible':False,'original':original,'state_features':{},'side_targets':{},'paths':{},
         'current_prices':{},'signals':{},'predictions':{},'raw_predictions':{}}
    if market != f'btc-updown-5m-{start}' or start % 300 or date != datetime.fromtimestamp(start,timezone.utc).date().isoformat():
        raise ValueError('invalid_market_identity')
    times = [q['ts'] for q in rows]
    if times != sorted(set(times)) or any(not start <= t < start+300 for t in times):
        raise ValueError('duplicate_or_cross_window_index')
    if not r['feature_eligible']:
        return r
    decision = start+offset
    current_idx = bisect.bisect_left(times,decision)-1
    lag_idx = bisect.bisect_right(times,decision-cfg['lookback_seconds'])-1
    current,lag = rows[current_idx],rows[lag_idx]
    if [lag['ts'],current['ts']] != original['feature_times']:
        raise ValueError('causal_feature_index_mismatch')
    exit_time = decision+cfg['horizon_seconds']
    future_idx = bisect.bisect_left(times,exit_time)
    future = rows[future_idx] if future_idx < len(rows) else None
    future_reason = ('exit_missing_or_late' if future is None or future['ts']>exit_time+cfg['future_tolerance_seconds'] else future['price_reason'])
    if (future_reason is None) != (original['targets'] is not None):
        raise ValueError('label_index_mismatch')
    later_idx = bisect.bisect_right(times,decision)
    later = rows[later_idx] if later_idx < len(rows) else None
    r.update(feature_times=original['feature_times'],target_time=original.get('target_time'),
             scheduled_exit_time=exit_time,observed_exit_time=future['ts'] if future else None)
    for side,i in (('up',0),('down',2)):
        r['state_features'][side] = state_features(current,lag,side,offset,cfg['horizon_seconds'])
        r['current_prices'][side] = {'ask':current['values'][i],'bid':current['values'][i+1]}
        r['side_targets'][side] = ({'bid_change_cents':future['values'][i+1]-current['values'][i+1],
                                  'ask_change_cents':future['values'][i]-current['values'][i]}
                                 if future_reason is None else None)
        r['paths'][side] = {}
        for step,entry in (('0',current),('1',later)):
            entry_reason = ('entry_missing' if entry is None else 'entry_at_or_after_fixed_exit'
                            if entry['ts']>=exit_time else entry['price_reason'])
            a = entry['values'][i] if entry_reason is None else None
            b = future['values'][i+1] if future_reason is None else None
            reasons = [str(x) for x in (entry_reason,future_reason) if x]
            depth_reasons = list(reasons)
            for role,q,idxs in (('decision',current,[i+4,i+5]),('entry',entry,[i+4]),('exit',future,[i+5])):
                if q is None or any(q['values'][j] is None or not math.isfinite(q['values'][j]) or q['values'][j]<=0 for j in idxs):
                    depth_reasons.append(role+'_missing_or_nonpositive_depth')
            r['paths'][side][step] = {'ask':a,'bid':b,'entry_time':entry['ts'] if entry else None,
                'entry_delay_recorded_seconds':entry['ts']-decision if entry else None,
                'price_only':{'known':not reasons,'unknown_reasons':reasons},
                'positive_depth':{'known':not depth_reasons,'unknown_reasons':depth_reasons}}
    return r


def load_samples(db, dates, p):
    cfg = p['prior_plan']['diagnostic']; result=[]
    for date in sorted(set(dates)):
        grouped,pc,dc,present=load_dates(db,[date])
        first=utc_us(date+'T00:00:00Z')//1000000
        for start in range(first,first+86400,300):
            market=f'btc-updown-5m-{start}'
            for offset in p['decision_offsets']:
                result.append(sample(grouped.get(market,[]),market,start,date,offset,cfg,market in present,market in pc,market in dc))
    return result


def training_rows(rows, dates, offsets, cutoff):
    groups=defaultdict(list)
    for r in rows:
        if r['date'] in dates:groups[r['market']].append(r)
    complete=[]
    for group in groups.values():
        if sorted(r['offset'] for r in group)!=offsets:
            raise ValueError('duplicate_or_missing_training_state')
        if all(r['feature_eligible'] and r['side_targets']['up'] is not None for r in group):
            if any(max(r['feature_times'])>=r['decision_time'] or r['target_time']>=cutoff for r in group):
                raise ValueError('training_label_or_feature_after_cutoff')
            complete.extend(group)
    return complete, {'scheduled_markets':len(groups),'complete_markets':len(complete)//len(offsets),
                      'excluded_incomplete_markets':len(groups)-len(complete)//len(offsets),'training_rows':len(complete)}


def fit_new(rows, penalty):
    return {side:{target:{name:fit([{'features':r['state_features'][side],'targets':r['side_targets'][side]} for r in rows],
                target,fields,penalty) for name,fields in MODELS.items()} for target in TARGETS} for side in ('up','down')}


def predict_new(model, features, current, target):
    raw=predict(model,{'features':features})
    field='bid' if target=='bid_change_cents' else 'ask'
    return raw, min(100-current[field],max(-current[field],raw))


def populate(r,new_fits,legacy_fits,buffer):
    r['eligible']=r['feature_eligible'] and bool(new_fits) and bool(legacy_fits)
    if not r['eligible']:return
    old={target:{name:predict(f,r['original']) for name,f in fits.items()} for target,fits in legacy_fits.items()}
    for side in ('up','down'):
        r['predictions'][side]={};r['raw_predictions'][side]={}
        for target in TARGETS:
            predictions={};raws={}
            for name,model in new_fits[side][target].items():
                raw,point=predict_new(model,r['state_features'][side],r['current_prices'][side],target)
                predictions['state_'+name]=point;raws['state_'+name]=raw
            for name in legacy_fits['bid_change_cents']:
                # Preserve prior Down directional hypothesis, not synthetic Down prices.
                opposite='ask_change_cents' if target=='bid_change_cents' else 'bid_change_cents'
                predictions['legacy_'+name]=old[target][name] if side=='up' else -old[opposite][name]
                raws['legacy_'+name]=predictions['legacy_'+name]
            r['predictions'][side][target]=predictions;r['raw_predictions'][side][target]=raws
        spread=r['current_prices'][side]['ask']-r['current_prices'][side]['bid']
        r['signals'][side]={name:value>spread+(buffer if name.startswith('state_') else 0)
                           for name,value in r['predictions'][side]['bid_change_cents'].items()}
        r['signals'][side].update(always_same_side=True,no_signal=False)


def auxiliary(rows,side,names):
    eligible=[r for r in rows if r['eligible']]
    known=[r for r in eligible if r['side_targets'][side] is not None]
    unknown=[r for r in eligible if r['side_targets'][side] is None]
    result={'scheduled_slots':len(rows),'unique_markets':len({r['market'] for r in rows}),
            'eligible_slots':len(eligible),'known_slots':len(known),'unknown_slots':len(unknown),'targets':{}}
    for target in TARGETS:
        out={};result['targets'][target]=out
        for name in names:
            errors=[r['predictions'][side][target][name]-r['side_targets'][side][target] for r in known]
            raw_errors=[r['raw_predictions'][side][target][name]-r['side_targets'][side][target] for r in known]
            zeros=[r['side_targets'][side][target] for r in known]
            worst=0
            for r in unknown:
                field='bid' if target=='bid_change_cents' else 'ask'
                current=r['current_prices'][side][field];prediction=r['predictions'][side][target][name]
                worst+=max((prediction-current_end+current)**2 for current_end in (0,100))
            ss=sum(e*e for e in errors)
            out[name]={'mse':ss/len(known) if known else None,
                       'raw_mse':sum(e*e for e in raw_errors)/len(known) if known else None,
                       'mean_squared_error_improvement_vs_zero':sum(y*y-e*e for y,e in zip(zeros,errors))/len(known) if known else None,
                       'unknown_mse_upper':(ss+worst)/len(eligible) if eligible else None,
                       'bounded_projection_count':sum(r['predictions'][side][target][name]!=r['raw_predictions'][side][target][name] for r in eligible)}
    return result


def primary_fold_economics(rows, names, side):
    known=[r for r in rows if r['eligible'] and r['paths'][side]['1']['positive_depth']['known']]
    eligible=[r for r in rows if r['eligible']]
    result={'scheduled':len(rows),'known':len(known),'unknown':len(eligible)-len(known),'policies':{}}
    for name in list(names)+['always_same_side','no_signal']:
        selected=[r for r in eligible if r['signals'][side][name]]
        observed=[r for r in known if r['signals'][side][name]]
        values=[r['paths'][side]['1']['bid']-r['paths'][side]['1']['ask']-1 for r in observed]
        lo=hi=0
        for r in selected:
            a,b=robustness.bounds(r['paths'][side]['1'],'positive_depth',1);lo+=a;hi+=b
        result['policies'][name]={'selected':len(selected),'observed':len(observed),'unknown':len(selected)-len(observed),
            'mean':sum(values)/len(values) if values else None,'lower':lo/len(selected) if selected else None,
            'upper':hi/len(selected) if selected else None,'shared_activity_mean':sum(values)/len(known) if known else None}
    return result


def screening(summary, folds):
    """Descriptive predeclared checks, never automatic selection or promotion."""
    result={}
    for side,offsets in summary.items():
        result[side]={}
        for name in ('state_level','state_clock'):
            cells={}
            for offset,views in offsets.items():
                p=views['positive_depth']['1']['assumed_fee']['policies'][name]
                mean=p['observed_distribution']['mean']
                days=p['leave_one_out_no_refit']['day']['groups']
                active=[g for g in days.values() if g['removed_observed_selected']]
                positive=sum(g['removed_contribution']>0 for g in active)
                fold_values=[f['primary_economic'][side][offset]['policies'][name]['mean'] for f in folds]
                active_folds=[v for v in fold_values if v is not None]
                checks={'at_least_3_active_folds':len(active_folds)>=3,
                        'majority_active_folds_positive':bool(active_folds) and sum(v>0 for v in active_folds)>len(active_folds)/2,
                        'observed_mean_positive':mean is not None and mean>0,
                        'missingness_lower_positive':p['selected_lower'] is not None and p['selected_lower']>0,
                        'at_least_100_observed':p['observed_selected']>=100,
                        'at_least_7_active_dates':len(active)>=7,
                        'majority_active_dates_positive':bool(active) and positive>len(active)/2,
                        'every_single_day_removal_positive':p['leave_one_out_no_refit']['day']['min_retained_mean'] is not None and p['leave_one_out_no_refit']['day']['min_retained_mean']>0}
                cells[offset]={'checks':checks,'passes_all':all(checks.values()),'active_dates':len(active),'positive_dates':positive,'fold_means':fold_values}
            result[side][name]={'by_offset':cells,'passes_all_registered_states':all(c['passes_all'] for c in cells.values()),'promotion_allowed':False}
    return result


def run(database,root,legacy_file,p):
    validate_plan(p)
    if walk.file_hash(database)!=p['database_sha256'] or walk.file_hash(legacy_file)!=p['legacy_file_sha256']:
        raise ValueError('frozen_input_changed')
    for path,sha in p['code_sha256'].items():
        if walk.file_hash(Path(__file__).resolve().parents[1]/path)!=sha:raise ValueError('frozen_code_changed')
    old=json.loads(Path(legacy_file).read_text())
    if old['plan']!=p['prior_plan']:raise ValueError('prior_plan_changed')
    legacy=old['cohorts']['depth'];names=['state_'+n for n in MODELS]+['legacy_'+n for n in legacy['models']]
    dates=sorted({d for f in p['folds'] for key in ('train_dates','validation_dates') for d in f[key]})
    with connect_readonly(database) as db:
        files=inventory(db);verify_sources(root,files)
        samples=load_samples(db,dates,p)
        evaluated=[];folds=[];seen=set()
        reference={r['market']:r for r in legacy['window_ledger']}
        for index,f in enumerate(p['folds']):
            cutoff=utc_us(min(f['validation_dates'])+'T00:00:00Z')/1e6
            train,coverage=training_rows(samples,set(f['train_dates']),p['decision_offsets'],cutoff)
            fits=fit_new(train,p['prior_plan']['diagnostic']['ridge_penalty']) if coverage['complete_markets']>=p['minimum_training_markets'] else {}
            legacy_fold=legacy['folds'][index]
            if any(legacy_fold[k]!=f[k] for k in ('train_dates','validation_dates')):raise ValueError('legacy_fold_changed')
            validation=[dict(r,fold=index) for r in samples if r['date'] in f['validation_dates']]
            for r in validation:
                key=(r['market'],r['offset'])
                if key in seen:raise ValueError('validation_market_state_repeated')
                seen.add(key)
                populate(r,fits,legacy_fold['fits'],p['new_signal_buffer_cents'])
                if r['offset']==120:
                    previous=reference[r['market']]
                    for key in ('features','feature_times','targets','target_time','status'):
                        if r['original'].get(key)!=previous.get(key):raise ValueError('original_control_sample_changed')
                    if r['eligible']:
                        for target,values in previous['predictions'].items():
                            if any(r['predictions']['up'][target]['legacy_'+n]!=value for n,value in values.items()):
                                raise ValueError('original_control_prediction_changed')
            evaluated.extend(validation)
            folds.append({'fold':index,**f,'training':coverage,'new_fits':fits,'legacy_fits_unchanged':True,
                'primary_economic':{side:{str(o):primary_fold_economics([r for r in validation if r['offset']==o],names,side) for o in p['decision_offsets']} for side in ('up','down')},
                'by_offset':{str(o):{side:auxiliary([r for r in validation if r['offset']==o],side,names) for side in ('up','down')} for o in p['decision_offsets']}})
        summaries={};metrics={}
        for side in ('up','down'):
            summaries[side]={};metrics[side]={}
            for offset in p['decision_offsets']:
                rows=[r for r in evaluated if r['offset']==offset]
                metrics[side][str(offset)]=auxiliary(rows,side,names)
                summaries[side][str(offset)]={view:{str(step):{c['id']:robustness.summarize(rows,names,side,str(step),view,c)
                    for c in p['cost_scenarios']} for step in p['entry_steps']} for view in ('price_only','positive_depth')}
        verify_sources(root,files)
    return {'protocol':p,'scope':'exposed historical state hypothesis; quote proxies only','folds':folds,
            'summaries':summaries,'auxiliary_mse':metrics,'screen':screening(summaries,folds),'window_ledger':evaluated,
            'legacy_refit':False,'main_changed':False,'canary_gate_changed':False,'promotion_allowed':False,'pnl':None,
            'limitations':['Scheduled time-to-window-end is not verified resolution time.',
              'Midpoint is a feature proxy, never an executable entry price or authenticated probability.',
              'All states of a market share the chronological split; they are not independent observations or simultaneous portfolio trades.',
              'Actual Up and Down future quotes only; no official opening price, settlement or complementary Down label.',
              'Legacy fits and gates are unchanged; transfer to other states is diagnostic, with the original state checked exactly.',
              'Source/receive availability, actual fees, minimum size and fills remain unverified.']}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('database','poly-root','legacy','protocol','output'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args(argv);result=run(a.database,a.poly_root,a.legacy,json.loads(a.protocol.read_text()))
    result['protocol_sha256']=walk.file_hash(a.protocol);write_private(a.output,result)
    print('PRIVATE_STATE_DIAGNOSTIC_COMPLETE; no empirical contents printed.')


if __name__=='__main__':main()
