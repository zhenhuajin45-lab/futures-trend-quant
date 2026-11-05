"""Read-only 15-minute trend cards; reuses the strategy's private Feishu outbox."""
from pathlib import Path
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
import argparse,hashlib,json,os,sys,time
TZ=ZoneInfo('Asia/Shanghai')
POOL=[('DCE.M','豆粕'),('SHFE.AU','黄金'),('DCE.EG','乙二醇'),('DCE.JD','鸡蛋'),('CZCE.CJ','红枣')]
DIR={'bull':'偏多','bear':'偏空','none':'暂无确认方向'}
WHY={'NO_PATH':'入场路径条件未到齐','NO_CONFIRMED_TREND':'趋势方向尚未确认','COOLDOWN':'冷却中','POSITION_EXISTS':'已有同向持仓'}
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def clock(t):return datetime.fromisoformat(str(t).replace('Z','+00:00'))
def active_symbols(root,t):
 registry=read(root/'state/paper_observation_five/market_registry.local.json');return {v['csymbol'] for v in registry['contracts'].values() if any(a<=t<b for a,b in v['trade_windows'])}
def rows(root,now):
 s=read(root/'evidence/paper_observation_five/status.json');fresh=0<=(now-clock(s['at'])).total_seconds()<=20;latest={};p=root/'evidence/paper_observation_five/signals.jsonl'
 if p.exists():
  for line in p.read_text(encoding='utf8').splitlines():
   try:r=json.loads(line);latest[r['csymbol']]=r
   except (ValueError,KeyError):continue
 active=active_symbols(root,now.timestamp());result=[]
 for cs,name in POOL:
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
  result.append(dict(csymbol=cs,name=name,symbol=product.get('symbol'),direction=direction,label=DIR.get(direction,'未知，不能判断'),eob=eob,active=cs in active,
   state_fresh=fresh and (bar_fresh or cs not in active),reason=why,evidence=(row.get('facts') or {}) if aligned else {},since_bar_id=state.get('since_bar_id'),blocked_dir=state.get('blocked_dir'),leave_votes=state.get('leave_votes'),h1_closed_at=product.get('core',{}).get('last_finalized_h1')))
 return s,result

def deliver(client,key,title,now):
 root=client/'_strategy_runtime';sys.path.insert(0,str(root));from futures_trend_quant.live.paper_notify import FeishuOutbox,message
 s,items=rows(root,now);fields=[(x['name']+' '+str(x['symbol']),x['label']+('（最近记录）' if not x['state_fresh'] or not x['active'] else '')+'\n截至 '+str(x['eob'] or 'UNKNOWN')) for x in items]
 detail=[x['name']+'：'+x['reason']+'；离场确认票 '+str(x['leave_votes'])+('；禁入方向 '+DIR.get(x['blocked_dir'],str(x['blocked_dir'])) if x['blocked_dir'] else '') for x in items]
 for x in items:
  f=x['evidence']
  if f:detail.append(x['name']+'依据：M15收盘 '+str(f.get('close'))+'，EMA34 '+str(f.get('ema34'))+'，斜率 '+str(f.get('slope_state'))+'；趋势层H1 '+str(f.get('h1_absolute'))+'。')
 ev=root/'evidence/paper_observation_five';bot=FeishuOutbox(root/'config/paper_feishu.local.json',ev,start=False)
 outbox_id=bot.enqueue(key,message(title,[],fields=fields,reasons=detail,execution=['生成时刻 '+now.isoformat(),'趋势来自实际核心检查点；方向授权不等于开仓信号。','已确认趋势反转由原策略即时单独通知，不等待本汇总。','休市品种仅展示最近日盘判断；数据过期不会伪装成实时判断。']))
 folder=ev/'trend_summaries';folder.mkdir(exist_ok=True);path=folder/(hashlib.sha256(key.encode()).hexdigest()+'.json')
 if not path.exists():path.write_text(json.dumps(dict(at=now.isoformat(),key=key,outbox_id=outbox_id,rows=items,host_status_at=s['at'],source='CORE_CHECKPOINT_AND_ALIGNED_SIGNAL_LOG',trade_api_called=False),ensure_ascii=False,indent=2),encoding='utf8')
 return outbox_id

def main():
 p=argparse.ArgumentParser();p.add_argument('--client',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--once',action='store_true');a=p.parse_args();client=a.client.resolve();root=client/'_strategy_runtime';ev=root/'evidence/paper_observation_five'
 if a.once:
  now=datetime.now(TZ);print(deliver(client,'trend-summary:first:'+now.strftime('%Y%m%d%H%M'),'五品种趋势方向总览',now));return
 import msvcrt
 lock=(ev/'trend_notifier.lock').open('a+b');lock.seek(0)
 if lock.seek(0,2)==0:lock.write(b'0');lock.flush()
 lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
 last_health=0;last_queued=None;last_slot=None
 while True:
  now=datetime.now(TZ);slot=int(now.timestamp())//900*900;delay=now.timestamp()-slot
  try:
   active=active_symbols(root,slot-1)
   if active and delay>=15 and delay<300 and slot!=last_slot:
    status=read(ev/'status.json');ready=all(clock(status['products'][cs]['core']['last_eob']).timestamp()>=slot for cs in active)
    if ready or delay>=90:
     key='trend-summary:m15:'+datetime.fromtimestamp(slot,TZ).isoformat();last_queued=deliver(client,key,'15分钟·五品种趋势判断',now);last_slot=slot
   if time.monotonic()-last_health>=10:
    body=dict(pid=os.getpid(),at=now.isoformat(),last_outbox_id=last_queued,interval='CLOSED_M15_PLUS_15_SECONDS;90_SECONDS_TIMEOUT_LABELLED',trade_api_called=False)
    tmp=ev/'trend_reminder_health.tmp';tmp.write_text(json.dumps(body),encoding='utf8');os.replace(tmp,ev/'trend_reminder_health.local.json');last_health=time.monotonic()
  except Exception as exc:
   with (ev/'trend_reminder_errors.local.log').open('a',encoding='utf8') as f:f.write(now.isoformat()+' '+type(exc).__name__+'\n')
  time.sleep(3)
if __name__=='__main__':main()
