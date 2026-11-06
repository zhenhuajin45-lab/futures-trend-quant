"""Read-only 15-minute trend cards; reuses the strategy's private Feishu outbox."""
from pathlib import Path
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
import argparse,hashlib,json,math,os,re,sys,time
TZ=ZoneInfo('Asia/Shanghai')
POOL=[('DCE.M','豆粕'),('SHFE.AU','黄金'),('DCE.EG','乙二醇'),('DCE.JD','鸡蛋'),('CZCE.CJ','红枣')]
DIR={'bull':'偏多','bear':'偏空','none':'暂无确认方向'}
ICONS={'bull':'🟥','bear':'🟩','none':'⬜'}
WHY={'NO_PATH':'入场路径条件未到齐','NO_CONFIRMED_TREND':'趋势方向尚未确认','COOLDOWN':'冷却中','POSITION_EXISTS':'已有同向持仓'}
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))

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
  try:
   cp=read(root/('state/paper_observation_five/products/'+cs.replace('.','_')+'/core_checkpoint.json'));state=cp['runtime']['trends'][cs];eob=cp['last_eob'];direction=state['trend_state']
   if clock(eob)>now:raise ValueError('future checkpoint')
   if state['last_closed_bar_id'].rsplit('|',1)[-1]!=eob:raise ValueError('checkpoint bar mismatch')
  except (ValueError,KeyError,OSError):state={};eob=None;direction='UNKNOWN'
  bar_fresh=bool(eob and clock(eob).timestamp()>=int(now.timestamp())//900*900)
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
  result.append(dict(csymbol=cs,name=name,symbol=product.get('symbol'),direction=direction,label=DIR.get(direction,'未知，不能判断'),eob=eob,active=cs in active,
   state_fresh=fresh and (bar_fresh or cs not in active),reason=why,evidence=(row.get('facts') or {}) if aligned else {},trend_observation=observation,previous_evidence=previous.get(cs,{}).get('facts',{}) if aligned else {},since_bar_id=state.get('since_bar_id'),blocked_dir=state.get('blocked_dir'),leave_votes=state.get('leave_votes'),h1_closed_at=product.get('core',{}).get('last_finalized_h1')))
 return s,result

def deliver(client,key,title,now):
 root=client/'_strategy_runtime';sys.path.insert(0,str(root));from futures_trend_quant.live.paper_notify import FeishuOutbox,message
 s,items=rows(root,now);fields=[(x['name']+' '+str(x['symbol']),'S0/M15背景：'+badge(x['direction'])+('（休市，沿用最近判断）' if not x['active'] else '（历史记录）' if not x['state_fresh'] else '')+('\n'+trend_risk(x) if trend_risk(x) else '')) for x in items]
 detail=[x['name']+'：'+x['reason']+'；离场确认票 '+str(x['leave_votes'])+('；禁入方向 '+DIR.get(x['blocked_dir'],str(x['blocked_dir'])) if x['blocked_dir'] else '') for x in items]
 for x in items:
  f=x['evidence']
  if f:detail.append(x['name']+'依据：'+ema_description(f,x.get('previous_evidence'))+'；趋势层H1 '+str(f.get('h1_absolute'))+'。')
  from futures_trend_quant.live.trend_observation import lines as observation_lines
  obs=x.get('trend_observation')
  if not obs and f:
   from futures_trend_quant.live.trend_observation import build
   try:obs=build(f,background=x['direction'],episode_start=None if not x.get('since_bar_id') else x['since_bar_id'].rsplit('|',1)[-1])
   except (ValueError,KeyError,TypeError):obs=None
  detail += [x['name']+'：'+line for line in observation_lines(obs)]
 ev=root/'evidence/paper_observation_five';bot=FeishuOutbox(root/'config/paper_feishu.local.json',ev,start=False)
 asof=datetime.fromtimestamp(int(now.timestamp())//900*900,TZ)
 outbox_id=bot.enqueue(key,message(title+'｜截至 '+display_time(asof),[],fields=fields,reasons=detail,execution=['色块只表示S0/M15已确认背景；H1背景、短动能、阶段和原开仓权限分开。','H1为会话混合时长序列（含15/30分钟尾桶），不能等同MT5原生60分钟柱。','历史结构有效不等于当前预警；现V1否决政策未改变，状态说明仅采用本项目闭合事实。','局部结构/观察跟随资格UNKNOWN不影响原S0权限；账户、报价及期限仍须另验。','已确认趋势反转即时单独提醒，不等待本汇总。','休市品种仅展示最近判断；历史记录不代表标题时刻已更新。']))
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
  try:return clock(read(path)['created_at']).timestamp()
  except (ValueError,KeyError,OSError,TypeError):return float('inf')
 paths=sorted((ev/'notifications').glob('*.json'),key=created_order)
 for path in paths:
  try:
   source=read(path);card=source.get('text',{}).get('card',{})
   if str(card.get('header',{}).get('title',{}).get('content','')).split('｜截至 ',1)[0]!='期货模拟观察 | 趋势方向变更' or source['id'] in done or clock(source['created_at'])<clock(state['cutoff']):continue
   fields={}
   for el in card['elements']:
    for field in el.get('fields',[]):
     k,v=field['text']['content'].split('\n',1);fields[k.strip('*')]=v
   symbol=fields['标的'].rsplit(' ',1)[-1];cs=next(k for k,v in status['products'].items() if v['symbol']==symbol)
   eob=fields['M15收盘时间'];old,new=fields['方向变化'].split(' → ')
   expected=hashlib.sha256(('trend:'+cs+':'+eob).encode()).hexdigest()
   if cs not in dict(observation_pool(status)) or source['id']!=expected or old not in DIR or new not in DIR or old==new or clock(eob)>now:continue
   previous=old if old in ('bull','bear') else last_direction.get(cs,old)
   reverse={previous,new}=={'bull','bear'};ending=new=='none'
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

def main():
 p=argparse.ArgumentParser();p.add_argument('--client',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--once',action='store_true');a=p.parse_args();client=a.client.resolve();root=client/'_strategy_runtime';ev=root/'evidence/paper_observation_five'
 if a.once:
  now=datetime.now(TZ);print(deliver(client,'trend-summary:first:'+now.strftime('%Y%m%d%H%M'),'五品种趋势方向总览',now));return
 import msvcrt
 lock=(ev/'trend_notifier.lock').open('a+b');lock.seek(0)
 if lock.seek(0,2)==0:lock.write(b'0');lock.flush()
 lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
 try:from .paper_notice_details import candidate_details,M5DivergenceMonitor
 except ImportError:from paper_notice_details import candidate_details,M5DivergenceMonitor
 monitor=M5DivergenceMonitor(client)
 last_health=0;last_queued=None;last_slot=None
 priority_changes(client,datetime.now(TZ))
 while True:
  now=datetime.now(TZ);slot=int(now.timestamp())//900*900;delay=now.timestamp()-slot
  try:
   priority_changes(client,now)
   active=active_symbols(root,slot-1)
   if active and delay>=15 and delay<300 and slot!=last_slot:
    status=read(ev/'status.json');ready=all(clock(status['products'][cs]['core']['last_eob']).timestamp()>=slot for cs in active)
    if ready or delay>=90:
     key='trend-summary:m15:'+datetime.fromtimestamp(slot,TZ).isoformat();last_queued=deliver(client,key,'15分钟·五品种趋势判断',now);last_slot=slot
   if time.monotonic()-last_health>=10:
    body=dict(pid=os.getpid(),at=now.isoformat(),last_outbox_id=last_queued,interval='CLOSED_M15_PLUS_15_SECONDS;90_SECONDS_TIMEOUT_LABELLED',trade_api_called=False)
    tmp=ev/'trend_reminder_health.tmp';tmp.write_text(json.dumps(body),encoding='utf8');os.replace(tmp,ev/'trend_reminder_health.local.json');last_health=time.monotonic()
   for name,callback in [('CANDIDATE_DETAILS',lambda:candidate_details(client,now)),('M5_DIVERGENCE',lambda:monitor.poll(now))]:
    try:callback()
    except Exception as exc:
     with (ev/'trend_reminder_errors.local.log').open('a',encoding='utf8') as f:f.write(now.isoformat()+' '+name+' '+type(exc).__name__+'\n')
  except Exception as exc:
   with (ev/'trend_reminder_errors.local.log').open('a',encoding='utf8') as f:f.write(now.isoformat()+' '+type(exc).__name__+'\n')
  time.sleep(3)
if __name__=='__main__':main()
