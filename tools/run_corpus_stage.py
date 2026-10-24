"""Install a bounded private corpus receiver on the already verified cloud host.

The administrative identity stays on Actions. The separate expiring upload
identity can invoke only the receiver protocol. Details are sealed on delivery.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile

import research_vault as vault
import run_quality_first_research as delivery
import run_corpus_capacity_probe as probe

INSTALL = r'''
import base64, fcntl, json, os, pathlib, re, stat
payload = json.loads(PAYLOAD_LITERAL)
root=pathlib.Path('/home/ubuntu/quantml-study')
for p in (root,root/'bin'):
    if p.is_symlink(): raise ValueError('symlink_forbidden')
    p.mkdir(mode=0o700,parents=True,exist_ok=True)
    if p.stat().st_uid != os.getuid(): raise ValueError('owner_mismatch')
    p.chmod(0o700)
free=os.statvfs(root).f_bavail*os.statvfs(root).f_frsize
if free < 10*1024**3: raise ValueError('insufficient_free_capacity')
for name in ('receiver.py',):
    raw=base64.b64decode(payload['files'][name],validate=True)
    if len(raw)>200000: raise ValueError('script_size_limit')
    compile(raw,name,'exec')
    target=root/'bin'/name
    if target.is_symlink(): raise ValueError('symlink_forbidden')
    temp=target.with_suffix('.new')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f:f.write(raw)
    temp.replace(target)
public=payload['public_key'].strip().split()
if len(public) not in (2,3) or public[0]!='ssh-ed25519': raise ValueError('invalid_upload_key')
base64.b64decode(public[1],validate=True)
key=' '.join(public[:2])
folder=pathlib.Path.home()/'.ssh'
if folder.is_symlink():raise ValueError('symlink_ssh_folder')
folder.mkdir(mode=0o700,exist_ok=True)
auth=folder/'authorized_keys'
if auth.is_symlink():raise ValueError('symlink_authorized_keys')
fd=os.open(auth,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'r+') as f:
    fcntl.flock(f,fcntl.LOCK_EX)
    old=f.read(1024*1024+1)
    if len(old)>1024*1024:raise ValueError('authorized_keys_size_limit')
    line='restrict,expiry-time="20261031060000Z",command="python3 /home/ubuntu/quantml-study/bin/receiver.py --root /home/ubuntu/quantml-study" '+key+' quantml-corpus-transfer-20261024'
    matching=[s for s in old.splitlines() if public[1] in s.split()]
    if matching and matching != [line]:raise ValueError('existing_key_constraints_conflict')
    if not matching:
        f.seek(0,2)
        f.write(('' if not old or old.endswith('\n') else '\n')+line+'\n')
        f.flush();os.fsync(f.fileno())
print(json.dumps({'status':'installed','receiver_root':str(root),'available_bytes':free,'task_spool_limit_bytes':8*1024**3,'free_floor_bytes':8*1024**3,'expiry_utc':'2026-10-31T06:00:00Z','upload_scope':'forced_receiver_only','trading_changed':False}))
'''

def install():
    if os.environ.get('GITHUB_ACTIONS')!='true':raise ValueError('CLOUD_ONLY')
    host,user,known,key=probe.configuration(os.environ)
    public=os.environ.get('CORPUS_UPLOAD_PUBLIC_KEY','')
    if not public.startswith('ssh-ed25519 ') or len(public)>1024:raise ValueError('UPLOAD_PUBLIC_KEY_REQUIRED')
    source=Path(__file__).with_name('corpus_receiver.py').read_bytes()
    payload={'files':{'receiver.py':base64.b64encode(source).decode()},'public_key':public}
    program='PAYLOAD_LITERAL='+repr(json.dumps(payload))+'\n'+INSTALL
    with tempfile.TemporaryDirectory(prefix='corpus-stage-identity-') as folder:
        p=Path(folder);identity=p/'identity';trust=p/'known_hosts'
        vault.write_new(identity,key.encode(),0o600);vault.write_new(trust,known.encode(),0o600)
        check=probe.check_identity(identity,subprocess.run)
        if check['status']!='parseable':return {'status':'identity_preflight_failed','identity_check':check}
        try:
            r=subprocess.run(probe.ssh_command(host,user,identity,trust),input=program.encode(),capture_output=True,timeout=60)
            record={'status':'installed' if r.returncode==0 else 'installation_failed','returncode':r.returncode,
                    'stdout_base64':base64.b64encode(r.stdout[:262144]).decode(),
                    'stderr_base64':base64.b64encode(r.stderr[:262144]).decode()}
            if r.returncode==0:record['installation']=json.loads(r.stdout)
            return record
        except subprocess.TimeoutExpired as exc:
            return {'status':'installation_outcome_unknown','stderr_base64':base64.b64encode((exc.stderr or b'')[:262144]).decode()}

def main():
    os.umask(0o077)
    ap=argparse.ArgumentParser();ap.add_argument('--output',default='sealed-delivery');args=ap.parse_args()
    rid=os.environ.get('QUALITY_FIRST_RECIPIENT_ID')
    recipient=delivery.load_recipient(os.environ.get('QUALITY_FIRST_RECIPIENT_JSON_B64'),rid)
    out=Path(args.output);out.mkdir(mode=0o700)
    try:record=install()
    except Exception as exc:record={'status':'configuration_rejected','error_class':type(exc).__name__}
    status={'version':1,'status':'complete' if record['status']=='installed' else 'failed','stage':'sealed_delivery',
            'strategy_evaluated':False,'live_orders_enabled':False,'plaintext_published':False}
    if status['status']=='failed':status['failed_stage']='acquisition'
    with tempfile.TemporaryDirectory() as folder:
        p=Path(folder);vault.write_new(p/'results'/'capacity.json',vault.canonical(record))
        status['sealed_parts']=delivery.deliver_bundle(delivery.make_bundle(p),out,recipient,rid)
    vault.write_new(out/'status.json',vault.canonical(status),0o644)
    print(json.dumps(status));return 0 if status['status']=='complete' else 2

if __name__=='__main__':raise SystemExit(main())
