"""Continue a frozen saved-signal policy on its original evolving cash ledger.

The original required prefix is verified in-loop before the next candidate.
Prefix finalization is a report snapshot, never a cash credit or account reset.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys
from typing import Any, Mapping

HERE=Path(__file__).resolve().parent
if str(HERE) not in sys.path:sys.path.insert(0,str(HERE))
import offline_exit_retry_audit as prior

ARM='C_scheduled_retry_fullstream'
REQUIRED_CODE_FILES=prior.REQUIRED_CODE_FILES|{'analysis/offline_exit_continuation_audit.py'}
LIMITS={'summary.json':1048576,'prefix-ledger.json':1048576,'full-ledger.json':1048576,'exit-attempts.jsonl':1048576,
    'quote-witness.jsonl':16777216,'source-manifest.json':2097152,'input-receipt.json':262144}
OUTPUTS=frozenset(LIMITS)
MAX_ATTEMPTS=150


def validate_config(config: dict)->dict:
    prior.require(config.get('evaluation_mode')=='offline_exit_continuation','continuation_mode_required')
    parent=config.get('parent_strategy_config');prior.require(isinstance(parent,dict),'frozen_parent_strategy_required')
    prior.require(prior.digest(prior.canonical(parent))==config.get('parent_strategy_config_sha256'),'parent_strategy_hash_mismatch')
    # Parent code/reader metadata is historical data. Its exact config is checked,
    # but only the outer current implementation is used for live code integrity.
    prior.validate_config(parent)
    for key in ('source_commit','source_repository','candidate_binding'):
        prior.require(config.get(key)==parent[key],'outer_parent_input_binding_mismatch')
    prior.require(parent['expected_control_candidate_count']<=50,'continuation_candidate_budget')
    prior.require(prior.valid_hash(config.get('expected_code_commit'),40),'current_code_commit_required')
    hashes=config.get('code_file_sha256')
    prior.require(isinstance(hashes,dict) and REQUIRED_CODE_FILES.issubset(hashes)
        and all(isinstance(k,str) and not Path(k).is_absolute() and '..' not in Path(k).parts and prior.valid_hash(v) for k,v in hashes.items()),
        'current_code_hash_bindings_required')
    prior.require(type(config.get('max_producer_replays')) is int and config['max_producer_replays']==1
        and type(config.get('actual_model_fit_calls')) is int and config['actual_model_fit_calls']==0
        and type(config.get('actual_model_predict_calls')) is int and config['actual_model_predict_calls']==0
        and config.get('live_orders_enabled') is False,'one_model_free_fullstream_required')
    prefix=config.get('prefix_binding')
    prior.require(isinstance(prefix,dict) and set(prefix)=={'frozen_pooled_result','expected_pooled_fingerprint','parent_retry_ledger_bytes','parent_retry_ledger_sha256'},'prefix_binding_required')
    frozen=prefix['frozen_pooled_result'];base=parent['base_protocol']
    prior.require(isinstance(frozen,dict) and frozen.get('scope')=='pooled_single_ledger' and type(frozen.get('block_index')) is int
        and frozen['block_index']==0 and frozen.get('runs')==base['run_order'][base['minimum_training_runs']:]
        and frozen.get('slippage')==base['slippage'] and frozen.get('latency_seconds')==base['latency_seconds']
        and frozen.get('depth_haircut')==base['depth_haircut'],'prefix_scope_binding_mismatch')
    prior.require(frozen.get('fills')==base['required_fills'] and frozen.get('resolved_fills')==base['required_fills']
        and frozen.get('locked_shares')==0 and frozen.get('ten_resolved_sequence_complete') is True,'complete_parent_prefix_required')
    prior.require(prior.valid_hash(prefix['expected_pooled_fingerprint'])
        and prior.fingerprints.result_fingerprint(frozen)==prefix['expected_pooled_fingerprint'],'prefix_fingerprint_binding_mismatch')
    raw=prior.canonical(prior.json_safe(frozen))+b'\n'
    prior.require(type(prefix['parent_retry_ledger_bytes']) is int and 0<prefix['parent_retry_ledger_bytes']<=1048576
        and len(raw)==prefix['parent_retry_ledger_bytes'] and prior.digest(raw)==prefix['parent_retry_ledger_sha256'],'prefix_byte_proof_mismatch')
    return config


class ContinuationEvidenceCollector:
    """Reuse the frozen per-request serializer with a larger bounded row packet.

    A fresh observation collector per callback only resets annotation storage;
    the same policy resolver and the engine's single cash/pending state persist.
    """
    def __init__(self,resolver,maximum_attempts: int):
        prior.require(type(maximum_attempts) is int and 0<maximum_attempts<=MAX_ATTEMPTS,'exit_attempt_budget_required')
        self.resolver=resolver;self.maximum_attempts=maximum_attempts;self.attempt_evidence=[];self.original_projection_calls=0
    def __call__(self,request,original_quote,original_reason,source):
        single=prior.ExitEvidenceCollector(self.resolver)
        resolution=single(request,original_quote,original_reason,source)
        prior.require(len(self.attempt_evidence)+len(single.attempt_evidence)<=self.maximum_attempts,'exit_attempt_row_budget')
        self.attempt_evidence.extend(single.attempt_evidence);self.original_projection_calls+=single.original_projection_calls
        return resolution


def write_artifact(path: Path,value: Any,*,jsonl: bool=False)->None:
    raw=b''.join(prior.canonical(prior.json_safe(row))+b'\n' for row in value) if jsonl else prior.canonical(prior.json_safe(value))+b'\n'
    prior.require(path.name in OUTPUTS and len(raw)<=LIMITS[path.name],'result_member_size_or_name_limit')
    prior.require(sum(p.stat().st_size for p in path.parent.iterdir() if p.is_file())+len(raw)<=25165824,'result_aggregate_size_limit')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as handle:handle.write(raw);handle.flush();os.fsync(handle.fileno())


def cash_time_readouts(full: Mapping,prefix: Mapping,base: Mapping)->dict:
    prefix_readout=prior.cash_time_readout(prefix,base)
    # Reuse only the shared per-position clock arithmetic for the longer ledger.
    # Its original len==required first-ten shortcut is deliberately not reused.
    positions=prior.cash_time_readout(full,base)['positions']
    n=base['required_fills'];chain=full['chain'];tail=chain[n:]
    all_resolved=bool(chain) and all(e['status']=='timed_quote_exit' for e in chain) and full['locked_shares']==0
    fully_resolved=bool(all_resolved and full['candidate_stream_exhausted'] and full['continuation_failure_reason'] is None)
    first=positions[0]['entry_ts'] if positions else None
    last=positions[-1] if positions else None
    release=last['assumed_cash_release_ts'] if last else None
    elapsed=(prior._time(release).value-prior._time(first).value)/1000000000 if fully_resolved and first and release else None
    full_readout={'positions':positions,'fully_resolved':fully_resolved,'first_entry_ts':first,
        'last_exit_ts':last['exit_ts'] if last else None,'last_assumed_cash_release_ts':release,
        'elapsed_first_entry_to_final_assumed_release_seconds':elapsed,
        'elapsed_status':'complete_assumed_cash_clock' if elapsed is not None else 'incomplete_or_unknown',
        'ending_liquid_cash':full['ending_cash'],'locked_shares':full['locked_shares'],
        'basis':'One original evolving ledger; saved simulated clocks and assumed close-plus-delay cash, not actual settlement or continuous five-minute sampling.'}
    tail_readout={'entered_positions_after_prefix':len(tail),'resolved_positions_after_prefix':sum(e['status']=='timed_quote_exit' for e in tail),
        'unresolved_positions_after_prefix':sum(e['status']=='unresolved_position' for e in tail),'positions':positions[n:],
        'prefix_assumed_cash':prefix['ending_cash'],'full_ending_liquid_cash':full['ending_cash'],
        'liquid_cash_difference_vs_prefix_not_total_wealth':full['ending_cash']-prefix['ending_cash'],
        'locked_shares':full['locked_shares'],'no_reset_or_topup':True,
        'basis':'Annotation of events after the prefix within the same ledger; not a standalone tail account or inventory valuation.'}
    return {'prefix':prefix_readout,'full':full_readout,'tail':tail_readout}


def evaluate(source_root: Path,candidate_path: Path,config: dict,output: Path)->dict:
    output=Path(output);output.mkdir(parents=True,exist_ok=True,mode=0o700)
    prior.require(not output.is_symlink() and not any(output.iterdir()),'empty_private_output_required');output.chmod(0o700)
    summary={'format':'offline-exit-continuation-audit-v1','evaluation_status':'failed','strategy_evaluated':False,'comparison_valid':False,
        'prefix_control_reproduced':False,'control_execution_reproduced':False,'input_verified':False,'runtime_environment':None,
        'actual_model_fit_calls':0,'actual_model_predict_calls':0,'replayed_cells':0,'producer_replayed_cells':0,'producer_replay_completions':0,
        'candidate_rows':None,'actual_fills':0,'verified_canary_fills':0,'live_orders_enabled':False,'new_data_collected':False,'new_policy_hypotheses':0,
        'reconstruction_counters':{'model':0,'producer_ledger_replays':0,'independent_audit_reconstructions':None,'execution_evidence_original_projections':0}}
    stage='configuration';failure=None;prefix_calls=0;captured=None
    try:
        validate_config(config);parent=config['parent_strategy_config'];base=parent['base_protocol']
        summary.update(protocol=config,protocol_sha256=prior.digest(prior.canonical(config)))
        stage='runtime_environment';summary['runtime_environment']=prior.probe_runtime_environment();prior.assert_runtime_environment(parent,summary['runtime_environment'])
        stage='immutable_inputs';summary.update(prior.verified_code_source(Path(source_root),config))
        candidates=prior.load_frozen_candidates(Path(candidate_path),parent['candidate_binding'],parent['frozen_control_candidates'])
        summary['candidate_rows']=len(candidates);summary['candidate_clock_coverage']={'first_decision_ts':candidates['decision_ts'].min().isoformat(),
            'last_decision_ts':candidates['decision_ts'].max().isoformat(),'primary_runs':base['run_order'][base['minimum_training_runs']:],
            'candidate_runs':list(dict.fromkeys(candidates['run_name'])),'continuous_five_minute_coverage_inferred':False}
        source,manifest=prior.load_candidate_quote_books(Path(source_root),candidates,base,parent['raw_budget'],
            deadline_after_original_target_seconds=parent['retry_policy']['deadline_after_original_target_seconds'])
        write_artifact(output/'source-manifest.json',manifest)
        write_artifact(output/'input-receipt.json',{'format':'exit-continuation-input-receipt-v1','status':'complete',
            'candidate_binding':parent['candidate_binding'],'candidate_bytes_verified':True,'source_byte_hashes_verified':True,
            'causal_fields':list(prior.CANDIDATE_FIELDS),'future_fields_excluded':True,'base_protocol_sha256':parent['base_protocol_sha256'],
            'parent_strategy_config_sha256':config['parent_strategy_config_sha256'],'source_manifest_sha256':prior.digest(prior.canonical(manifest)+b'\n')})
        summary['input_verified']=True
        def prefix_observer(original_result):
            nonlocal prefix_calls,captured
            prefix_calls+=1;prior.require(prefix_calls==1,'duplicate_required_prefix_observer')
            captured=prior.pooled_wrapper(original_result,base)
            write_artifact(output/'prefix-ledger.json',captured)
            actual=prior.fingerprints.result_fingerprint(captured);expected=config['prefix_binding']
            summary['prefix_full_fingerprint']=actual
            prior.require(actual==expected['expected_pooled_fingerprint']
                and prior.fingerprints.normalized_result(captured)==prior.fingerprints.normalized_result(expected['frozen_pooled_result']),
                'required_prefix_full_fingerprint_mismatch')
            summary['prefix_control_reproduced']=summary['control_execution_reproduced']=True
            summary['prefix_guard_selector_request_count']=len(source.requests)
            summary['prefix_guard_processed_candidate_count']=len(original_result['chain'])+len(original_result['attempts'])
        from scheduled_exit_retry import RetryPolicy,ScheduledExitResolver
        resolver=ContinuationEvidenceCollector(ScheduledExitResolver(RetryPolicy(**parent['retry_policy'])),len(candidates)*(1+parent['retry_policy']['max_extra_attempts']))
        stage='fullstream_with_required_prefix_guard';source.phase=ARM
        summary['replayed_cells']=summary['producer_replayed_cells']=1;summary['reconstruction_counters']['producer_ledger_replays']=1
        raw=prior.execution.replay_cached_quotes(candidates,base['run_order'][base['minimum_training_runs']:],source,
            exit_resolver=resolver,continue_after_required_fills=True,required_prefix_observer=prefix_observer,**prior.replay_kwargs(base))
        summary['producer_replay_completions']=1
        full=prior.pooled_wrapper(raw,base);write_artifact(output/'full-ledger.json',full)
        prior.require(prefix_calls==1 and captured is not None and raw.get('required_prefix_result') is not None,'required_prefix_not_reproduced')
        prior.require(prior.fingerprints.result_fingerprint(prior.pooled_wrapper(raw['required_prefix_result'],base))==summary['prefix_full_fingerprint'],
            'returned_prefix_snapshot_mismatch')
        processed=raw.get('processed_candidate_count');prefix_processed=raw.get('required_prefix_processed_candidate_count')
        prior.require(type(processed) is int and type(prefix_processed) is int and prefix_processed==summary['prefix_guard_processed_candidate_count']
            and prefix_processed<=processed<=len(candidates) and processed==len(raw['chain'])+len(raw['attempts'])
            and raw.get('candidate_stream_count')==len(candidates) and type(raw.get('candidate_stream_exhausted')) is bool
            and raw['candidate_stream_exhausted']==(processed==len(candidates)),'processed_candidate_conservation_failure')
        write_artifact(output/'exit-attempts.jsonl',resolver.attempt_evidence,jsonl=True)
        summary.update(arms={ARM:full},prefix_ledger=captured,full_ledger=full,processed_candidate_count=processed,
            required_prefix_processed_candidate_count=prefix_processed,unprocessed_candidate_count=len(candidates)-processed,
            candidate_stream_exhausted=raw['candidate_stream_exhausted'],full_terminal_failure_reason=raw['continuation_failure_reason'],
            prefix_economic_pass=bool(captured['strict_net_growth_ten']),
            full_economic_pass=bool(raw['full_sequence_net_growth']),
            full_net_growth=bool(raw['full_sequence_net_growth']) if raw['full_sequence_resolved'] else None,
            cash_time_readouts=cash_time_readouts(full,captured,base))
        summary['quote_request_counts']={'requests':len(source.requests),'by_saved_selector_status':{status:sum(r['quote_status']==status for r in source.requests)
            for status in ('selected','target_at_or_after_close','missing_book','no_quote_in_window','unknown_quote_conflict','invalid_quote_price')}}
        summary.update(evaluation_status='evaluated',strategy_evaluated=True,comparison_valid=True)
    except Exception as exc:
        failure=exc;summary['failure']={'stage':stage,'type':type(exc).__name__,'code':str(exc)}
    finally:
        if 'resolver' in locals():
            summary['reconstruction_counters']['execution_evidence_original_projections']=resolver.original_projection_calls
            if not (output/'exit-attempts.jsonl').exists():write_artifact(output/'exit-attempts.jsonl',resolver.attempt_evidence,jsonl=True)
        if 'source' in locals():
            write_artifact(output/'quote-witness.jsonl',source.witness,jsonl=True);summary['quote_requests']=source.requests
        write_artifact(output/'summary.json',summary)
    if failure is not None:raise ValueError('offline_exit_continuation_evaluation_failed') from None
    return summary


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source-root','candidate-path','config','output'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args();config=prior.load_json(prior.read_bounded(Path(args.config),262144))
    evaluate(Path(args.source_root),Path(args.candidate_path),config,Path(args.output));return 0

if __name__=='__main__':raise SystemExit(main())
