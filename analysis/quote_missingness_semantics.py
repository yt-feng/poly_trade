"""Describe raw CSV missing-bid signatures and adjacent records, never fills."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path

try:
    from archive_coverage_ledger import connect_readonly, inventory, load_dates, utc_us, verify_sources
    from archive_baseline_audit import number, market_start, write_private
    from historical_walk_forward import file_hash
except ImportError:
    from analysis.archive_coverage_ledger import connect_readonly, inventory, load_dates, utc_us, verify_sources
    from analysis.archive_baseline_audit import number, market_start, write_private
    from analysis.historical_walk_forward import file_hash


def cell_kind(value):
    if value is None or not str(value).strip():return 'blank'
    x=number(value)
    if x is None:return 'nonfinite_or_unparseable'
    return 'zero' if x==0 else 'negative' if x<0 else 'positive'


def signature(row, side):
    bid=cell_kind(row.get('sell_'+side+'_cents'))
    fields={'bid':bid,'size':cell_kind(row.get('sell_'+side+'_size')),
            'levels':cell_kind(row.get('level_count_bid_'+side)),
            'depth5':cell_kind(row.get('bid_depth_'+side+'_5')),
            'mid':cell_kind(row.get('mid_'+side+'_cents')),
            'spread':cell_kind(row.get('spread_'+side+'_cents'))}
    if bid!='blank':label='bid_'+bid
    elif all(fields[x]=='blank' for x in ('size','depth5','mid','spread')) and fields['levels']=='zero':
        label='compatible_with_no_parsed_bid_levels_ambiguous_cause'
    elif all(fields[x]=='blank' for x in ('size','depth5','levels')):
        label='bid_and_metadata_blank_ambiguous_generation'
    else:label='blank_bid_with_other_metadata'
    return label,fields


def adjacent_episodes(rows, slug, side, missing_sources):
    """Maximal runs of numeric-unavailable bids; no interpolation or relabeling."""
    i=1 if side=='up' else 3;out=[];n=0
    while n<len(rows):
        if rows[n]['values'][i] is not None:
            n+=1;continue
        first=n
        while n<len(rows) and rows[n]['values'][i] is None:n+=1
        last=n-1;before=rows[first-1] if first else None;after=rows[n] if n<len(rows) else None
        sources=[missing_sources[(slug,round(r['ts']*1e6),side)] for r in rows[first:n]]
        out.append({'market':slug,'side':side,'first_ts':rows[first]['ts'],'last_ts':rows[last]['ts'],
          'unavailable_rows':n-first,'raw_kinds':dict(Counter(s['kind'] for s in sources)),
          'previous_ts':before['ts'] if before else None,'previous_bid':before['values'][i] if before else None,
          'next_ts':after['ts'] if after else None,'next_bid':after['values'][i] if after else None,
          'previous_gap_seconds':rows[first]['ts']-before['ts'] if before else None,
          'next_gap_seconds':after['ts']-rows[last]['ts'] if after else None,
          'observed_span_seconds':rows[last]['ts']-rows[first]['ts'],
          'is_tail_of_recorded_window':after is None,'is_entire_recorded_window':first==0 and after is None,
          'first_source':sources[0],'last_source':sources[-1]})
    return out


def audit(root, database, protocol):
    if file_hash(database)!=protocol['database_sha256']:raise ValueError('database_changed')
    repo=Path(__file__).resolve().parents[1]
    for path,sha in protocol['code_sha256'].items():
        if file_hash(repo/path)!=sha:raise ValueError('audit_code_changed')
    for path,sha in protocol['poly_code_sha256'].items():
        if file_hash(Path(root)/path)!=sha:raise ValueError('collector_code_changed')
    counts=defaultdict(Counter);panels=defaultdict(Counter);missing={};file_headers=Counter();row_total=0;windows=set()
    with connect_readonly(database) as db:
        files=inventory(db);verify_sources(root,files)
        for info in files:
            reader=csv.DictReader(io.StringIO((Path(root)/info['path']).read_text(encoding='utf-8-sig')))
            file_headers[json.dumps(reader.fieldnames)]+=1
            for line,row in enumerate(reader,2):
                row_total+=1;slug,start=market_start(row);ts=utc_us(row.get('ts_iso'))
                canonical=start is not None and ts is not None and start*1000000<=ts<(start+300)*1000000
                if start is not None:windows.add(slug)
                date=datetime.fromtimestamp(start,timezone.utc).date().isoformat() if start is not None else 'unknown'
                bucket=str((ts//1000000-start)//30*30) if canonical else 'outside_or_invalid_time'
                for side in ('up','down'):
                    label,fields=signature(row,side)
                    for target in (counts[side],panels[side+'/date/'+date],panels[side+'/offset/'+bucket]):
                        target['rows']+=1;target[label]+=1;target['canonical_time_rows']+=int(canonical)
                        for field,kind in fields.items():target[field+':'+kind]+=1
                        if fields['bid']=='blank':
                            for field,kind in fields.items():target['blank_bid_'+field+':'+kind]+=1
                            other='down' if side=='up' else 'up'
                            target['blank_bid_opposite_ask:'+cell_kind(row.get('buy_'+other+'_cents'))]+=1
                    if canonical and fields['bid'] in ('blank','nonfinite_or_unparseable'):
                        key=(slug,ts,side)
                        value={'file':info['path'],'csv_line':line,'file_sha256':info['sha256'],'kind':fields['bid'],'signature':label}
                        if key in missing:raise ValueError('duplicate_unavailable_bid_key')
                        missing[key]=value
        episodes=[]
        dates=[x[0] for x in db.execute("SELECT DISTINCT strftime('%Y-%m-%d',start,'unixepoch') FROM windows ORDER BY 1")]
        for date in dates:
            grouped,pc,dc,_=load_dates(db,[date])
            if pc or dc:raise ValueError('conflicting_rows_need_separate_audit')
            for slug,rows in grouped.items():
                for side in ('up','down'):episodes.extend(adjacent_episodes(rows,slug,side,missing))
        if sum(e['unavailable_rows'] for e in episodes)!=len(missing):raise ValueError('unavailable_row_coverage_mismatch')
        verify_sources(root,files)
    debug=[]
    for item in protocol['debug_inventory']:
        path=Path(root)/item['path']
        if file_hash(path)!=item['sha256']:raise ValueError('debug_source_changed')
        obj=json.loads(path.read_text());market=obj.get('raw_market',{})
        debug.append({**item,'slug':obj.get('slug'),'recorded_time':obj.get('ts_iso'),
            'matches_csv_window':obj.get('slug') in windows,
            'has_order_book_payload':any(k in obj for k in ('book','bids','asks','http_status')),
            'cached_market_flags':{k:market.get(k) for k in ('active','closed','acceptingOrders','enableOrderBook')},
            'role':'conditional market/reference debug; not a contemporaneous CLOB attempt log'})
    ep_summary={}
    for side in ('up','down'):
        group=[e for e in episodes if e['side']==side]
        ep_summary[side]={'episodes':len(group),'unavailable_rows':sum(e['unavailable_rows'] for e in group),
            'episodes_with_later_numeric_bid':sum(e['next_ts'] is not None for e in group),
            'tail_episodes':sum(e['is_tail_of_recorded_window'] for e in group),
            'entire_recorded_window_episodes':sum(e['is_entire_recorded_window'] for e in group),
            'rows_in_tail_episodes':sum(e['unavailable_rows'] for e in group if e['is_tail_of_recorded_window'])}
    return {'scope':'historical observation-generation audit, not a strategy evaluation',
        'protocol':protocol,'file_count':len(files),'raw_rows':row_total,'file_header_patterns':dict(file_headers),
        'counts':dict(counts),'panels':dict(panels),'episode_summary':ep_summary,'episodes':episodes,'debug_files':debug,
        'raw_inventory':files,'pnl':None,'promotion_allowed':False,'canary_gate_changed':False,
        'limits':['Signature compatibility does not identify the raw HTTP payload or the true venue cause.',
          'Repeated values, tail gaps and cached market flags do not authenticate freshness, closure or fills.',
          'Numeric recovery denotes a later recorded value, never an imputed exit or a fill.']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for n in ('poly-root','database','protocol','output'):parser.add_argument('--'+n,type=Path,required=True)
    a=parser.parse_args(argv);result=audit(a.poly_root,a.database,json.loads(a.protocol.read_text()))
    result['protocol_sha256']=file_hash(a.protocol);write_private(a.output,result)
    print('PRIVATE_BID_SEMANTICS_AUDIT_COMPLETE; no empirical results printed.')


if __name__=='__main__':main()
