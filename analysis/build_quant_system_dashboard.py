#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
ROBUST_DIR = ROOT / "reports" / "robust_trading_system"
INTRADAY_DIR = ROOT / "reports" / "systematic_intraday_tool"
LIVE_DIR = ROOT / "reports" / "live_paper_trading"
OUT_DIR = ROOT / "reports" / "quant_system_dashboard"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return rows if limit is None else rows[:limit]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def top_rows(rows: List[Dict[str, Any]], key: str, limit: int, reverse: bool = True) -> List[Dict[str, Any]]:
    def value(row: Dict[str, Any]) -> float:
        try:
            return float(row.get(key, 0) or 0)
        except Exception:
            return 0.0

    return sorted(rows, key=value, reverse=reverse)[:limit]


def payload() -> Dict[str, Any]:
    strategy_results = read_csv(INTRADAY_DIR / "strategy_results.csv")
    factor_rows = read_csv(ROBUST_DIR / "factor_diagnostics.csv")
    stable_factors = [
        row for row in factor_rows
        if str(row.get("stable_corr_pnl_sign", "")) == "1"
    ]
    stable_factors = top_rows(stable_factors, "test_corr_pnl", 10)
    return {
        "manifest": read_json(ROBUST_DIR / "manifest.json", {}),
        "splitMetrics": read_csv(ROBUST_DIR / "selected_system_split_metrics.csv"),
        "probabilityMetrics": read_csv(ROBUST_DIR / "probability_metrics.csv"),
        "decisionFunnel": read_csv(ROBUST_DIR / "decision_funnel.csv"),
        "modelMix": read_csv(ROBUST_DIR / "model_mix.csv"),
        "factorDiagnostics": top_rows(factor_rows, "test_auc", 14),
        "stableFactors": stable_factors,
        "stressTests": read_csv(ROBUST_DIR / "stress_tests.csv"),
        "walkForward": read_csv(ROBUST_DIR / "walk_forward_metrics.csv"),
        "paperReplay": read_csv(ROBUST_DIR / "paper_replay_scenarios.csv"),
        "topStrategies": top_rows(strategy_results, "research_score", 18),
        "priorStrategies": read_csv(INTRADAY_DIR / "prior_selected_comparison.csv", 12),
        "strategyBrief": read_text(INTRADAY_DIR / "strategy_brief.md"),
        "liveState": read_json(LIVE_DIR / "state.json", {}),
        "liveEvents": read_csv(LIVE_DIR / "paper_events.csv")[-80:],
        "audit": read_text(ROOT / "docs" / "quant_system_audit.md"),
    }


def build_html(data: Dict[str, Any]) -> str:
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket Quant System</title>
  <style>
    :root {{
      --ink:#172026; --muted:#62727f; --line:#d9e0e4; --paper:#fbfcfd; --panel:#ffffff;
      --teal:#0f766e; --blue:#2563eb; --amber:#b45309; --red:#b42318; --violet:#6d28d9; --green:#137333;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--paper); color:var(--ink); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; letter-spacing:0; }}
    header {{ position:sticky; top:0; z-index:5; display:flex; align-items:center; justify-content:space-between; gap:16px; padding:12px 18px; background:rgba(251,252,253,.96); border-bottom:1px solid var(--line); backdrop-filter:blur(10px); }}
    h1 {{ margin:0; font-size:18px; line-height:1.1; }}
    h2 {{ margin:0 0 10px; font-size:15px; }}
    h3 {{ margin:0 0 8px; font-size:13px; color:#344054; }}
    main {{ max-width:1480px; margin:0 auto; padding:16px 18px 36px; }}
    .tabs {{ display:flex; gap:6px; flex-wrap:wrap; }}
    button.tab {{ border:1px solid var(--line); background:#fff; color:#344054; border-radius:6px; padding:7px 10px; font-weight:650; cursor:pointer; }}
    button.tab.active {{ background:var(--ink); color:#fff; border-color:var(--ink); }}
    section.view {{ display:none; }}
    section.view.active {{ display:block; }}
    .kpis {{ display:grid; grid-template-columns:repeat(6,minmax(130px,1fr)); gap:10px; margin-bottom:12px; }}
    .kpi,.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .kpi .label {{ color:var(--muted); font-size:12px; }}
    .kpi .value {{ margin-top:4px; font-size:23px; font-weight:760; overflow-wrap:anywhere; }}
    .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
    .grid3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }}
    .flow {{ display:grid; grid-template-columns:repeat(6,minmax(120px,1fr)); gap:8px; }}
    .step {{ border:1px solid var(--line); border-radius:8px; padding:10px; min-height:92px; background:#fff; }}
    .step b {{ display:block; margin-bottom:6px; }}
    .small {{ color:var(--muted); font-size:12px; }}
    .formula {{ padding:10px 12px; background:#f7f9fb; border:1px solid var(--line); border-radius:8px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; overflow:auto; }}
    canvas {{ display:block; width:100%; height:260px; background:#fff; border:1px solid var(--line); border-radius:8px; }}
    table {{ width:100%; border-collapse:collapse; min-width:760px; }}
    th,td {{ padding:7px 8px; border-bottom:1px solid #e7edf1; text-align:left; white-space:nowrap; }}
    th {{ position:sticky; top:0; background:#f3f6f8; color:#40515f; font-size:12px; z-index:1; }}
    .scroll {{ max-height:390px; overflow:auto; border:1px solid var(--line); border-radius:8px; background:#fff; }}
    .good {{ color:var(--green); font-weight:700; }} .bad {{ color:var(--red); font-weight:700; }}
    .pill {{ display:inline-flex; align-items:center; min-height:22px; border-radius:999px; padding:2px 8px; background:#eef6f5; color:var(--teal); font-weight:700; font-size:12px; }}
    .warn {{ background:#fff7ed; color:var(--amber); }}
    .danger {{ background:#fff1f3; color:var(--red); }}
    @media (max-width:1100px) {{ .kpis,.grid3,.grid2,.flow {{ grid-template-columns:1fr 1fr; }} }}
    @media (max-width:720px) {{ header {{ align-items:flex-start; flex-direction:column; }} .kpis,.grid3,.grid2,.flow {{ grid-template-columns:1fr; }} main {{ padding:12px; }} }}
  </style>
</head>
<body>
<header>
  <h1>Polymarket BTC 5m Quant System</h1>
  <nav class="tabs" id="tabs"></nav>
</header>
<main>
  <section id="overview" class="view active">
    <div class="kpis" id="kpis"></div>
    <div class="grid2">
      <div class="panel"><h2>资金曲线对比</h2><canvas id="splitChart" width="900" height="300"></canvas></div>
      <div class="panel"><h2>概率质量</h2><canvas id="probChart" width="900" height="300"></canvas></div>
    </div>
    <div class="panel" style="margin-top:12px"><h2>系统流水线</h2><div class="flow" id="flow"></div></div>
  </section>
  <section id="signals" class="view">
    <div class="grid2">
      <div class="panel"><h2>决策漏斗</h2><canvas id="funnelChart" width="900" height="310"></canvas></div>
      <div class="panel"><h2>模型混合</h2><canvas id="mixChart" width="900" height="310"></canvas></div>
    </div>
    <div class="grid2" style="margin-top:12px">
      <div class="panel"><h2>主因子</h2><div class="scroll"><table id="factorTable"></table></div></div>
      <div class="panel"><h2>稳定 PnL 因子</h2><div class="scroll"><table id="stableTable"></table></div></div>
    </div>
  </section>
  <section id="risk" class="view">
    <div class="grid3">
      <div class="panel"><h2>仓位公式</h2><div class="formula">edge = q_model - entry_price - fee<br>full_kelly = edge / (1 - entry_price)<br>stake = min(event_cap / max_trades, 0.25 * full_kelly)</div></div>
      <div class="panel"><h2>执行闸门</h2><div id="riskGates"></div></div>
      <div class="panel"><h2>实时护栏</h2><div id="liveGates"></div></div>
    </div>
    <div class="panel" style="margin-top:12px"><h2>压力测试</h2><div class="scroll"><table id="stressTable"></table></div></div>
  </section>
  <section id="validation" class="view">
    <div class="grid2">
      <div class="panel"><h2>Walk Forward</h2><canvas id="wfChart" width="900" height="310"></canvas></div>
      <div class="panel"><h2>Paper Replay</h2><canvas id="paperChart" width="900" height="310"></canvas></div>
    </div>
    <div class="panel" style="margin-top:12px"><h2>策略演变</h2><div class="scroll"><table id="strategyTable"></table></div></div>
  </section>
  <section id="live" class="view">
    <div class="grid3">
      <div class="panel"><h2>Live 状态</h2><div id="liveStatus"></div></div>
      <div class="panel"><h2>运行命令</h2><div class="formula">python analysis/live_paper_trader.py --execution-mode paper<br>python analysis/live_paper_trader.py --execution-mode plan --once<br>python analysis/live_paper_trader.py --env-file .env.local --execution-mode live --allow-live-trading --max-live-order-usd 1</div></div>
      <div class="panel"><h2>合规状态</h2><div id="geoStatus"></div></div>
    </div>
    <div class="panel" style="margin-top:12px"><h2>最近事件</h2><div class="scroll"><table id="liveEvents"></table></div></div>
  </section>
</main>
<script id="payload" type="application/json">{data_json}</script>
<script>
const data = JSON.parse(document.getElementById('payload').textContent);
const views = [
  ['overview','总览'], ['signals','信号'], ['risk','仓位/风控'], ['validation','验证'], ['live','实盘']
];
const tabs = document.getElementById('tabs');
tabs.innerHTML = views.map(([id,label],i)=>`<button class="tab ${{i===0?'active':''}}" data-view="${{id}}">${{label}}</button>`).join('');
tabs.addEventListener('click', e => {{
  const b = e.target.closest('button'); if (!b) return;
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active', x===b));
  document.querySelectorAll('.view').forEach(x=>x.classList.toggle('active', x.id===b.dataset.view));
  setTimeout(drawAll, 20);
}});
const num = v => {{ const n=Number(v); return Number.isFinite(n)?n:0; }};
const pct = v => Number.isFinite(Number(v)) ? (Number(v)*100).toFixed(1)+'%' : '';
const money = v => Number.isFinite(Number(v)) ? '$'+Number(v).toFixed(2) : '';
const fmt = v => Number.isFinite(Number(v)) ? Number(v).toFixed(3) : '';
const cls = v => Number(v) >= 0 ? 'good' : 'bad';
function table(id, rows, cols) {{
  document.getElementById(id).innerHTML = '<thead><tr>'+cols.map(c=>`<th>${{c[0]}}</th>`).join('')+'</tr></thead><tbody>'+
    rows.map(r=>'<tr>'+cols.map(c=>`<td class="${{c[2]?c[2](r):''}}">${{c[1](r) ?? ''}}</td>`).join('')+'</tr>').join('')+'</tbody>';
}}
function canvas(id) {{
  const el=document.getElementById(id); if (!el) return null;
  const ctx=el.getContext('2d'); ctx.clearRect(0,0,el.width,el.height); return [el,ctx];
}}
function bars(id, rows, label, value, opts={{}}) {{
  const got=canvas(id); if(!got||!rows.length)return; const [el,ctx]=got,w=el.width,h=el.height,p=42;
  const vals=rows.map(value).map(num), max=Math.max(1,...vals.map(Math.abs));
  ctx.strokeStyle='#d9e0e4'; ctx.beginPath(); ctx.moveTo(p,h-p); ctx.lineTo(w-p,h-p); ctx.stroke();
  const bw=(w-2*p)/rows.length*.58;
  rows.forEach((r,i)=>{{ const v=num(value(r)), x=p+(i+.22)*(w-2*p)/rows.length, bh=Math.abs(v)/max*(h-2*p);
    ctx.fillStyle=opts.color?opts.color(r,i,v):(v>=0?'#0f766e':'#b42318'); ctx.fillRect(x,h-p-bh,bw,bh);
    ctx.fillStyle='#40515f'; ctx.font='12px system-ui'; ctx.save(); ctx.translate(x,h-12); ctx.rotate(-0.55); ctx.fillText(String(label(r)).slice(0,18),0,0); ctx.restore();
    ctx.fillStyle='#172026'; ctx.fillText(opts.format?opts.format(v):String(Math.round(v)),x,Math.max(16,h-p-bh-6));
  }});
}}
function line(id, rows, label, value, opts={{}}) {{
  const got=canvas(id); if(!got||!rows.length)return; const [el,ctx]=got,w=el.width,h=el.height,p=42;
  const vals=rows.map(value).map(num), min=Math.min(...vals), max=Math.max(...vals), span=Math.max(1e-9,max-min);
  ctx.strokeStyle='#d9e0e4'; ctx.strokeRect(p,p,w-2*p,h-2*p);
  ctx.strokeStyle=opts.color||'#2563eb'; ctx.lineWidth=2; ctx.beginPath();
  vals.forEach((v,i)=>{{ const x=p+i/Math.max(1,vals.length-1)*(w-2*p), y=h-p-(v-min)/span*(h-2*p); if(i===0)ctx.moveTo(x,y); else ctx.lineTo(x,y); }});
  ctx.stroke(); ctx.fillStyle='#40515f'; ctx.fillText((opts.format?opts.format(vals[vals.length-1]):vals[vals.length-1].toFixed(2)),p,22);
}}
function drawAll() {{
  const splits = data.splitMetrics || [];
  bars('splitChart', splits, r=>r.split, r=>r.ending_bankroll, {{color:r=>r.split==='test'?'#2563eb':r.split==='validation'?'#0f766e':'#b45309', format:money}});
  bars('probChart', data.probabilityMetrics||[], r=>r.split, r=>-num(r.logloss_delta_vs_market), {{color:()=> '#6d28d9', format:v=>v.toFixed(3)}});
  bars('funnelChart', (data.decisionFunnel||[]).filter(r=>r.split==='test'), r=>r.stage, r=>r.count, {{color:()=> '#2563eb'}});
  bars('mixChart', (data.modelMix||[]).filter(r=>r.layer==='mixed_member'), r=>r.name, r=>r.weight, {{color:()=> '#0f766e', format:v=>v.toFixed(2)}});
  bars('wfChart', (data.walkForward||[]).filter(r=>r.split==='test'), r=>r.fold, r=>r.ending_bankroll, {{color:()=> '#b45309', format:money}});
  bars('paperChart', (data.paperReplay||[]).filter(r=>r.split==='test').slice(0,8), r=>r.scenario, r=>r.ending_bankroll, {{color:()=> '#6d28d9', format:money}});
}}
function render() {{
  const m=data.manifest||{{}}, test=(data.splitMetrics||[]).find(r=>r.split==='test')||{{}}, val=(data.splitMetrics||[]).find(r=>r.split==='validation')||{{}};
  document.getElementById('kpis').innerHTML = [
    ['Test ending', money(test.ending_bankroll), 'OOS'],
    ['Test return', pct(test.total_return), 'after fee'],
    ['Win rate', pct(test.win_rate), 'executed trades'],
    ['Profit factor', fmt(test.profit_factor), 'gain/loss'],
    ['Max drawdown', pct(test.max_drawdown), 'test'],
    ['Trades', test.trades || 0, 'test']
  ].map(x=>`<div class="kpi"><div class="label">${{x[2]}}</div><div class="value">${{x[1]}}</div><div class="label">${{x[0]}}</div></div>`).join('');
  document.getElementById('flow').innerHTML = [
    ['行情','Gamma/CLOB + BTC spot','每 5m market 定位当前 token 和盘口'],
    ['候选','dense4s','从第 '+m.entry_start_seconds+' 秒开始节流取样'],
    ['概率','mixed q model','校准 q_model，和 market baseline 比 logloss'],
    ['Edge','q - price - fee','普通阈值 '+fmt(m.execution_search_grid?.normal_edge?.[1] || test.normal_edge)],
    ['Sizing','fractional Kelly','event cap '+pct(test.event_cap)+'，Kelly shrink '+fmt(test.kelly_shrink)],
    ['Execution','paper / plan / live','depth、spread、overround、重复开仓 gate']
  ].map(s=>`<div class="step"><b>${{s[0]}}</b><div>${{s[1]}}</div><div class="small">${{s[2]}}</div></div>`).join('');
  document.getElementById('riskGates').innerHTML = [
    ['normal edge', fmt(test.normal_edge)], ['boundary edge', fmt(test.boundary_edge)], ['min depth', test.min_depth],
    ['max trades/market', test.max_trades_per_market], ['entry gap', test.min_entry_gap_seconds+'s'], ['exit', test.exit_policy]
  ].map(x=>`<p><span class="pill">${{x[0]}}</span> ${{x[1]}}</p>`).join('');
  const live=data.liveState||{{}};
  document.getElementById('liveGates').innerHTML = [
    ['mode', live.execution_mode || 'paper'], ['signals', live.signals || 0], ['fills', live.fills || 0], ['open positions', (live.open_positions||[]).length]
  ].map(x=>`<p><span class="pill warn">${{x[0]}}</span> ${{x[1]}}</p>`).join('');
  document.getElementById('liveStatus').innerHTML = `<p><span class="pill ${{live.feed_status==='error'?'danger':''}}">${{live.feed_status||'initialized'}}</span></p><p>${{htmlEscape(live.current_slug||'no current slug')}}</p><p class="small">${{htmlEscape(live.last_error||'')}}</p>`;
  document.getElementById('geoStatus').innerHTML = '<p><span class="pill danger">restricted check required</span></p><p class="small">官方文档列出 US 等地区不能开新仓；live 模式只应在平台允许的物理位置和账户状态下使用。</p>';
  table('factorTable', data.factorDiagnostics||[], [
    ['factor', r=>r.feature], ['test AUC', r=>fmt(r.test_auc)], ['test pnl corr', r=>fmt(r.test_corr_pnl), r=>cls(r.test_corr_pnl)], ['val pnl corr', r=>fmt(r.validation_corr_pnl), r=>cls(r.validation_corr_pnl)]
  ]);
  table('stableTable', data.stableFactors||[], [
    ['factor', r=>r.feature], ['train', r=>fmt(r.train_corr_pnl), r=>cls(r.train_corr_pnl)], ['validation', r=>fmt(r.validation_corr_pnl), r=>cls(r.validation_corr_pnl)], ['test', r=>fmt(r.test_corr_pnl), r=>cls(r.test_corr_pnl)]
  ]);
  table('stressTable', (data.stressTests||[]).filter(r=>r.split==='test'), [
    ['scenario', r=>r.scenario], ['note', r=>r.note], ['trades', r=>r.trades], ['ending', r=>money(r.ending_bankroll)], ['return', r=>pct(r.total_return), r=>cls(r.total_return)], ['DD', r=>pct(r.max_drawdown)]
  ]);
  table('strategyTable', data.topStrategies||[], [
    ['strategy', r=>r.strategy], ['family', r=>r.family], ['exit', r=>r.exit_policy], ['trades', r=>r.trades], ['ending', r=>money(r.ending_bankroll)], ['return', r=>pct(r.total_return), r=>cls(r.total_return)], ['DD', r=>pct(r.max_drawdown)], ['score', r=>fmt(r.research_score)]
  ]);
  table('liveEvents', data.liveEvents||[], [
    ['time', r=>r.event_ts], ['type', r=>r.event_type], ['status', r=>r.status], ['slug', r=>r.slug], ['side', r=>r.side], ['q', r=>fmt(r.q_model)], ['edge', r=>fmt(r.edge)], ['amount', r=>money(r.live_amount_usd||r.fill_cost)], ['order', r=>r.live_order_id||r.live_order_local_id||'']
  ]);
  drawAll();
}}
function htmlEscape(s) {{ return String(s).replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch])); }}
render();
window.addEventListener('resize', drawAll);
</script>
</body>
</html>"""


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = payload()
    out = OUT_DIR / "index.html"
    out.write_text(build_html(data), encoding="utf-8")
    print(json.dumps({"dashboard": str(out), "source": str(ROBUST_DIR)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
