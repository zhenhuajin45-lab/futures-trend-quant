"""Read-only 15-minute trend cards; reuses the strategy's private Feishu outbox."""
from pathlib import Path
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
import argparse,hashlib,json,math,os,re,sys,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from futures_trend_quant.live.paper_io import read_json
TZ=ZoneInfo('Asia/Shanghai')
POOL=[('DCE.M','豆粕'),('SHFE.AU','黄金'),('DCE.EG','乙二醇'),('DCE.JD','鸡蛋'),('CZCE.CJ','红枣')]
DIR={'bull':'偏多','bear':'偏空','none':'暂无确认方向'}
ICONS={'bull':'🟥','bear':'🟩','none':'⬜'}
WHY={'NO_PATH':'入场路径条件未到齐','NO_CONFIRMED_TREND':'趋势方向尚未确认','COOLDOWN':'冷却中','POSITION_EXISTS':'已有同向持仓'}
def read(p):return read_json(p)

_notice_cache={}
def read_notice(path):
 """Immutable payload cache invalidated by actual file identity/size/mtime."""
 path=Path(path);s=path.stat();identity=(s.st_ino,s.st_size,s.st_mtime_ns)
 prior=_notice_cache.get(path)
 if prior is None or prior[0]!=identity:_notice_cache[path]=(identity,read(path))
 if len(_notice_cache)>4096:
  for key in list(_notice_cache)[:1024]:_notice_cache.pop(key,None)
 return _notice_cache[path][1]

def observation_pool(status):
 from futures_trend_quant.live.paper_products import PRODUCTS,registered_pool
 universe=status.get('universe',[cs for cs,name in POOL])
 registered_pool(universe,status.get('pool_policy_version'))
 return [(cs,PRODUCTS[cs]['name']) for cs in universe]
def clock(t):return datetime.fromisoformat(str(t).replace('Z','+00:00'))

def display_time(t):return clock(t).astimezone(TZ).strftime('%Y-%m-%d  %H:%M:%S')

def badge(direction):return ICONS.get(direction,'🟨')+' '+DIR.get(direction,'未知，不能判断')

def ema_description(facts,previous=None):
 try:
  close=float(facts['close']);ema=float(facts['ema34'])
  if not all(math.isfinite(x) for x in (close,ema)):raise ValueError('invalid display price')
 except (ValueError,KeyError,TypeError):return 'EMA依据不足，不能判断均线位置'
 delta=close-ema;position='均线上方' if delta>0 else '均线下方' if delta<0 else '均线处'
 shape=''
 if previous:
  try:
   old=float(previous['close'])-float(previous['ema34'])
   if old<=0<delta:shape='；本根收盘上穿EMA34'
   elif old>=0>delta:shape='；本根收盘下穿EMA34'
  except (ValueError,KeyError,TypeError):pass
 slope={'up':'均线上行','down':'均线下行','flat':'均线走平'}.get(facts.get('slope_state'),'均线斜率待确认')
 return f'M15收盘 {close:.2f}；EMA34 {ema:.2f}；收盘在{position}{shape}；{slope}'

def trend_risk(item):
 f=item.get('evidence') or {};direction=item['direction'];alerts=[]
 if f.get('entry_policy_version')=='PAPER_ENTRY_V5':
  if direction not in ('bull','bear'):
   detail=[]
   for side,label in [('top','顶'),('bottom','底')]:
    event=f.get(side+'_divergence_event')
    if event and f.get(side+'_div'):
     try:detail.append('历史'+label+'背驰：确认 '+display_time(event['defined_at_bar_id'].rsplit('|',1)[-1])+'，极值 '+str(event['extreme_price']))
     except (ValueError,KeyError,TypeError):detail.append('历史'+label+'背驰：确认时钟或极值UNKNOWN')
   state=(f.get('trend_decision') or {}).get('state') or {}
   detail.append('建立票 '+str(state.get('establish_votes',0))+'；首因 '+str((f.get('trend_decision') or {}).get('reason_code','UNKNOWN')))
   return '尚无S0方向授权，入场后续层NOT_EVALUATED；不假定空头或多头。'+'；'.join(detail)
  selected=f.get('entry_permission') or {};entry=item.get('entry') or {}
  from futures_trend_quant.live.paper_cards import reason_text
  if selected.get('new_entry_allowed'):
   path=(f.get('entry_policy_proof') or {}).get('path') or entry.get('path') or 'UNKNOWN'
   text='路径 '+path+' 资格通过'+('（M15柱符号仅提示）' if path=='B' else '')
   if (item.get('candidate_plan') or {}).get('invalid'):
    text+='；冻结参考计划不可用：'+reason_text(item['candidate_plan']['invalid'])+'（不改写实际首拦截）'
   terminal=item.get('execution_terminal') or {}
   if terminal.get('status')=='SUBMIT_OUTCOME_UNKNOWN':return text+'；提交结果未知，禁止重发或切换路径，等待柜台对账；不能按未成交或零敞口处理'
   if terminal.get('status') in ('BLOCKED','INVALIDATED','EXPIRED','EXPIRED_BEFORE_QUEUE','FUTURE_CLOCK_REJECTED','ABORTED_RESTART','LOCAL_REJECTED_BEFORE_NATIVE'):
    return text+'；执行已终止：'+reason_text(terminal.get('reason') or terminal['status'])+'；未下单'
   if terminal.get('status')=='SUBMITTED':return text+'；已生成委托意图，柜台受理和成交另报'
   return text+'；'+('仍须结构、报价及账户检查，非成交' if entry.get('accepted') else '尚未形成执行候选：'+reason_text(entry.get('reason','UNKNOWN')))
  formed=[x for x in f.get('candidate_audit',[]) if x.get('evidence_id')]
  labels={'PATH_D_PREREQUISITE':'D原条件未满足','PATH_C_PREREQUISITE':'C原条件未满足','ORDERED_PROCESS_NOT_CONFIRMED_OR_ACTIVE':'有序回调未确认或已结束','NO_TECHNICAL_PATH':'未形成路径','M15_MACD_NOT_ALIGNED':'M15 MACD柱与路径方向不同向（A/C/D硬门槛）','M15_OPPOSITE_DIVERGENCE_ACTIVE':'该路径受反向M15历史结构背驰限制，非新预警'}
  detail='；'.join(x['path']+'：'+'、'.join(labels.get(k,reason_text(k)) for k in x.get('reasons',[])[:3]) for x in formed)
  return '本根无合格入场'+('；'+detail if detail else '：尚未形成有效技术路径')+'；B柱符号仅提示，A/C/D仍为硬门槛。'
 if direction=='bull' and f.get('top_div'):alerts.append('⚠️ 历史M15顶背驰结构仍有效；是否为当前预警另行登记，新开权限另列')
 if direction=='bear' and f.get('bottom_div'):alerts.append('⚠️ 历史M15底背驰结构仍有效；是否为当前预警另行登记，新开权限另列')
 if f.get('trend_decision',{}).get('proposed_action')=='veto':alerts.append('退势条件已触发，被H1强势否决；未确认反转')
 p=f.get('trade_permission')
 if p:
  from futures_trend_quant.live.paper_cards import reason_text
  alerts.append(('✅ 允许评估新开' if p['new_entry_allowed'] else '⛔ 禁止新开：'+'；'.join(reason_text(x) for x in p['reasons'])))
  if p.get('technical_exit_direction'):alerts.append('⚠️ 原方向技术退出；不等于反向开仓')
 return '；'.join(alerts)
def active_symbols(root,t):
 registry=read(root/'state/paper_observation_five/market_registry.local.json');return {v['csymbol'] for v in registry['contracts'].values() if any(a<=t<b for a,b in v['trade_windows'])}
def rows(root,now):
 s=read(root/'evidence/paper_observation_five/status.json');fresh=0<=(now-clock(s['at'])).total_seconds()<=20;latest={};previous={};p=root/'evidence/paper_observation_five/signals.jsonl'
 if p.exists():
  for line in p.read_text(encoding='utf8').splitlines():
   try:
    r=json.loads(line);cs=r['csymbol']
    if cs in latest and latest[cs]['bar_eob']!=r['bar_eob']:previous[cs]=latest[cs]
    latest[cs]=r
   except (ValueError,KeyError):continue
 active=active_symbols(root,now.timestamp());result=[]
 for cs,name in observation_pool(s):
  product=s['products'].get(cs,{})
  from futures_trend_quant.live.paper_symbol_health import display_status
  market=display_status(root,cs,now,s)
  if not market['ready']:continue
  try:
   cp=read(root/('state/paper_observation_five/products/'+cs.replace('.','_')+'/core_checkpoint.json'));state=cp['runtime']['trends'][cs];eob=cp['last_eob'];direction=state['trend_state']
   if clock(eob)>now:raise ValueError('future checkpoint')
   if state['last_closed_bar_id'].rsplit('|',1)[-1]!=eob:raise ValueError('checkpoint bar mismatch')
  except (ValueError,KeyError,OSError):state={};eob=None;direction='UNKNOWN'
  bar_fresh=bool(eob and clock(eob).timestamp()>=int(now.timestamp())//900*900)
  from futures_trend_quant.live.paper_symbol_health import POLICY as SYMBOL_POLICY
  if market.get('policy')==SYMBOL_POLICY and cs in active and not bar_fresh:continue
  row=latest.get(cs);aligned=bool(row and row.get('bar_eob')==eob)
  entry=(row.get('entry') or {}) if aligned else {};decision=row.get('paper_decision') if aligned else None
  if cs not in active:why='当前休市，沿用最近已闭合M15'
  elif not aligned:why='本轮入场明细尚未生成；等待已闭合M15'
  elif entry.get('accepted'):why='技术候选已成立；'+('已交执行意图，成交另报' if decision and decision[0] else '未报单：'+str(decision[1] if decision else '待执行核对'))
  else:why=WHY.get(entry.get('reason'),str(entry.get('reason') or '本根无有效入场路径'))
  if not fresh:why='执行状态陈旧，仅展示历史记录；'+why
  elif cs in active and not bar_fresh:why='本轮M15尚未更新，仅展示最近记录；'+why
  if product.get('frozen') or product.get('fault'):why='品种停开：'+str(product.get('fault'))+'；'+why
  observation=row.get('trend_observation') if aligned else None
  if observation and (observation.get('actual_symbol')!=product.get('symbol') or observation.get('bar_eob')!=eob):observation=None
  terminal={}
  if entry.get('evidence_id'):
   try:terminal=(read(root/'state/paper_observation_five/execution.json').get('candidate_status') or {}).get(cs+'|'+entry['evidence_id'],{})
   except (OSError,ValueError,KeyError):pass
   if terminal.get('status')=='SUBMIT_OUTCOME_UNKNOWN':why='提交结果未知，禁止重发；等待柜台对账'
   if terminal.get('status') in ('BLOCKED','INVALIDATED','EXPIRED','EXPIRED_BEFORE_QUEUE','FUTURE_CLOCK_REJECTED','ABORTED_RESTART','LOCAL_REJECTED_BEFORE_NATIVE'):
    why='技术候选已终止，未下单：'+str(terminal.get('reason') or terminal['status'])
  result.append(dict(candidate_plan=None if not aligned else row.get('candidate_plan'),execution_terminal=terminal,csymbol=cs,name=name,symbol=product.get('symbol'),direction=direction,label=DIR.get(direction,'未知，不能判断'),eob=eob,active=cs in active,
   state_fresh=fresh and (bar_fresh or cs not in active),reason=why,entry=entry,evidence=(row.get('facts') or {}) if aligned else {},trend_observation=observation,previous_evidence=previous.get(cs,{}).get('facts',{}) if aligned else {},since_bar_id=state.get('since_bar_id'),blocked_dir=state.get('blocked_dir'),leave_votes=state.get('leave_votes'),h1_closed_at=product.get('core',{}).get('last_finalized_h1')))
 return s,result

def deliver(client,key,title,now):
 root=client/'_strategy_runtime';sys.path.insert(0,str(root));from futures_trend_quant.live.paper_notify import FeishuOutbox,message
 s,items=rows(root,now)
 if not items:return None
 fields=[(x['name']+' '+str(x['symbol']),'S0/M15背景：'+badge(x['direction'])+('（休市，沿用最近判断）' if not x['active'] else '（历史记录）' if not x['state_fresh'] else '')+('\n'+trend_risk(x) if trend_risk(x) else '')) for x in items]
 broker=s.get('broker') or {}
 fields.append(('账户执行状态','⛔ 停止新开：'+str(broker.get('halt_reason') or 'pause_new=true') if broker.get('pause_new') or broker.get('halt_reason') else '账户待对账，不能判定可报单' if broker.get('status')!='RECONCILED' else '对账正常；仍须按路径、报价、仓位与时效核验，非下单许可'))
 if broker.get('pause_new'):fields.append(('停开来源',str((broker.get('pause_metadata') or {}).get('reason','UNKNOWN_LEGACY_PAUSE'))+'；需受控恢复，进程在线不会自动解除'))
 health=s.get('clock_health') or {}
 if health:fields.append(('时钟健康',str(health.get('status','UNKNOWN'))+'；偏差 '+str(health.get('measured_offset_seconds','UNKNOWN'))+'秒；报价时效门槛仍为3秒'))
 try:
  m5=read(root/'evidence/paper_observation_five/m5_divergence_health.local.json')
  fresh=0<=(now-clock(m5['at'])).total_seconds()<=30
  fields.append(('独立M5提醒资料',str(m5.get('status')) if fresh and not m5.get('errors') else
      'UNKNOWN/DEGRADED：'+','.join(sorted(m5.get('errors',{})))+'；不可据此判断无背驰，主S0与PA另列'))
 except (OSError,ValueError,KeyError,TypeError):fields.append(('独立M5提醒资料','UNKNOWN，缺少当前质量证明'))
 detail=[x['name']+'：'+x['reason']+'；离场确认票 '+str(x['leave_votes'])+('；禁入方向 '+DIR.get(x['blocked_dir'],str(x['blocked_dir'])) if x['blocked_dir'] else '') for x in items]
 for x in items:
  closure=(s['products'].get(x['csymbol'],{}).get('data_closure') or {})
  pending=[freq for freq in ('m1','m15') if closure.get(freq,{}).get('status')=='PENDING_SOURCE_CLOSURE']
  if pending:detail.append(x['name']+'：'+ '/'.join(pending)+'待数据源闭合确认；不把正在形成的柱提升为已完成，不延长原截止。')
  f=x['evidence']
  if f:detail.append(x['name']+'依据：'+ema_description(f,x.get('previous_evidence'))+'；趋势层H1 '+str(f.get('h1_absolute'))+'。')
  from futures_trend_quant.live.trend_observation import lines as observation_lines
  obs=x.get('trend_observation')
  if not obs and f:
   from futures_trend_quant.live.trend_observation import build
   try:obs=build(f,background=x['direction'],episode_start=None if not x.get('since_bar_id') else x['since_bar_id'].rsplit('|',1)[-1])
   except (ValueError,KeyError,TypeError):obs=None
  detail += [x['name']+'：'+line for line in observation_lines(obs,include_divergence=False)]
 ev=root/'evidence/paper_observation_five';bot=FeishuOutbox(root/'config/paper_feishu.local.json',ev,start=False)
 asof=datetime.fromtimestamp(int(now.timestamp())//900*900,TZ)
 title=title.replace('多品种',str(len(items))+'品种')
 outbox_id=bot.enqueue(key,message(title+'｜截至 '+display_time(asof),[],fields=fields,reasons=detail,execution=['色块只表示S0/M15背景，不等于即时涨跌或下单许可。','V5逐路径资格与账户停开分开；仅B的M15柱符号为提示，其余限制保留。','M5新确认背驰独立提醒并区分与授权的关系，不等待本汇总；历史M15结构不冒充新预警。','H1为会话混合时长序列（含短尾桶）；局部跟随资格UNKNOWN不是交易否决。','已确认趋势反转独立提醒；休市只展示最近闭合判断。']))
 folder=ev/'trend_summaries';folder.mkdir(exist_ok=True);path=folder/(hashlib.sha256(key.encode()).hexdigest()+'.json')
 if not path.exists():path.write_text(json.dumps(dict(at=now.isoformat(),key=key,outbox_id=outbox_id,rows=items,host_status_at=s['at'],source='CORE_CHECKPOINT_AND_ALIGNED_SIGNAL_LOG',trade_api_called=False),ensure_ascii=False,indent=2),encoding='utf8')
 return outbox_id

def priority_changes(client,now):
 """Repackage existing confirmed core notices; no inferred or pending flips."""
 root=client/'_strategy_runtime';sys.path.insert(0,str(root));from futures_trend_quant.live.paper_notify import FeishuOutbox,message,atomic_json
 ev=root/'evidence/paper_observation_five';cursor=ev/'trend_priority_cursor.local.json'
 if not cursor.exists():atomic_json(cursor,dict(cutoff=now.isoformat(),processed=[],last_direction={x['csymbol']:x['direction'] for x in rows(root,now)[1] if x['direction'] in ('bull','bear')}));return []
 try:from .paper_notice_details import run_cursor_floor
 except ImportError:from paper_notice_details import run_cursor_floor
 state=run_cursor_floor(ev,read(cursor));atomic_json(cursor,state);done=set(state['processed']);sent=[];status=read(ev/'status.json')
 if 'last_direction' not in state:
  state['last_direction']={x['csymbol']:x['direction'] for x in rows(root,now)[1] if x['direction'] in ('bull','bear')};atomic_json(cursor,state)
 last_direction=state['last_direction']
 def created_order(path):
  try:return clock(read_notice(path)['created_at']).timestamp()
  except (ValueError,KeyError,OSError,TypeError):return float('inf')
 paths=sorted((ev/'notifications').glob('*.json'),key=created_order)
 for path in paths:
  try:
   source=read_notice(path);card=source.get('text',{}).get('card',{})
   if str(card.get('header',{}).get('title',{}).get('content','')).split('｜截至 ',1)[0]!='期货模拟观察 | 趋势方向变更' or source['id'] in done or clock(source['created_at'])<clock(state['cutoff']):continue
   fields={}
   for el in card['elements']:
    for field in el.get('fields',[]):
     k,v=field['text']['content'].split('\n',1);fields[k.strip('*')]=v
   symbol=fields['标的'].rsplit(' ',1)[-1];cs=next(k for k,v in status['products'].items() if v['symbol']==symbol)
   from futures_trend_quant.live.paper_symbol_health import display_status
   if not display_status(root,cs,now,status)['ready']:continue
   eob=fields['M15收盘时间'];old,new=fields['方向变化'].split(' → ')
   from futures_trend_quant.live.paper_symbol_health import current_m15_event
   if not current_m15_event(status,cs,eob):continue
   expected=hashlib.sha256(('trend:'+cs+':'+eob).encode()).hexdigest()
   if cs not in dict(observation_pool(status)) or source['id']!=expected or old not in DIR or new not in DIR or old==new or clock(eob)>now:continue
   previous=old
   reverse={old,new}=={'bull','bear'};ending=new=='none'
   title=('🚨🚨 趋势反转' if reverse else '⚠️ 趋势结束' if ending else '🔔 新趋势确认')+'｜'+fields['标的']+'｜截至 '+display_time(eob)
   reasons=[]
   for el in card['elements']:
    text=el.get('text',{}).get('content','')
    if text.startswith('**交易理由／风控依据**'):reasons=[re.sub(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)',lambda m:display_time(m.group()),line) for line in text.split('\n')[1:]]
   broker=status.get('broker') or {};fresh=0<=(now-clock(status['at'])).total_seconds()<=20
   volumes=sum(int(p.get('volume',0)) for p in broker['positions'] if p.get('symbol')==symbol) if fresh and broker.get('status')=='RECONCILED' and isinstance(broker.get('positions'),list) else None
   change=badge(previous if reverse else old)+' → '+badge(new)+('（经过无趋势阶段）' if reverse and old=='none' else '')
   payload=message(title,[],fields=[('方向变化',change),('特别注意','原方向已反转，请关注原持仓退出回报' if reverse else '原趋势结束，等待新的方向确认' if ending else '新方向获授权，入场仍按原规则'),('当前该合约持仓',str(volumes)+' 手' if volumes is not None else 'UNKNOWN，持仓尚未完成核对'),('成交状态','方向变化不代表已开仓或已平仓')],reasons=reasons,execution=['来自原策略已确认趋势事件；不使用待确认票推测反转。','原方向退出、委托、成交分别由策略原执行链核验；本提醒不发送交易指令。'])
   payload['card']['header']['template']='red' if reverse else 'orange' if ending else 'blue'
   outbox=FeishuOutbox(root/'config/paper_feishu.local.json',ev,start=False);sent.append(outbox.enqueue('priority-trend:'+source['id'],payload));done.add(source['id'])
   if new in ('bull','bear'):last_direction[cs]=new
   elif old in ('bull','bear'):last_direction[cs]=old
  except (ValueError,KeyError,OSError,StopIteration):continue
 if sent:state['processed']=sorted(done);atomic_json(cursor,state)
 return sent

def priority_divergences(client,now):
 """Only newly confirmed core M15 events, never historical validity banners."""
 root=client/'_strategy_runtime';sys.path.insert(0,str(root))
 from futures_trend_quant.live.paper_notify import FeishuOutbox,message,atomic_json
 from futures_trend_quant.live.trend_observation import event_id
 try:from .paper_notice_details import run_cursor_floor
 except ImportError:from paper_notice_details import run_cursor_floor
 ev=root/'evidence/paper_observation_five';path=ev/'m15_divergence_notice_cursor.local.json'
 if not path.exists():atomic_json(path,dict(cutoff=now.isoformat(),processed=[]));return []
 state=run_cursor_floor(ev,read(path));done=set(state['processed']);sent=[];status=read(ev/'status.json')
 if not 0<=(now-clock(status['at'])).total_seconds()<=20:return []
 source=ev/'signals.jsonl'
 if not source.exists():return []
 for line in source.read_text(encoding='utf8').splitlines():
  try:r=json.loads(line)
  except ValueError:continue
  cs=r.get('csymbol');f=r.get('facts') or {};product=status['products'].get(cs,{})
  from futures_trend_quant.live.paper_symbol_health import display_status
  if not display_status(root,cs,now,status)['ready']:continue
  if not product or f.get('symbol')!=product.get('symbol'):continue
  from futures_trend_quant.live.paper_symbol_health import current_m15_event
  if not current_m15_event(status,cs,r.get('bar_eob')):continue
  for side in ('top','bottom'):
   event=f.get(side+'_divergence_event')
   if not event:continue
   confirmed=event['defined_at_bar_id'].rsplit('|',1)[-1]
   if confirmed!=r['bar_eob'] or not clock(state['cutoff'])<clock(confirmed)<=now:continue
   identity=event_id(event);key='m15-divergence:'+cs+'|'+identity
   if key in done:continue
   label='顶背驰' if side=='top' else '底背驰'
   payload=message('⚠️ M15背驰新确认｜截至 '+display_time(confirmed),[],fields=[('标的',str(product.get('symbol'))),('新事件',label+'；极值 '+f"{float(event['extreme_price']):.2f}"),('性质','核心已闭合M15事件；不是即时开平仓指令')],reasons=['只在本根新确认时提醒一次；历史仍有效不重复推送。','确认晚于极值发生；不能把极值时刻当作当时已知。'],execution=['实际识别 '+display_time(now)+'；不等待15分钟总览卡片。','是否限制新开仍由现行逐路径政策决定；不放宽背驰限制。'])
   out=FeishuOutbox(root/'config/paper_feishu.local.json',ev,start=False).enqueue(key,payload);sent.append(out);done.add(key)
 state['processed']=sorted(done);atomic_json(path,state);return sent


def main():
 p=argparse.ArgumentParser();p.add_argument('--client',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--once',action='store_true');a=p.parse_args();client=a.client.resolve();root=client/'_strategy_runtime';ev=root/'evidence/paper_observation_five'
 if a.once:
  now=datetime.now(TZ);print(deliver(client,'trend-summary:first:'+now.strftime('%Y%m%d%H%M'),'多品种趋势方向总览',now));return
 import msvcrt
 lock=(ev/'trend_notifier.lock').open('a+b');lock.seek(0)
 if lock.seek(0,2)==0:lock.write(b'0');lock.flush()
 lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
 try:from .paper_notice_details import candidate_details,M5DivergenceMonitor,NOTICE_POLICY
 except ImportError:from paper_notice_details import candidate_details,M5DivergenceMonitor,NOTICE_POLICY
 monitor=M5DivergenceMonitor(client)
 from futures_trend_quant.live.paper_progress import TaskProgress
 progress=TaskProgress(ev/'trend_reminder_health.local.json',NOTICE_POLICY)
 last_queued=None;last_slot=None
 while True:
  now=datetime.now(TZ);slot=int(now.timestamp())//900*900;delay=now.timestamp()-slot
  try:
   progress.run('PRIORITY_CHANGES',lambda:priority_changes(client,now))
   active=active_symbols(root,slot-1)
   if active and delay>=15 and delay<300 and slot!=last_slot:
    status=read(ev/'status.json')
    from futures_trend_quant.live.paper_symbol_health import display_status
    active={cs for cs in active if display_status(root,cs,now,status)['ready']}
    ready=bool(active) and all(clock(status['products'][cs]['core']['last_eob']).timestamp()>=slot for cs in active)
    if ready or delay>=90:
     key='trend-summary:m15:'+datetime.fromtimestamp(slot,TZ).isoformat();last_queued=progress.run('M15_SUMMARY',lambda:deliver(client,key,'15分钟·多品种趋势判断',now));last_slot=slot
   for name,callback in [('CANDIDATE_DETAILS',lambda:candidate_details(client,now)),('M15_DIVERGENCE',lambda:priority_divergences(client,now))]:
    try:progress.run(name,callback)
    except Exception as exc:
     with (ev/'trend_reminder_errors.local.log').open('a',encoding='utf8') as f:f.write(now.isoformat()+' '+name+' '+type(exc).__name__+'\n')
   monitor.poll_progress(progress)
  except Exception as exc:
   with (ev/'trend_reminder_errors.local.log').open('a',encoding='utf8') as f:f.write(now.isoformat()+' '+type(exc).__name__+'\n')
  time.sleep(3)
if __name__=='__main__':main()
