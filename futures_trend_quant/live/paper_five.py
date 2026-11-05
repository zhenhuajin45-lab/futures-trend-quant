"""Five-product S0 paper observation. One account engine, independent product facts/clocks."""
from collections import Counter
from pathlib import Path
from datetime import datetime,timezone
import argparse,hashlib,json,multiprocessing,os,queue,subprocess,time
import numpy as np
import pandas as pd
from .paper_config import ROOT,load_paper_config
from .paper_products import FIVE_POOL,PRODUCTS,product_of
from .paper_calendar import PortfolioCalendar,PaperCalendar
from .paper_broker import PaperEngine,atomic,stamp,TERMINAL
from .paper_data import MarketWorker,SignalWorker
from .paper_transport import SimTransport
from .paper_runner import PaperRunner,elapsed_review_slots
from .paper_notify import FeishuOutbox,message
from .paper_explain import context as reason_context,lines as reason_lines,EXITS,DIRECTIONS,closing_gross


def contract_record(meta,calendar):
    spec=meta['spec'];values=[float(spec[k]) for k in ('price_tick','multiplier','margin_ratio')]
    if not all(np.isfinite(x) and x>0 for x in values) or values[2]>1:raise ValueError('invalid official contract metadata')
    ns=calendar.ns;cuts=np.r_[0,np.flatnonzero(np.diff(ns)>60_000_000_000)+1,len(ns)]
    trade=[(int(ns[a]-60_000_000_000)/1e9,int(ns[b-1])/1e9) for a,b in zip(cuts[:-1],cuts[1:])]
    p=calendar.policy.products[calendar.cs]
    blocks=list(p['blackouts'])+[(v-300_000_000_000,v) for v in p['day_closes'].values()]
    entry=[]
    for a,b in trade:
        pieces=[(a,b)]
        for x,y in blocks:
            x/=1e9;y/=1e9;updated=[]
            for c,d in pieces:
                if y<=c or x>=d:updated.append((c,d))
                else:
                    if c<x:updated.append((c,min(x,d)))
                    if y<d:updated.append((max(y,c),d))
            pieces=updated
        entry.extend(pieces)
    return dict(csymbol=meta['csymbol'],symbol=meta['symbol'],tick=values[0],multiplier=values[1],margin_ratio=values[2],
        trade_windows=trade,entry_windows=entry,source='GM_ACTUAL_CONTRACT_METADATA_AND_EXCHANGE_CALENDAR')


def quote_process(inbox,events,evidence_dir):
    from gm.api import set_token,current
    set_token(os.environ['GM_TOKEN']);records={};last=0.;errors=0
    path=Path(evidence_dir)/'quote_seen.jsonl'
    while True:
        try:
            while True:records=inbox.get_nowait()
        except queue.Empty:pass
        now=time.time();active={s:r for s,r in records.items() if any(a<=now<b for a,b in r['trade_windows'])}
        wanted=active if active else records
        interval=1 if active else 30
        if wanted and time.monotonic()-last>=interval:
            try:
                with path.open('a',encoding='utf8') as f:f.write(json.dumps(dict(requested_at=datetime.now(timezone.utc).isoformat(),purpose='PAPER_OBSERVATION_SEEN',symbols=sorted(wanted),source='GM_BULK_CURRENT_NOT_TICK_STREAM'))+'\n')
                rows=current(symbols=','.join(sorted(wanted)));received=pd.Timestamp.now(tz='Asia/Shanghai').isoformat()
                for row in rows or []:
                    symbol=row['symbol']
                    if symbol not in wanted:raise ValueError('foreign quote symbol')
                    book=row.get('quotes') or [];level=book[0] if book else {}
                    q=dict(created_at=str(row['created_at']),last=row.get('price'),tick=wanted[symbol]['tick'],
                        bid=level.get('bid_p',0),ask=level.get('ask_p',0),bid_size=level.get('bid_v',0),ask_size=level.get('ask_v',0),source='GM_BULK_CURRENT_POLL')
                    events.put(('QUOTE',symbol,q,received))
                errors=0
            except Exception as exc:
                errors+=1;events.put(('QUOTE_ERROR',type(exc).__name__,errors))
            last=time.monotonic()
        time.sleep(min(10,2**min(errors,3)) if errors else .2 if active else 1)


class FiveRunner:
    _acquire_lock=PaperRunner._acquire_lock
    event=PaperRunner.event
    def __init__(self,path,readonly=False):
        self.config_path=Path(path).resolve();self.c=load_paper_config(path);self.readonly=readonly
        if self.c['schema_version']!=2:raise ValueError('five-product schema required')
        self.started=pd.Timestamp.now(tz='Asia/Shanghai');self.run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        if readonly:
            base=ROOT/'data/cache/five_preflight'/self.run_id;self.c['state_dir']=str(base/'state');self.c['evidence_dir']=str(base/'evidence')
        self.evidence=Path(self.c['evidence_dir']);self.evidence.mkdir(parents=True,exist_ok=True);self._acquire_lock()
        if not readonly and not (Path(self.c['state_dir'])/'migration.local.json').exists():raise ValueError('validated account handoff manifest required')
        self.calendar=PortfolioCalendar();self.products={};self.records={};self.engine=None;self.verified=False;self.readonly_snapshot=None
        self.transport=SimTransport(self.c,self.config_path);ctx=multiprocessing.get_context('spawn')
        self.quote_in=ctx.Queue();self.quote_events=ctx.Queue()
        self.quote_worker=ctx.Process(target=quote_process,args=(self.quote_in,self.quote_events,str(self.evidence)),daemon=True,name='paper-five-quotes')
        self.counts=Counter();self.fill_notified=set();self.last_poll=0.;self.last_report=0.;self.max_gap=0.;self.last_loop=time.monotonic()
        self.notifications=None if readonly else FeishuOutbox(ROOT/self.c['notification_config'],self.evidence)
        self.cursor_path=Path(self.c['state_dir'])/'notification_cursor.json'
        orderfile=self.evidence/'order_events.jsonl'
        saved=json.loads(self.cursor_path.read_text(encoding='utf8')) if self.cursor_path.exists() else {}
        self.order_offset=saved.get('order_offset',orderfile.stat().st_size if orderfile.exists() else 0)
        self.start_notice=False;self.last_problem=None
        for cs in self.c['universe']:
            key=cs.replace('.','_');child=dict(self.c,csymbol=cs,quote_poll_enabled=False,
                state_dir=str(Path(self.c['state_dir'])/'products'/key),evidence_dir=str(self.evidence/'products'/key))
            Path(child['state_dir']).mkdir(parents=True,exist_ok=True);Path(child['evidence_dir']).mkdir(parents=True,exist_ok=True)
            product=dict(config=child,market=MarketWorker(child),core=SignalWorker(child),metadata=None,calendar=None,
                coverage={},ready=False,frozen=False,fault=None,counts=Counter(),last_positions=None,last_trend=None,received={})
            self.products[cs]=product
            cache=Path(child['evidence_dir'])/'market_metadata.local.json'
            if cache.exists():
                m=json.loads(cache.read_text(encoding='utf8'));cal=PaperCalendar(m['dates'],m['sessions'],cs);self.register(cs,m,cal)
        if self.calendar.contracts and not readonly:self.engine=PaperEngine(self.c,self.calendar,self.transport)
        self.quote_worker.start()
        for p in self.products.values():p['market'].thread.start();p['core'].thread.start()
        self.observation_started=self.started
        history=self.evidence/'observation_start.json'
        if history.exists():self.observation_started=stamp(json.loads(history.read_text())['started_at'])
        else:atomic(history,dict(started_at=self.started.isoformat(),pool=self.c['universe']))
        manifest=dict(schema_version=2,run_id=self.run_id,started_at=self.started.isoformat(),readonly=readonly,
            code_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            universe=self.c['universe'],strategy='S0_SHORT_PAPER_V1',live_orders_enabled=False,config_sha256=self.c['config_sha256'],
            market='GM_BULK_CURRENT_PLUS_PER_PRODUCT_CLOSED_M1_POLL',streaming_subscriptions=0,
            base_rules_unchanged=True,review_mode='SINGLE_AGENT_SELF_CHECK',independent_score_run=False,
            source_sha256_lf={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest() for p in (ROOT/'futures_trend_quant/live').glob('paper_*.py')})
        atomic(self.evidence/('run_manifest_'+self.run_id+'.json'),manifest);atomic(self.evidence/'run_manifest.json',manifest)

    def register(self,cs,meta,calendar):
        record=contract_record(meta,calendar)
        if product_of(meta['symbol'])!=cs:raise ValueError('metadata product mismatch')
        self.products[cs].update(metadata=meta,calendar=calendar)
        self.calendar.register(meta['symbol'],calendar);self.records[meta['symbol']]=record
        atomic(Path(self.c['state_dir'])/'market_registry.local.json',dict(config_sha256=self.c['config_sha256'],contracts=self.records))
        self.quote_in.put(dict(self.records))

    def notice(self,key,title,lines,tag='STRATEGY',fields=None,reasons=None,execution=None):
        if self.notifications:self.notifications.enqueue(key,message(title,lines,tag,fields,reasons,execution))

    def market_events(self,now):
        for cs,p in self.products.items():
            while True:
                try:e=p['market'].events.get_nowait()
                except queue.Empty:break
                kind=e[0]
                if kind=='METADATA':
                    try:self.register(cs,e[1],e[2])
                    except Exception as exc:
                        p.update(frozen=True,fault='METADATA_INVALID');self.event('METADATA_INVALID',csymbol=cs,category=type(exc).__name__);continue
                    if self.engine is None and not self.readonly:self.engine=PaperEngine(self.c,self.calendar,self.transport)
                elif kind=='INITIAL_CACHE':
                    p['coverage']=e[2];p['core'].inbox.put(('INITIAL_CACHE',e[1]))
                    self.event('PRODUCT_WARMUP',csymbol=cs,coverage=e[2])
                elif kind=='NEW_BARS':
                    if p['calendar'] is None:continue
                    for bar in e[1]:
                        if self.engine:self.engine.m1(bar,now)
                        day=p['calendar'].day(bar.bob);p['received'].setdefault(day,set()).add(str(bar.eob))
                        with (Path(p['config']['evidence_dir'])/'m1_observed.jsonl').open('a',encoding='utf8') as f:
                            f.write(json.dumps(dict(symbol=bar.symbol,bob=str(bar.bob),eob=str(bar.eob),open=bar.open,high=bar.high,low=bar.low,close=bar.close,
                                volume=bar.volume,local_observed_at=str(now),purpose='PAPER_OBSERVATION_SEEN'))+'\n')
                    p['core'].inbox.put(('NEW',e[1]))
                elif kind in ('DATA_FAILED','DATA_GAP','ROLLOVER_FREEZE'):
                    p.update(frozen=True,fault=kind);self.event(kind,csymbol=cs,detail=e[1])
                    self.notice('data:'+self.run_id+':'+cs+':'+kind,'单品种停止新开',[PRODUCTS[cs]['name']+' '+cs,kind,'已知持仓仍由账户执行器监督退出；其余品种独立检查。'])
        while True:
            try:e=self.quote_events.get_nowait()
            except queue.Empty:break
            if e[0]=='QUOTE':
                _,symbol,q,received=e
                if self.engine:self.engine.quote(symbol,q,received)
                with (self.evidence/'quotes.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(dict(symbol=symbol,quote=q,local_received_at=received))+'\n')
            else:self.event('QUOTE_SOURCE_ERROR',category=e[1],consecutive=e[2])

    def core_events(self,now):
        for cs,p in self.products.items():
            while True:
                try:e=p['core'].outbox.get_nowait()
                except queue.Empty:break
                kind=e[0]
                if kind in ('CORE_PROGRESS','CORE_READY'):
                    p['core'].progress=e[1]
                    if kind=='CORE_READY':p['ready']=True
                elif kind in ('WARMUP_FAILED','CORE_FAILED'):
                    p.update(ready=False,frozen=True,fault=kind);self.event(kind,csymbol=cs,detail=e[1])
                    self.notice('core:'+self.run_id+':'+cs+':'+kind,'策略数据不可用',[PRODUCTS[cs]['name']+' '+cs,kind,'该品种暂停新开，不填零产生信号。'])
                elif kind=='CORE_RESULT':
                    result,bar=e[1],e[2];now=pd.Timestamp.now(tz='Asia/Shanghai');entry=result.entry;decision=None
                    p['counts']['M15_DECISIONS']+=1;self.counts['M15_DECISIONS']+=1
                    explanation=reason_context(result)
                    if self.engine and result.trend.confirmed:self.engine.force_exit(bar.symbol,'TREND_EXIT',now,direction=result.trend.confirmed_state,context=explanation)
                    if result.trend_changed:
                        self.notice('trend:'+cs+':'+bar.eob.isoformat(),'趋势方向变更',reason_lines(explanation),
                            fields=[('标的',PRODUCTS[cs]['name']+' '+bar.symbol),('方向变化',str(result.prior_trend)+' → '+str(result.trend.confirmed_state)),
                                ('M15收盘时间',bar.eob.isoformat()),('入场判定',str(None if entry is None else entry.reason))],
                            execution=['方向授权变化不代表已有成交；入场、风控和委托回报分别核验。'])
                    if entry is not None and entry.accepted:
                        p['counts']['TECHNICAL_SIGNALS']+=1
                        if self.engine and p['ready'] and p['coverage'].get('qualified') and not p['frozen'] and result.anchor is not None:
                            spec=p['metadata']['spec'];atr=result.analysis.indicators.atr14[-1]
                            signal=dict(symbol=bar.symbol,csymbol=cs,direction=entry.direction,
                                evidence_id=cs+'|'+entry.evidence_id,core_evidence_id=entry.evidence_id,signal_at=bar.eob.isoformat(),
                                anchor=result.anchor.anchor_price,atr=float(atr),multiplier=float(spec['multiplier']),margin_ratio=float(spec['margin_ratio']),
                                rules=self.c['base_config'].raw['rules'],path=entry.path,m15_index=result.m15_index)
                            signal['trade_reason']=explanation
                            decision=self.engine.open(signal,now)
                        else:decision=(False,'PRODUCT_NOT_READY_OR_READONLY')
                        p['counts']['ORDER_ALLOWED' if decision[0] else 'BLOCKED_'+decision[1]]+=1
                        self.notice('signal:'+cs+':'+entry.evidence_id,'入场候选',[PRODUCTS[cs]['name']+' '+bar.symbol,
                            ('做多' if entry.direction=='bull' else '做空')+'；路径 '+str(entry.path),'信号收盘 '+bar.eob.isoformat(),
                            '计算完成 '+str(e[3]),'处理：'+('已生成委托意图，尚未确认成交' if decision[0] else '未报单：'+decision[1]),
                            '每笔最多1手；实际成交另发通知；D+1和休市前清仓。'],
                            fields=[('标的',PRODUCTS[cs]['name']+' '+bar.symbol),('方向',DIRECTIONS.get(entry.direction,entry.direction)),
                                ('动作／状态','候选；委托意图已生成' if decision[0] else '候选；未报单 '+decision[1]),('触发路径',str(entry.path)),
                                ('信号收盘时间',bar.eob.isoformat()),('计划手数','最多1手，同品种不加仓')],
                            reasons=reason_lines(explanation),
                            execution=['处理结果 '+str(decision),'计算完成 '+str(e[3]),'实际成交、止损目标与截止时点以成交卡片及柜台持仓为准。'])
                    row=dict(csymbol=cs,bar_eob=bar.eob.isoformat(),signal_ready_at=e[3],order_evaluated_at=str(now),facts=result.facts_row,
                        entry=None if entry is None else entry.__dict__,paper_decision=decision)
                    with (self.evidence/'signals.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')

    def pump_notifications(self):
        if not self.notifications or not self.engine:return
        path=self.evidence/'order_events.jsonl'
        if not path.exists():return
        with path.open('rb') as f:
            f.seek(self.order_offset)
            for raw in f:
                e=json.loads(raw);kind=e['kind'];tag=e.get('tag','STRATEGY')
                if kind=='EXECUTION_REPORT':
                    fill=e['execution'];i=next((x for x in self.engine.s['intents'].values() if x.get('order_id')==fill['cl_ord_id']),None)
                    if i:
                        meta=self.engine.s['meta'].get(self.engine.key(i['symbol'],i['direction']),{})
                        opening=i['effect']==1;reason=i.get('trade_reason',{}) if opening else i.get('exit_evidence',{})
                        why=reason_lines(reason) if opening and reason else [EXITS.get(i.get('reason'),str(i.get('reason','原因待核')))]
                        if not opening and reason:why+=reason_lines(reason)
                        self.notice('fill:'+fill['exec_id'],'入场已成交' if opening else '离场已成交',[
                            PRODUCTS[i['csymbol']]['name']+' '+i['symbol']+' · '+('多' if i['direction']=='bull' else '空'),
                            '成交 '+str(fill['volume'])+' 手，价格 '+str(fill['price']),'平台模拟手续费 '+str(fill.get('commission','UNKNOWN')),
                            '保护止损 '+str(meta.get('stop','UNKNOWN'))+'；2R目标 '+str(meta.get('target','UNKNOWN')),
                            '最迟启动清仓 '+str(meta.get('deadline','UNKNOWN')),
                            '原因 '+str(i.get('reason',i.get('path','UNKNOWN'))),
                            '成交回报不替代柜台余量核对；净经济性尚未验证。'],i['tag'],
                            fields=[('标的',PRODUCTS[i['csymbol']]['name']+' '+i['symbol']),('持仓方向',DIRECTIONS[i['direction']]),
                                ('明确动作',('买入开多' if i['side']==1 else '卖出开空') if opening else ('卖出平多' if i['side']==2 else '买入平空')),('模拟结果','实际成交回报，尚需余量对账'),
                                ('成交价格',str(fill['price'])),('成交手数',str(fill['volume'])),
                                ('保护止损',str(meta.get('stop','UNKNOWN'))),('2R目标',str(meta.get('target','UNKNOWN'))),
                                ('平台本次扣费',str(fill.get('commission','UNKNOWN'))),('清仓截止',str(meta.get('deadline','UNKNOWN')))]+closing_gross(i,fill),
                            reasons=why,execution=['信号时间 '+str(i.get('signal_at',meta.get('opened_at','UNKNOWN'))),
                                '平台成交时点 '+str(fill.get('created_at','UNKNOWN')),'本机记录 '+e['recorded_at'],
                                '相对发令对手价偏差 '+str(i.get('fill_vs_quoted_opponent_ticks','UNKNOWN'))+' tick；不代表真实冲击成本。','价差毛盈亏未扣开平两侧全部费用，不等同真实净收益。'])
                elif kind=='ORDER_STATUS' and e['status']==1:
                    i=next((x for x in self.engine.s['intents'].values() if x.get('order_id')==e['order_id']),None)
                    if i:self.notice('ack:'+e['order_id'],'委托已受理',['柜台已受理委托，尚不能视作成交。'],i['tag'],
                        fields=[('标的',PRODUCTS[i['csymbol']]['name']+' '+i['symbol']),('动作','开仓' if i['effect']==1 else '平仓'),
                            ('方向',DIRECTIONS[i['direction']]),('委托手数','1'),('限价',str(i['price'])),('状态','已受理／等待成交')],
                        reasons=reason_lines(i.get('trade_reason',{})) if i['effect']==1 else [EXITS.get(i.get('reason'),str(i.get('reason')))],
                        execution=['报价事件 '+str(i.get('decision_quote',{}).get('created_at','UNKNOWN')),'本机受理观测 '+str(i.get('ack_received_at','UNKNOWN'))])
                elif kind=='EXIT_DUE':
                    self.notice('due:'+e['symbol']+':'+e['recorded_at'],'退出待完成',[e['symbol'],'原因 '+e['reason'],'应减余量 '+str(e['quantity'])+' 手','持续核对成交与柜台持仓；未平风险仍保留。'])
                elif kind=='EXIT_FLAT_CONFIRMED':
                    self.notice('flat:'+e['symbol']+':'+e['recorded_at'],'柜台确认已平',[e['symbol'],'该合约方向余量为0，且无可再开仓活动委托。'])
                elif kind in ('RPC_FAILED','RECON_FAILED','UNOWNED_RESIDUAL_RISK_REQUIRES_RECON'):
                    reason=str(e.get('reason') or e.get('error') or kind)
                    self.notice('execution-risk:'+kind+':'+reason+':'+str(int(time.time()//600)),'执行或对账异常',[
                        kind,reason,'新开仓保持禁止或待核；已知持仓只做可确认减仓，未知敞口不按零处理。'])
                elif kind=='ORDER_STATUS' and e['status'] in (5,8,12):
                    self.notice('terminal:'+e['order_id']+':'+str(e['status']),'撤单或拒单回报',[
                        '状态码 '+str(e['status'])+'；累计成交 '+str(e['filled'])+' 手',
                        '拒绝原因 '+str(e.get('reject_detail') or e.get('reject_reason') or '无／不适用'),
                        '已成交部分继续管理；状态以柜台对账为准。'],tag)
            self.order_offset=f.tell()
        atomic(self.cursor_path,dict(order_offset=self.order_offset))

    def report(self,now):
        broker=None if self.engine is None else self.engine.summary();rows={};slots=[]
        for cs,p in self.products.items():
            m=p['metadata'];cal=p['calendar']
            ready=p['ready'] and p['coverage'].get('qualified') and not p['frozen']
            rows[cs]=dict(name=PRODUCTS[cs]['name'],symbol=None if m is None else m['symbol'],ready=bool(ready),frozen=p['frozen'],fault=p['fault'],
                warmup=p['coverage'],core=p['core'].progress,counts=dict(p['counts']),received_m1_minutes={d:len(v) for d,v in p['received'].items()},
                market_alive=p['market'].thread.is_alive(),core_alive=p['core'].thread.is_alive())
            if cal:slots.extend(elapsed_review_slots(cal,self.observation_started,now))
        slots=sorted(set(slots));ready_count=sum(x['ready'] for x in rows.values())
        if self.engine and len(slots)>=20:self.engine.s['pause_new']=True;self.engine.save()
        phase='READONLY_PREFLIGHT_PASS' if self.readonly and self.verified and ready_count==5 else 'READONLY_PREFLIGHT_PENDING' if self.readonly else (
            'PENDING_ACCOUNT_RECON' if not self.engine or not self.engine.ready else 'STOP_NEW_OPEN_MANAGING_POSITIONS' if self.engine.s['pause_new'] or self.engine.s['halt_reason'] else
            'NATURAL_SIGNAL_OBSERVATION' if ready_count==5 else 'PARTIAL_PRODUCT_READINESS')
        status=dict(at=str(now),phase=phase,universe=self.c['universe'],products=rows,broker=broker,ready_products=ready_count,config_sha256=self.c['config_sha256'],
            live_orders_enabled=False,quote_worker_alive=self.quote_worker.is_alive(),streaming_subscriptions=0,maximum_main_loop_gap_seconds=self.max_gap,
            elapsed_complete_trading_day_slots=slots,five_day_review_due=len(slots)>=5,twenty_day_review_due=len(slots)>=20,
            notifications=None if not self.notifications else self.notifications.health(),independent_score_run=False,review_mode='SINGLE_AGENT_SELF_CHECK')
        atomic(self.evidence/'status.json',status)
        text='# 五品种掘金模拟观察\n\n'+str(now)+'\n\n阶段：'+phase+'；就绪 '+str(ready_count)+'/5；完整交易日槽 '+str(len(slots))+'/20。缺数日不移出复核分母。\n\n'
        text+='|品种|实际合约|可评估新开|预热M15|缺M1|累计信号/拦截|\n|---|---|---|---:|---:|---|\n'
        for cs,r in rows.items():text+='|'+r['name']+'|'+str(r['symbol'])+'|'+str(r['ready'])+'|'+str(r['warmup'].get('expected_m15','UNKNOWN'))+'|'+str(r['warmup'].get('missing_m1','UNKNOWN'))+'|'+json.dumps(r['counts'],ensure_ascii=False)+'|\n'
        text+='\n'
        if broker:
            positive=[p for p in broker['positions'] if int(p['volume'])>0]
            text+='模拟净值 '+str(broker['nav'])+'；账户权益变化 '+str(broker['platform_equity_change'])+'（含联调与未平估值）；平台模拟扣费 '+str(broker['platform_simulated_deductions'])+'。费用未核验。\n\n'
            text+='正持仓 '+str(len(positive))+'；活动意图 '+str(broker['active_intents'])+'；对账 '+broker['status']+'；停开原因 '+str(broker['halt_reason'])+'。\n\n'
            text+='持仓：\n\n'+json.dumps(positive,ensure_ascii=False,indent=2)+'\n\n保护与截止：\n\n'+json.dumps(broker['position_plans'],ensure_ascii=False,indent=2)+'\n\n应平事件：\n\n'+json.dumps(broker['due_events'],ensure_ascii=False,indent=2)+'\n'
        text+='\n通知状态：'+json.dumps(status['notifications'],ensure_ascii=False)+'。HTTP独立进程；不以提醒成功替代下单、成交或平仓确认。\n'
        (self.evidence/'daily_report.md').write_text(text,encoding='utf8')
        if self.notifications and self.engine:
            if not self.start_notice and all(p['ready'] or p['frozen'] for p in self.products.values()):
                self.notice('startup:'+self.run_id,'五品种运行状态',[r['name']+' '+str(r['symbol'])+'：'+('预热就绪' if r['ready'] else '暂停新开 '+str(r['fault'])) for r in rows.values()]+[
                    '每笔最多1手，同品种不加仓，组合最多2仓；实盘关闭。','日盘15:05发送账户摘要；已知持仓持续监督。']);self.start_notice=True
            local=now.tz_convert('Asia/Shanghai')
            if (local.hour==15 and local.minute>=5) or 15<local.hour<20:
                day=self.calendar.day(now)
                pos=[p for p in broker['positions'] if int(p['volume'])>0]
                cash=self.engine.snapshot['cash'] if self.engine.snapshot else {}
                hold_lines=[]
                for position in pos:
                    direction='bull' if int(position['side'])==1 else 'bear';plan=broker['position_plans'].get(position['symbol']+'|'+direction,{})
                    hold_lines.append(position['symbol']+' '+DIRECTIONS[direction]+' '+str(position['volume'])+'手；入场 '+str(position['vwap'])+'；止损 '+str(plan.get('stop','UNKNOWN'))+'；目标 '+str(plan.get('target','UNKNOWN'))+'；截止 '+str(plan.get('deadline','UNKNOWN')))
                self.notice('daily:'+day,'日盘收盘账户摘要',['交易日 '+day,'快照时间 '+str(now),'模拟净值 '+str(broker['nav']),
                    '账户权益变化 '+str(broker['platform_equity_change'])+'（含联调／未平估值）','平台模拟扣费 '+str(broker['platform_simulated_deductions']),
                    '正持仓 '+str(len([p for p in broker['positions'] if int(p['volume'])>0]))+'；活动意图 '+str(broker['active_intents']),
                    '数据就绪 '+str(ready_count)+'/5；完整日槽 '+str(len(slots))+'/20',
                    'D+1及休市前清仓持续管理；模拟费用与真实费用分开。'],
                    fields=[('交易日',day),('账户环境','专用GM期货模拟账户'),('模拟净值',str(broker['nav'])),('可用资金',str(cash.get('available','UNKNOWN'))),
                        ('账户权益变化',str(broker['platform_equity_change'])+'（含联调和未平估值）'),('平台模拟扣费',str(broker['platform_simulated_deductions'])),
                        ('当前正持仓',str(len(pos))+'，组合上限2'),('活动委托意图',str(broker['active_intents']))],
                    reasons=['数据就绪 '+str(ready_count)+'/5；完整交易日槽 '+str(len(slots))+'/20',
                        '已平测试与策略分别记账，权益变化不冒充已核验真实净收益。']+(hold_lines or ['当前无正持仓。']),
                    execution=['快照 '+str(now),'对账 '+broker['status']+'；停开原因 '+str(broker['halt_reason'] or '无'),
                        '夜盘及已知敞口继续监督；缺数日保留在第5/20日复核分母。'])
        return status

    def run(self,max_seconds=0):
        started=time.monotonic();self.last_loop=started
        while True:
            now=pd.Timestamp.now(tz='Asia/Shanghai');mono=time.monotonic();self.max_gap=max(self.max_gap,mono-self.last_loop);self.last_loop=mono
            for cs,p in self.products.items():
                if not p['market'].thread.is_alive() or not p['core'].thread.is_alive():
                    if p['fault']!='WORKER_EXITED':self.notice('worker:'+self.run_id+':'+cs,'单品种进程异常',[PRODUCTS[cs]['name'],'已停止新开；已知持仓继续由账户执行器监督。'])
                    p.update(frozen=True,fault='WORKER_EXITED')
            if not self.quote_worker.is_alive() and self.engine:
                self.engine.s['pause_new']=True
                self.notice('quote-worker:'+self.run_id,'报价进程异常',['停止账户新开；若有余仓，保留风险并等待有效报价和受控恢复。'])
            try:self.market_events(now);self.core_events(now)
            except Exception as exc:
                self.event('INTERNAL_PRODUCT_HANDOFF_ERROR',category=type(exc).__name__,detail=str(exc)[:120])
                for p in self.products.values():p['frozen']=True
            for job,reply in self.transport.drain():
                if self.readonly:
                    if reply.get('ok') and job['op']=='snapshot':
                        s=reply['data'];self.verified=all(r.get('account_id')==self.c['account_id'] for r in [s['cash']]+s['positions']+s['orders']+s['active']+s['fills']);self.readonly_snapshot=s
                elif self.engine:self.engine.accept(job,reply,pd.Timestamp.now(tz='Asia/Shanghai'))
            if self.engine:
                control=Path(self.c['state_dir'])/'control.json'
                if control.exists():
                    c=json.loads(control.read_text(encoding='utf8'))
                    if c.get('stop_new_open'):self.engine.s['pause_new']=True
                    if c.get('flatten'):
                        for m in self.engine.s['meta'].values():m['force_exit']='USER_CONTROL_FLATTEN'
                try:self.engine.tick(pd.Timestamp.now(tz='Asia/Shanghai'))
                except Exception as exc:
                    self.engine.ready=False;self.engine.block='EXECUTION_FAULT';self.engine.s['pause_new']=True
                    self.event('EXECUTION_FAULT',category=type(exc).__name__)
                for cs,p in self.products.items():
                    symbol=None if p['metadata'] is None else p['metadata']['symbol']
                    positions=dict(Counter('bull' if int(r['side'])==1 else 'bear' for r in self.engine.positions() if int(r['volume'])>0 and r['symbol']==symbol))
                    if positions!=p['last_positions']:p['core'].inbox.put(('POSITIONS',positions));p['last_positions']=positions
                for i in self.engine.s['intents'].values():
                    if i['effect']==1 and i.get('filled') and i['tag']=='STRATEGY' and i['id'] not in self.fill_notified:
                        self.products[i['csymbol']]['core'].inbox.put(('FILL',dict(i)));self.fill_notified.add(i['id'])
                self.pump_notifications()
                problem=self.engine.s['halt_reason'] or ('PENDING_RECON' if self.transport.hung() else None)
                if problem and problem!=self.last_problem:self.notice('risk:'+self.run_id+':'+problem,'账户风险或执行异常',[problem,'暂停新开；已知敞口继续核对与减仓监督。'])
                self.last_problem=problem
            if self.calendar.contracts and not self.transport.busy() and mono-self.last_poll>=(1 if self.calendar.market_open(now) else 5):
                self.transport.request('snapshot');self.last_poll=mono
            if mono-self.last_report>=5:
                try:status=self.report(now)
                except Exception as exc:
                    if self.engine:self.engine.s['pause_new']=True
                    self.event('REPORT_FAULT',category=type(exc).__name__);status={}
                self.last_report=mono
                if self.readonly and status.get('phase')=='READONLY_PREFLIGHT_PASS':print(json.dumps(dict(phase=status['phase'],ready_products=5,status_file=str(self.evidence/'status.json'))),flush=True);return
            if max_seconds and mono-started>=max_seconds:return
            time.sleep(.2)


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--preflight',action='store_true');p.add_argument('--max-seconds',type=int,default=0)
    a=p.parse_args()
    if a.max_seconds and not a.preflight:p.error('timed shutdown is readonly only')
    runner=FiveRunner(a.config,a.preflight)
    print(json.dumps(dict(pid=os.getpid(),readonly=a.preflight,evidence_dir=str(runner.evidence))),flush=True);runner.run(a.max_seconds)
