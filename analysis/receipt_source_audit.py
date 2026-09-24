"""Read-only AST audit of the legacy runner; never imports or executes it."""
from __future__ import annotations
import argparse, ast, hashlib, json
from pathlib import Path

def functions(text):
    tree=ast.parse(text)
    return {n.name:n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}

def calls(node,name):
    return [n for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr==name]

def audit(root:Path)->dict:
    files={p:(root/p).read_text(encoding='utf-8') for p in ('analysis/live_paper_trader.py','analysis/live_execution.py')}
    runner=functions(files['analysis/live_paper_trader.py']); adapter=functions(files['analysis/live_execution.py']); findings=[]
    def add(code,node,description):
        findings.append({'code':code,'line':node.lineno,'end_line':node.end_lineno,'evidence':description,'scope':'source pattern, not actual account diagnosis'})
    record=runner.get('record_signal')
    if record:
        if any(isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Attribute) and n.value.func.attr=='mark_slug_traded' for n in record.body):
            add('ATTEMPT_MARKED_AS_TRADED',record,'Top-level mark_slug_traded also executes for rejected/blocked plans; no confirmed-fill predicate.')
    accepted=runner.get('record_live_acceptance')
    if accepted and 'live_order_ok' in ast.unparse(accepted):
        add('ACCEPTANCE_NOT_CONFIRMED_FILL',accepted,'Daily count is updated from order acceptance rather than confirmed trade receipts.')
    settle=runner.get('settle_positions')
    if settle and 'last_final_by_slug' in ast.unparse(settle) and 'bankroll' in ast.unparse(settle):
        add('REFERENCE_PRICE_PAPER_SETTLEMENT',settle,'Reference-price comparison changes simulated bankroll; not an official payout or cash receipt.')
    pending=runner.get('process_pending')
    if pending and not any(isinstance(n,ast.Compare) and 'slug' in ast.unparse(n) for n in ast.walk(pending)):
        add('NO_PENDING_SNAPSHOT_MARKET_CHECK',pending,'No slug comparison before pending intents consume the latest snapshot.')
    price=adapter.get('price_text')
    if price and ('0.01' in ast.unparse(price) or '.01' in ast.unparse(price)):
        add('HARDCODED_PRICE_GRID',price,'Price rounding contains a fixed cent grid rather than a current market tick argument.')
    buy_plan=adapter.get('build_buy_market_plan')
    if buy_plan:
        plan_text=ast.unparse(buy_plan).lower()
        net_share_markers=('net_shares_after_fee','net_exitable_shares','buy_fee_shares')
        if 'min_order_size' in plan_text and 'estimated_shares' in plan_text and not any(x in plan_text for x in net_share_markers):
            add('BUY_FEE_NET_SHARES_EXIT_MIN_UNCHECKED',buy_plan,
                'Buy planning compares gross estimated shares with min_order_size but exposes no fee-adjusted net-share exit check. '
                'For fee-enabled taker buys, execution readiness must verify that post-fee sellable shares still satisfy the current sell minimum.')
    if not any('sell' in name.lower() or 'redeem' in name.lower() for name in adapter):
        findings.append({'code':'NO_SELL_REDEEM_ADAPTER_FOUND','line':None,'evidence':'Inspected adapter function names contain no sell/redeem operation; local uncommitted implementations are outside scope.'})
    parser=runner.get('parse_args');defaults={}
    for node in ast.walk(ast.parse(files['analysis/live_paper_trader.py'])):
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='add_argument' and node.args and isinstance(node.args[0],ast.Constant):
            name=node.args[0].value
            for kw in node.keywords:
                if kw.arg=='default' and isinstance(kw.value,ast.Constant):defaults[name]=kw.value.value
    state_path=root/'reports/live_paper_trading/state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {}
    return {'schema':1,'source_sha256':{p:hashlib.sha256(s.encode()).hexdigest() for p,s in files.items()},'findings':findings,
            'defaults':{k:v for k,v in defaults.items() if k in ('--sample-seconds','--max-live-order-usd','--starting-bankroll','--max-live-orders-per-day')},
            'saved_state':{k:state.get(k) for k in ('updated_at','signals','fills','feed_status')},
            'account_cash_verified':False,'live_098_implementation_verified':False,'execution_enabled':False,
            'note':'Read-only source audit. Findings require integration work, not proof of historical real fills or account losses.'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('.'));p.add_argument('--output',type=Path,default=Path('receipt_source_audit.json'));a=p.parse_args()
    result=audit(a.root);a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))
