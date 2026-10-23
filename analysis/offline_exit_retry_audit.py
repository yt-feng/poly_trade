"""Bounded saved-signal comparison of one pooled original and scheduled-exit ledger.

Only nine causal saved candidate fields reach execution. No model is constructed,
fitted or scored; quote outcomes remain historical simulation evidence.
"""
from __future__ import annotations
import argparse, base64, csv, hashlib, io, json, lzma, math, os, re, stat, subprocess, sys
from pathlib import Path
from typing import Any, Mapping, Sequence
import pandas as pd

HERE=Path(__file__).resolve().parent
if str(HERE) not in sys.path:sys.path.insert(0,str(HERE))
import cached_quote_execution as execution
import offline_bid_residual_audit as book_base
import offline_side_interaction_audit as fingerprints

CANDIDATE_FIELDS=tuple(fingerprints.CANDIDATE_FIELDS)
ARMS=('C_original','C_scheduled_retry')
HARD_RAW={'max_selected_runs':96,'max_quote_files':4096,'max_quote_rows':4000000,'max_quote_bytes':1073741824}
MAX_CANDIDATE_BYTES=262144
REQUIRED_CODE_FILES=frozenset({'analysis/offline_exit_retry_audit.py','analysis/scheduled_exit_retry.py','analysis/cached_quote_execution.py',
    'analysis/offline_bid_residual_audit.py','analysis/timed_quote_execution.py','analysis/offline_side_interaction_audit.py'})
OUTPUTS={'summary.json','baseline-ledger.json','retry-ledger.json','exit-attempts.jsonl','quote-witness.jsonl','source-manifest.json','input-receipt.json'}


def require(value: Any,code: str)->None:
    if not value:raise ValueError(code)

def canonical(value: Any)->bytes:return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(raw: bytes)->str:return hashlib.sha256(raw).hexdigest()
def valid_hash(value: Any,n: int=64)->bool:return isinstance(value,str) and re.fullmatch('[0-9a-f]{'+str(n)+'}',value) is not None

def stable(before: Any,after: Any)->bool:
    return all(getattr(before,k)==getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_nlink'))

def read_bounded(path: Path,maximum: int)->bytes:
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    with os.fdopen(fd,'rb') as handle:
        before=os.fstat(handle.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and 0<before.st_size<=maximum,'bounded_regular_input_required')
        raw=handle.read(maximum+1)
        require(len(raw)==before.st_size and stable(before,os.fstat(handle.fileno())),'input_changed_during_read')
    return raw

def _object(pairs: list)->dict:
    result={}
    for key,value in pairs:require(key not in result,'duplicate_json_key');result[key]=value
    return result

def load_json(raw: bytes)->dict:
    value=json.loads(raw,object_pairs_hook=_object,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite_json')))
    require(isinstance(value,dict),'json_object_required');return value

def _time(value: Any)->pd.Timestamp:
    require(isinstance(value,(str,pd.Timestamp)),'timestamp_required')
    stamp=pd.Timestamp(value)
    require(not pd.isna(stamp) and stamp.tzinfo is not None,'timezone_aware_timestamp_required')
    return stamp.tz_convert('UTC')

def _number(value: Any)->float:
    require(not isinstance(value,bool),'numeric_boolean_forbidden')
    number=float(value);require(math.isfinite(number),'finite_number_required');return number

def decode_candidate_packet(encoded: str,binding: Mapping)->bytes:
    require(isinstance(encoded,str) and 0<len(encoded)<=45000,'candidate_encoded_size_limit')
    packed=base64.b64decode(encoded,validate=True);require(len(packed)<=49152,'candidate_packed_size_limit')
    limit=binding['bytes'];require(type(limit) is int and 0<limit<=MAX_CANDIDATE_BYTES,'candidate_expanded_size_limit')
    decoder=lzma.LZMADecompressor(format=lzma.FORMAT_XZ,memlimit=64*1024*1024)
    raw=decoder.decompress(packed,max_length=limit+1)
    require(len(raw)==limit and decoder.eof and not decoder.unused_data,'candidate_xz_boundary_mismatch')
    require(digest(raw)==binding['sha256'],'candidate_packet_sha256_mismatch');return raw

def validate_config(config: dict)->dict:
    require(config.get('evaluation_mode')=='offline_exit_retry','exit_retry_mode_required')
    for key in ('source_commit','expected_code_commit'):require(valid_hash(config.get(key),40),'commit_binding_required')
    require(isinstance(config.get('source_repository'),str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',config['source_repository']),'source_repository_required')
    parent=book_base.validate_config(config['base_protocol'])
    require(parent['source_commit']==config['source_commit'] and digest(canonical(parent))==config.get('base_protocol_sha256'),'base_protocol_binding_mismatch')
    require(config.get('actual_model_fit_calls')==0 and type(config['actual_model_fit_calls']) is int
        and config.get('actual_model_predict_calls')==0 and type(config['actual_model_predict_calls']) is int
        and config.get('max_producer_replays')==2 and type(config['max_producer_replays']) is int
        and config.get('live_orders_enabled') is False,'model_free_two_replay_budget_required')
    require(valid_hash(config.get('expected_original_pooled_fingerprint')),'baseline_fingerprint_required')
    binding=config.get('candidate_binding',{})
    require(type(binding.get('bytes')) is int and 0<binding['bytes']<=MAX_CANDIDATE_BYTES and valid_hash(binding.get('sha256'))
        and type(binding.get('expected_rows')) is int and 0<binding['expected_rows']<=96
        and isinstance(binding.get('expected_header'),list) and all(isinstance(x,str) and x for x in binding['expected_header']) and len(set(binding['expected_header']))==len(binding['expected_header'])
        and set(CANDIDATE_FIELDS).issubset(binding['expected_header']),'candidate_binding_required')
    require(type(config.get('expected_control_candidate_count')) is int and config['expected_control_candidate_count']==binding['expected_rows'] and config.get('frozen_control_candidate_fields')==list(CANDIDATE_FIELDS),'candidate_count_or_projection_mismatch')
    frozen=config.get('frozen_control_candidates')
    require(isinstance(frozen,list) and len(frozen)==binding['expected_rows']
        and all(isinstance(r,dict) and set(r)==set(CANDIDATE_FIELDS) and all(isinstance(v,str) for v in r.values()) for r in frozen)
        and digest(canonical(frozen))==config.get('frozen_control_candidate_rows_sha256'),'frozen_manifest_binding_mismatch')
    budget=config.get('raw_budget',{})
    require(set(budget)==set(HARD_RAW) and all(type(budget[k]) is int and 0<budget[k]<=v for k,v in HARD_RAW.items()),'raw_budget_required')
    policy=config.get('retry_policy',{})
    require(set(policy)=={'reaction_delay_seconds','order_latency_seconds','max_quote_delay_seconds','deadline_after_original_target_seconds','max_extra_attempts'},'retry_policy_fields_required')
    for name in set(policy)-{'max_extra_attempts'}:require(_number(policy[name])>=0,'invalid_retry_clock')
    require(type(policy['max_extra_attempts']) is int and 0<policy['max_extra_attempts']<=2
        and policy['reaction_delay_seconds']>0 and policy['deadline_after_original_target_seconds']>0
        and policy['order_latency_seconds']==parent['latency_seconds'] and policy['max_quote_delay_seconds']==parent['max_quote_delay_seconds'],'retry_policy_protocol_mismatch')
    hashes=config.get('code_file_sha256')
    require(isinstance(hashes,dict) and REQUIRED_CODE_FILES.issubset(hashes) and all(isinstance(k,str) and not Path(k).is_absolute() and '..' not in Path(k).parts and valid_hash(v) for k,v in hashes.items()),'code_hash_bindings_required')
    return config

def load_frozen_candidates(path: Path,binding: Mapping,frozen_rows: Sequence[Mapping[str,str]])->pd.DataFrame:
    raw=read_bounded(path,MAX_CANDIDATE_BYTES)
    require(len(raw)==binding['bytes'] and digest(raw)==binding['sha256'],'candidate_bytes_binding_mismatch')
    reader=csv.reader(io.StringIO(raw.decode('utf-8'),newline=''),strict=True)
    header=next(reader,None);require(header==binding['expected_header'] and len(header)==len(set(header)) and set(CANDIDATE_FIELDS).issubset(header),'candidate_exact_header_mismatch')
    rows=[]
    for values in reader:
        require(len(values)==len(header) and len(rows)<binding['expected_rows'],'candidate_csv_shape_mismatch')
        rows.append(dict(zip(header,values)))
    require(len(rows)==binding['expected_rows'] and len(frozen_rows)==len(rows),'candidate_row_count_mismatch')
    causal=[]
    for raw_row,frozen in zip(rows,frozen_rows):
        row={k:raw_row[k] for k in CANDIDATE_FIELDS}
        for name in CANDIDATE_FIELDS:
            if name in ('decision_ts','market_close_ts'):
                require(_time(row[name])==_time(frozen[name]),'candidate_manifest_clock_mismatch');row[name]=_time(row[name])
            elif name in ('decision_ask_price','decision_sell_size','predicted_exit_bid_price'):
                row[name]=_number(row[name]);require(round(row[name],10)==round(_number(frozen[name]),10),'candidate_manifest_numeric_mismatch')
            else:require(row[name]==frozen[name],'candidate_manifest_identity_mismatch')
        require(row['side'] in ('up','down') and row['market_id']==row['run_name']+'::'+row['slug']
            and re.fullmatch(r'btc-updown-5m-[0-9]{10}',row['slug']) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',row['run_name'])
            and row['decision_ts']<row['market_close_ts'] and 0<row['decision_ask_price']<1
            and 0<=row['predicted_exit_bid_price']<=1 and row['decision_sell_size']>=0,'candidate_domain_invalid')
        causal.append(row)
    frame=pd.DataFrame(causal,columns=CANDIDATE_FIELDS)
    require(not frame['market_id'].duplicated().any() and not frame['slug'].duplicated().any(),'candidate_identity_duplicate')
    return frame

class RawQuoteSource:
    def __init__(self,books: Mapping[str,pd.DataFrame],files: Sequence[Mapping],conflicts: Mapping|None=None):
        self.books=dict(books);self.files=list(files);self.conflicts=dict(conflicts or {});self.phase='unassigned';self.requests=[];self.witness=[];self.witness_bytes=0
    def witness_for_attempt(self,market_id: str,arrival_ts: Any,window_end_ts: Any)->list[dict]:
        book=self.books.get(market_id)
        if book is None:return []
        start,end=_time(arrival_ts),_time(window_end_ts)
        found=book[book['ts_utc'].ge(start)&book['ts_utc'].le(end)]
        return [{'market_id':market_id,'ts_utc':r.ts_utc.isoformat(),'source_file':str(r.source_file),
            'quote_conflict':bool(r.quote_conflict),'conflict_evidence':self.conflicts.get((market_id,r.ts_utc.value),[]),**{k:float(getattr(r,k)) if pd.notna(getattr(r,k)) and math.isfinite(float(getattr(r,k))) else None for k in book_base.BOOK_NUMERIC}}
            for r in found.itertuples(index=False)]
    def select(self,market_id: str,target_ts: Any,close_ts: Any,*,side: str,kind: str,max_quote_delay_seconds: float):
        target,close=_time(target_ts),_time(close_ts);end=min(target+pd.Timedelta(seconds=max_quote_delay_seconds),close)
        quote,reason=book_base.execution.select_book_quote(self.books.get(market_id),target,close,side=side,kind=kind,max_quote_delay_seconds=max_quote_delay_seconds)
        request={'request_index':len(self.requests),'arm':self.phase,'market_id':market_id,'kind':kind,'side':side,
            'arrival_ts':target.isoformat(),'window_end_ts':end.isoformat(),'window_end_inclusive':end<close,
            'quote_status':reason,'selected_quote_ts':None if quote is None else _time(quote['ts_utc']).isoformat()}
        self.requests.append(request)
        for row in self.witness_for_attempt(market_id,target,end):
            if _time(row['ts_utc'])<close:
                evidence={**request,'quote':row};size=len(canonical(evidence))+1
                require(self.witness_bytes+size<=16*1024*1024,'quote_witness_byte_budget')
                self.witness.append(evidence);self.witness_bytes+=size
        require(len(self.witness)<=100000,'quote_witness_row_budget')
        return quote,reason

def load_candidate_quote_books(source_root: Path,candidates: pd.DataFrame,parent: Mapping,budget: Mapping,*,deadline_after_original_target_seconds: float)->tuple[RawQuoteSource,dict]:
    require(_number(deadline_after_original_target_seconds)>0,'retention_deadline_required')
    selected={r:[row for row in candidates.to_dict('records') if row['run_name']==r] for r in parent['run_order'] if r in set(candidates['run_name'])}
    require(set(candidates['run_name']).issubset(parent['run_order'][parent['minimum_training_runs']:]) and len(selected)<=budget['max_selected_runs'],'candidate_run_scope_mismatch')
    require(source_root.is_dir() and not source_root.is_symlink(),'raw_source_directory_required')
    pieces={};first={};files=[];scanned_rows=scanned_bytes=retained_count=0
    for run,rows in selected.items():
        directory=source_root/run
        require(directory.is_dir() and not directory.is_symlink() and directory.resolve().is_relative_to(source_root.resolve()),'missing_or_invalid_source_run')
        paths=sorted(directory.glob(book_base.FILE_GLOB));require(len(paths)==parent['expected_files_per_run'],'source_file_count_mismatch')
        bounds={r['slug']:(r['decision_ts']+pd.Timedelta(seconds=parent['entry_delay_seconds']+parent['latency_seconds']),
            min(r['market_close_ts'],r['decision_ts']+pd.Timedelta(seconds=parent['entry_delay_seconds']+2*parent['latency_seconds']+parent['max_quote_delay_seconds']+parent['hold_seconds']+max(deadline_after_original_target_seconds,parent['max_quote_delay_seconds'])))) for r in rows}
        for path in paths:
            require(len(files)<budget['max_quote_files'] and path.resolve().is_relative_to(directory.resolve()),'source_file_budget_or_path')
            fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0));hash_value=hashlib.sha256();count=kept=total=0
            with os.fdopen(fd,'rb') as handle:
                before=os.fstat(handle.fileno());require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_size>0,'source_regular_file_required')
                require(scanned_bytes+before.st_size<=budget['max_quote_bytes'],'source_byte_budget')
                def lines():
                    nonlocal total
                    for line in handle:
                        require(len(line)<=1048576,'source_line_budget');total+=len(line);require(total<=before.st_size,'source_grew_during_read');hash_value.update(line)
                        yield line.decode('utf-8')
                reader=csv.DictReader(lines(),strict=True);header=reader.fieldnames
                require(header is not None and len(header)==len(set(header)) and {'slug','ts_iso',*book_base.BOOK_NUMERIC}.issubset(header),'source_exact_schema_required')
                for raw in reader:
                    count+=1;scanned_rows+=1;require(scanned_rows<=budget['max_quote_rows'] and None not in raw and all(v is not None for v in raw.values()),'source_row_budget_or_shape')
                    slug=raw['slug'];require(re.fullmatch(r'btc-updown-5m-[0-9]{10}',slug),'source_slug_identity_invalid')
                    stamp=_time(raw['ts_iso'])
                    if slug not in bounds:continue
                    require('run_name' not in raw or raw['run_name']==run,'source_run_identity_conflict')
                    key=run+'::'+slug;record={'slug':slug,'ts_iso':raw['ts_iso'],'source_file':str(path.relative_to(source_root)),**{k:raw[k] for k in book_base.BOOK_NUMERIC}}
                    if key not in first:first[key]=record
                    lo,hi=bounds[slug]
                    if lo<=stamp<=hi:pieces.setdefault(key,[]).append(record);kept+=1;retained_count+=1
                    require(retained_count<=100000,'retained_quote_row_budget')
                require(total==before.st_size and stable(before,os.fstat(handle.fileno())),'source_changed_during_read')
            scanned_bytes+=total;files.append({'path':str(path.relative_to(source_root)),'run_name':run,'bytes':total,'sha256':hash_value.hexdigest(),'scanned_rows':count,'retained_rows':kept})
    books={};conflicts={}
    for key in set(candidates['market_id']):
        data=list(pieces.get(key,[]))
        if key in first and first[key] not in data:data.append(first[key])
        if data:
            books[key]=book_base.normalize_book_rows(pd.DataFrame(data))
            for stamp in books[key].loc[books[key]['quote_conflict'],'ts_utc']:
                conflicts[(key,stamp.value)]=[{'source_file':r['source_file'],'ts_iso':r['ts_iso'],'raw_values':{k:r[k] for k in book_base.BOOK_NUMERIC}} for r in data if _time(r['ts_iso'])==stamp]
    manifest={'format':'bounded-candidate-raw-source-v1','files':files,'scanned_files':len(files),'scanned_rows':scanned_rows,'scanned_bytes':scanned_bytes,
        'selected_runs':list(selected),'retained_rows':sum(len(v) for v in pieces.values()),'normalization':'offline_bid_residual_audit.normalize_book_rows; per candidate market with conflicts retained',
        'book_presence_anchors':'One original raw row per present market retained outside window if necessary, solely to preserve original missing_book versus no_quote_in_window behavior.'}
    return RawQuoteSource(books,files,conflicts),manifest

class ExitEvidenceCollector:
    """Keep every treatment original attempt without altering ledger fields.

    The resolver owns all policy decisions. An unchanged original resolution has
    no event trace by design; its already selected quote is projected here using
    the same observation serializer, without a selector call or policy replay.
    """
    def __init__(self,resolver):
        self.resolver=resolver;self.attempt_evidence=[];self.original_projection_calls=0
    def __call__(self,request,original_quote,original_reason,source):
        resolution=self.resolver(request,original_quote,original_reason,source)
        trace=list(resolution.trace)
        if not trace:
            from scheduled_exit_retry import _observation,_trace
            target=request.original_exit_target_ts
            observation=_observation(request,original_quote,original_reason,target,request.market_close_ts)
            trace=[_trace(request,observation,original_reason,0,target-pd.Timedelta(seconds=request.order_latency_seconds),
                target,request.market_close_ts,None)]
            self.original_projection_calls+=1
        self.attempt_evidence.extend({'market_id':request.market_id,**row} for row in trace)
        require(len(self.attempt_evidence)<=30,'exit_attempt_row_budget')
        return resolution


def pooled_wrapper(result: dict,parent: Mapping)->dict:
    return {'scope':'pooled_single_ledger','block_index':0,'runs':parent['run_order'][parent['minimum_training_runs']:],
        'slippage':parent['slippage'],'latency_seconds':parent['latency_seconds'],'depth_haircut':parent['depth_haircut'],**result}

def replay_kwargs(parent: Mapping)->dict:
    names=('hold_seconds','entry_delay_seconds','latency_seconds','max_quote_delay_seconds','edge_floor','fee_rate','slippage','depth_haircut',
        'cash_release_delay_after_close_seconds','initial_cash','minimum_order_usd','minimum_order_shares','required_fills')
    return {**{k:parent[k] for k in names},'thresholds':tuple(parent['depth_thresholds'])}

def cash_time_readout(ledger: Mapping,parent: Mapping)->dict:
    def seconds(later,earlier):return (later.value-earlier.value)/1000000000
    chain=ledger['chain'];positions=[];previous_release=None
    for event in chain:
        entry=_time(event['entry_ts']) if event.get('entry_ts') else None;exit_=_time(event['exit_ts']) if event.get('exit_ts') else None
        release=_time(event['cash_release_ts']) if event.get('cash_release_ts') else None
        require(exit_ is None or entry is None or exit_>=entry,'exit_precedes_entry')
        require(release is None or exit_ is not None and release>=exit_,'release_precedes_exit')
        idle=None if previous_release is None or entry is None else seconds(entry,previous_release)
        require(idle is None or idle>0,'inter_position_cash_overlap')
        positions.append({'market_id':event['market_id'],'entry_ts':None if entry is None else entry.isoformat(),'exit_ts':None if exit_ is None else exit_.isoformat(),
            'assumed_cash_release_ts':None if release is None else release.isoformat(),'holding_seconds':None if exit_ is None or entry is None else seconds(exit_,entry),
            'assumed_funds_unavailable_seconds':None if release is None or entry is None else seconds(release,entry),'idle_after_previous_assumed_release_seconds':idle})
        previous_release=release
    complete=len(chain)==parent['required_fills'] and all(e['status']=='timed_quote_exit' for e in chain) and ledger['locked_shares']==0
    first=positions[0]['entry_ts'] if positions else None;tenth=positions[-1] if complete else None
    elapsed=seconds(_time(tenth['assumed_cash_release_ts']),_time(first)) if tenth and first and tenth['assumed_cash_release_ts'] else None
    return {'complete_first_ten_resolved':bool(complete),'positions':positions,'first_entry_ts':first,
        'tenth_exit_ts':None if tenth is None else tenth['exit_ts'],'tenth_assumed_cash_release_ts':None if tenth is None else tenth['assumed_cash_release_ts'],
        'first_ten_ending_cash':ledger['ending_cash'] if complete else None,'first_ten_cash_multiple':ledger['ending_cash']/parent['initial_cash'] if complete else None,
        'elapsed_first_entry_to_tenth_assumed_release_seconds':elapsed,'elapsed_status':'complete_assumed_cash_clock' if elapsed is not None else 'incomplete_or_unknown',
        'ending_liquid_cash':ledger['ending_cash'],'locked_shares':ledger['locked_shares'],
        'basis':'Saved simulated clocks; assumed close-plus-delay cash release, not actual authenticated settlement or continuous market sampling.'}

def json_safe(value: Any)->Any:
    """Output-only unknown numeric representation; never used before accounting.

    The baseline fingerprint still sees the original finite-round10 ledger.
    Unknown depth remains null in transport, never zero or a liquidation value.
    """
    if isinstance(value,dict):return {str(k):json_safe(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [json_safe(v) for v in value]
    if value is None or isinstance(value,(str,bool,int)):return value
    if isinstance(value,float):return value if math.isfinite(value) else None
    if isinstance(value,pd.Timestamp):return value.isoformat()
    if hasattr(value,'item'):return json_safe(value.item())
    raise TypeError('unsupported_result_value')


def _write(path: Path,value: Any,jsonl: bool=False)->None:
    raw=b''.join(canonical(json_safe(row))+b'\n' for row in value) if jsonl else canonical(json_safe(value))+b'\n'
    limits={'summary.json':1048576,'baseline-ledger.json':1048576,'retry-ledger.json':1048576,'exit-attempts.jsonl':1048576,
        'quote-witness.jsonl':16777216,'source-manifest.json':2097152,'input-receipt.json':262144}
    require(path.name in OUTPUTS and len(raw)<=limits[path.name],'result_member_size_or_name_limit')
    current=sum(p.stat().st_size for p in path.parent.iterdir() if p.is_file())
    require(current+len(raw)<=25165824,'result_aggregate_size_limit')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as handle:handle.write(raw);handle.flush();os.fsync(handle.fileno())

def verified_code_source(source_root: Path,config: Mapping)->dict:
    def git(path: Path,args: list[str])->str:return subprocess.check_output(['git','-C',str(path),*args],text=True,stderr=subprocess.DEVNULL).strip()
    source_head=git(source_root,['rev-parse','HEAD']);code_head=git(HERE.parent,['rev-parse','HEAD'])
    require(source_head==config['source_commit'] and code_head==config['expected_code_commit'],'immutable_commit_mismatch')
    require(not git(source_root,['status','--porcelain','--untracked-files=no']),'dirty_immutable_source')
    observed={name:digest(read_bounded(HERE.parent/name,1048576)) for name in config['code_file_sha256']}
    require(observed==config['code_file_sha256'],'code_file_hash_mismatch')
    return {'source_commit':source_head,'code_commit':code_head,'code_file_sha256':observed}

def evaluate(source_root: Path,candidate_path: Path,config: dict,output: Path)->dict:
    output=Path(output);output.mkdir(parents=True,exist_ok=True,mode=0o700)
    require(not output.is_symlink() and not any(output.iterdir()),'empty_private_output_required');output.chmod(0o700)
    summary={'format':'offline-exit-retry-audit-v1','evaluation_status':'failed','strategy_evaluated':False,'comparison_valid':False,'control_execution_reproduced':False,
        'input_verified':False,'actual_model_fit_calls':0,'actual_model_predict_calls':0,'replayed_cells':0,'producer_replayed_cells':0,
        'producer_replay_completions':0,'candidate_rows':None,'actual_fills':0,'verified_canary_fills':0,'live_orders_enabled':False,'new_data_collected':False,'reconstruction_counters':{'model':0,'producer_ledger_replays':0,'independent_audit_reconstructions':None,'execution_evidence_original_projections':0}}
    stage='configuration';failure=None
    try:
        validate_config(config);summary['protocol_sha256']=digest(canonical(config));summary['protocol']=config
        stage='immutable_inputs';summary.update(verified_code_source(Path(source_root),config))
        candidates=load_frozen_candidates(Path(candidate_path),config['candidate_binding'],config['frozen_control_candidates']);parent=config['base_protocol']
        summary['candidate_rows']=len(candidates);summary['candidate_clock_coverage']={'first_decision_ts':candidates['decision_ts'].min().isoformat(),'last_decision_ts':candidates['decision_ts'].max().isoformat(),
            'primary_runs':parent['run_order'][parent['minimum_training_runs']:],'candidate_runs':list(dict.fromkeys(candidates['run_name'])),'continuous_five_minute_coverage_inferred':False}
        source,manifest=load_candidate_quote_books(Path(source_root),candidates,parent,config['raw_budget'],deadline_after_original_target_seconds=config['retry_policy']['deadline_after_original_target_seconds']);_write(output/'source-manifest.json',manifest)
        receipt={'format':'exit-retry-input-receipt-v1','status':'complete','candidate_bytes_verified':True,'source_byte_hashes_verified':True,'candidate_binding':config['candidate_binding'],'causal_fields':list(CANDIDATE_FIELDS),'future_fields_excluded':True,
            'base_protocol_sha256':config['base_protocol_sha256'],'source_manifest_sha256':digest(canonical(manifest)+b'\n')}
        _write(output/'input-receipt.json',receipt);summary['input_verified']=True
        runs=parent['run_order'][parent['minimum_training_runs']:];kwargs=replay_kwargs(parent)
        stage='baseline';source.phase=ARMS[0];summary['replayed_cells']=summary['producer_replayed_cells']=1;summary['reconstruction_counters']['producer_ledger_replays']=1
        baseline=pooled_wrapper(execution.replay_cached_quotes(candidates,runs,source,**kwargs),parent);summary['producer_replay_completions']=1;_write(output/'baseline-ledger.json',baseline)
        observed=fingerprints.result_fingerprint(baseline);summary['baseline_full_fingerprint']=observed
        require(observed==config['expected_original_pooled_fingerprint'],'baseline_full_fingerprint_mismatch');summary['control_execution_reproduced']=True
        stage='scheduled_retry'
        from scheduled_exit_retry import RetryPolicy,ScheduledExitResolver
        resolver=ExitEvidenceCollector(ScheduledExitResolver(RetryPolicy(**config['retry_policy'])));source.phase=ARMS[1]
        summary['replayed_cells']=summary['producer_replayed_cells']=2;summary['reconstruction_counters']['producer_ledger_replays']=2
        treatment=pooled_wrapper(execution.replay_cached_quotes(candidates,runs,source,exit_resolver=resolver,**kwargs),parent);summary['producer_replay_completions']=2;_write(output/'retry-ledger.json',treatment)
        attempts=resolver.attempt_evidence
        summary['reconstruction_counters']['execution_evidence_original_projections']=resolver.original_projection_calls
        require(len(attempts)<=30,'exit_attempt_row_budget')
        _write(output/'exit-attempts.jsonl',attempts,True)
        summary['arms']={ARMS[0]:baseline,ARMS[1]:treatment};summary['cash_time_readouts']={arm:cash_time_readout(ledger,parent) for arm,ledger in summary['arms'].items()}
        summary['quote_request_counts']={arm:{'requests':sum(r['arm']==arm for r in source.requests),'unavailable':sum(r['arm']==arm and r['quote_status']!='selected' for r in source.requests),
            'by_saved_selector_status':{status:sum(r['arm']==arm and r['quote_status']==status for r in source.requests) for status in ('selected','target_at_or_after_close','missing_book','no_quote_in_window','unknown_quote_conflict','invalid_quote_price')}} for arm in ARMS}
        summary.update(evaluation_status='evaluated',strategy_evaluated=True,comparison_valid=True,economic_pass={arm:bool(value['strict_net_growth_ten']) for arm,value in summary['arms'].items()})
    except Exception as exc:
        failure=exc;summary['failure']={'stage':stage,'type':type(exc).__name__,'code':str(exc)}
    finally:
        if 'resolver' in locals():summary['reconstruction_counters']['execution_evidence_original_projections']=resolver.original_projection_calls
        if 'source' in locals():
            _write(output/'quote-witness.jsonl',source.witness,True);summary['quote_requests']=source.requests
        _write(output/'summary.json',summary)
    if failure is not None:raise ValueError('offline_exit_retry_evaluation_failed') from None
    return summary

def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source-root','candidate-path','config','output'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args();config=load_json(read_bounded(Path(args.config),262144))
    result=evaluate(Path(args.source_root),Path(args.candidate_path),config,Path(args.output))
    return 0 if result['evaluation_status']=='evaluated' else 1

if __name__=='__main__':raise SystemExit(main())
