#!/usr/bin/env python3
"""Create a sealed, bounded preflight for the next private corpus cursor."""
from __future__ import annotations
import datetime as dt, json, os, sys
from pathlib import Path

ROOT=Path("/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015")
BASE=Path("/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016")
STUDY=BASE/"quantml-full-study"
N=os.environ.get("EXTRACT_BATCH","0136")
PRIOR=os.environ.get("EXTRACT_PRIOR", "0135")
PREFLIGHT=STUDY/f"extract-{N}-preflight-20261026.json"
DELIVERY=STUDY/f"extract-{N}-preflight-delivery"
RID="5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9"
sys.path.insert(0,str(ROOT/"tools"))
import research_vault as vault  # noqa: E402

def main():
    if PREFLIGHT.exists() or DELIVERY.exists(): raise SystemExit("already_exists")
    prior=STUDY/f"extract-{PRIOR}-readback-20261026.json"
    d=json.loads(prior.read_text(encoding="utf-8")); b=d.get("source_receipts_bounds") or {}
    now=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
    pre={"format":f"quantml-extract-{N}-preflight-v1","created_utc":now,"batch_id":f"extract-{N}-no-ack-cursor-continuation",
      "workflow":"corpus-private-extract.yml","mode":"extract","source_previous_batch":{"run_id":d.get("workflow_run_id"),"readback":str(prior),"metadata_cursor_committed":d.get("metadata_cursor_committed"),"last_source_id":b.get("last_source_id"),"source_count_including_discovered_children":(d.get("coverage") or {}).get("source_count_including_discovered_children")},
      "input_scope":{"scope":f"existing private manifest after verified {PRIOR} cursor; no shared delivery ACK input; two metadata entries, two files and at most twenty units","local_bytes_read":0,"archive_and_video_denominators_preserved":True,"ack_secret_state":"absent_by_name_check; secret value never read"},
      "cloud_execution":{"backend":"GitHub Actions corpus-private-extract","runner":"ubuntu-latest","expected_cpu":"bounded worker batch only","expected_memory":"existing worker and 1 GiB output cap","expected_runtime_minutes":20,"concurrency":1,"local_heavy_compute":False},
      "parameters":{"pdf_ocr":True,"max_metadata":2,"max_files":2,"max_units":20,"recovery_source_ids":[],"no_strategy_evaluation":True,"no_model_refit":True,"no_live_orders":True},
      "gates":{"duplicate_dispatch":False,"prior_terminal_readback_verified":True,"ack_secret_present_by_name":False,"semantic_credit_before":0,"full_corpus_complete_before":False,"actual_fills_before":0,"verified_canary_before":0,"buying_paused":True},
      "hypothesis":f"A separate bounded cursor continuation {N} expands coverage without replaying {PRIOR} or treating transport as semantic learning.","success_contract":"Persist encrypted report and direct readback; count only newly committed units and never infer semantic learning from transport.","failure_contract":"Retain terminal or unknown result and do not replay without readback; keep semantic credit 0 and trading disabled.","idempotency_key":f"quantml-extract-{N}|after-{d.get('workflow_run_id')}|no-ack|files-2|units-20|ocr-true","result_paths":{"local_compact_summary":str(STUDY/f"extract-{N}-readback-20261026.json"),"encrypted_journal_branch":"sealed-research-journal"}}
    PREFLIGHT.write_text(json.dumps(pre,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); PREFLIGHT.chmod(0o600)
    recipient=vault.load_json_bytes(vault.read_bounded(ROOT/"research_vault/recipient.json",4096),4096); vault.validate_recipient(recipient,RID); DELIVERY.mkdir(mode=0o700)
    vault.write_new(DELIVERY/"part-0000.vault",vault.seal(vault.canonical({"format":f"quantml-extract-{N}-preflight-v1","preflight":pre,"actual_fills":0,"verified_canary_fills":0,"live_orders_enabled":False,"plaintext_published":False}),recipient,RID),0o600)
    vault.write_new(DELIVERY/"status.json",vault.canonical({"version":1,"status":"complete","stage":"sealed_delivery","strategy_evaluated":False,"live_orders_enabled":False,"plaintext_published":False,"sealed_parts":1})+b"\n",0o644)
    print(json.dumps({"status":"prepared","batch":N,"preflight":str(PREFLIGHT)},ensure_ascii=False))

if __name__=="__main__": main()
