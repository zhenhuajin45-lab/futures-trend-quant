"""A separate paper permission boundary. Existing backtest gates stay closed."""
from pathlib import Path
import hashlib
import json
import math
import uuid
from .paper_products import FIVE_POOL
from futures_trend_quant.config import load_config

ROOT=Path(__file__).resolve().parents[2]
SIM_ENDPOINT='api.myquant.cn:9000'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def frozen_core_digest(root):
    root=Path(root);h=hashlib.sha256()
    files=sorted((root/'futures_trend_quant/core').rglob('*.py'))+[root/'tools/short_swing_v9/policy.py']
    for p in files:
        h.update(str(p.relative_to(root)).replace('\\','/').encode());h.update(b'\0');h.update(p.read_bytes().replace(b'\r\n',b'\n'));h.update(b'\0')
    return h.hexdigest()


def load_paper_config(path, root=ROOT):
    path=Path(path).resolve(); root=Path(root).resolve()
    c=json.loads(path.read_text(encoding='utf-8-sig'))
    if c.get('schema_version') not in (1,2) or c.get('strategy_name') not in ('S0_SHORT_PAPER_V1','S0_SHORT_PAPER_CORE_V2'):raise ValueError('invalid paper strategy')
    if c.get('paper_orders_enabled') is not True or c.get('live_orders_enabled') is not False:raise ValueError('paper/live permission conflict')
    if c.get('endpoint')!=SIM_ENDPOINT or c.get('transport')!='OFFICIAL_SIM_GMTRADE':raise ValueError('non-simulation route denied')
    if c.get('max_lots')!=1 or c.get('max_positions') not in (1,2):raise ValueError('paper size limit')
    if (c['schema_version']==1 and c.get('universe')!=['DCE.M']) or (c['schema_version']==2 and c.get('universe')!=list(FIVE_POOL)):raise ValueError('unregistered observation pool')
    if c['schema_version']==2 and c.get('market_registry')!=c['state_dir']+'/market_registry.local.json':raise ValueError('registry must belong to this execution state')
    if c['schema_version']==2 and c.get('notification_config')!='config/paper_feishu.local.json':raise ValueError('private notification configuration required')
    if c.get('warmup_no_trade_manifest'):
        proof=(root/c['warmup_no_trade_manifest']).resolve()
        if not proof.is_relative_to(root.resolve()/'data/cache'):raise ValueError('warmup evidence outside private cache')
        if digest(proof)!=c.get('warmup_no_trade_manifest_sha256'):raise ValueError('warmup evidence manifest changed')
    for k,cap in [('risk_pct',.0025),('margin_usage_pct',.20),('daily_loss_pct',.01),('observation_drawdown_pct',.05)]:
        x=c.get(k)
        if type(x) not in (int,float) or not math.isfinite(x) or not 0<x<=cap:raise ValueError('invalid paper '+k)
    if c.get('quote_max_age_seconds')!=3 or c.get('open_wait_seconds')!=10 or c.get('signal_max_age_seconds')!=30 or c.get('max_spread_ticks')!=4:raise ValueError('unregistered execution bounds')
    if c.get('warmup_m15')!=1200 or c.get('seed_m1_count')!=22000:raise ValueError('warmup cannot be weakened')
    if c.get('connectivity_test_required') is not True:raise ValueError('connectivity test required')
    if c.get('execution_policy_version') not in (None,'POSTCLOSE_EXEC_V3_20261106'):raise ValueError('unregistered execution policy')
    binding=(root/c['account_binding']).resolve()
    if not binding.is_relative_to(root) or digest(binding)!=c.get('account_binding_sha256'):raise ValueError('account binding changed')
    a=json.loads(binding.read_text(encoding='utf-8-sig'))
    account=a.get('account_id','');uuid.UUID(account)
    if a.get('endpoint')!=SIM_ENDPOINT or a.get('source')!='GM_OFFICIAL_SIM_ACCOUNT_CASH_ID_MATCH':raise ValueError('unproved simulation account')
    if a.get('user_designated_futures_sim') is not True:raise ValueError('futures account not designated')
    c['account_id']=account;c['config_sha256']=digest(path);c['root']=str(root)
    base=root/c['base_strategy']
    if hashlib.sha256(base.read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=c.get('base_strategy_sha256_lf'):raise ValueError('base S0 configuration changed')
    if frozen_core_digest(root)!=c.get('core_policy_sha256_lf'):raise ValueError('core or holding policy changed')
    c['base_config']=load_config(base)
    if c['strategy_name']=='S0_SHORT_PAPER_CORE_V2':
        if c.get('permission_policy')!='CLOSED_FACTS_PERMISSION_V1':raise ValueError('unregistered permission version')
        c['paper_rules']=dict(c['base_config'].raw['rules'],paper_permission_v1=True)
    for key in ('state_dir','evidence_dir'):
        target=(root/c[key]).resolve()
        if not target.is_relative_to(root):raise ValueError('paper path escapes root')
        c[key]=str(target)
    return c
