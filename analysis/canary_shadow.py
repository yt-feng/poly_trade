"""Streaming shadow diagnostics only. Accepts snapshot JSONL; sends no orders."""
import argparse,json,sys
from pathlib import Path
from canary_core import CausalSignals

def stream(source,destination):
    engine=CausalSignals();attempted=set();count=0
    for line in source:
        if not line.strip():continue
        row=json.loads(line)
        if row.get('asset')!='btc':continue
        f=engine.update(row);direction=f['directions']['roll_veto'];proposal=None
        if direction and row['slug']not in attempted:
            attempted.add(row['slug']);side='up'if direction>0 else'down';ask=row[side]['ask']
            proposal={'type':'shadow_candidate_not_order','hypothesis':'roll_veto','hold_seconds':30,
              'side':side,'shares':5,'decision_ask':ask,'price_ceiling':min(.99,ask+f['tick_size']),
              'virtual_exposure_ceiling_usd':5,'expiry_ms':row['sample_ms']+5000,
              'selection_status':'post_exploration_shortlist_requires_forward_validation'}
        f.update(proposal=proposal,live_enabled=False,private_execution_reconciled=False)
        destination.write(json.dumps(f,ensure_ascii=False,allow_nan=False)+'\n');destination.flush();count+=1
    return count

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('w')as out:
        if a.input=='-':stream(sys.stdin,out)
        else:
            with open(a.input)as src:stream(src,out)
