"""Read-only explanations from local closed bars, original rules and execution records."""
from pathlib import Path
from datetime import datetime,timedelta
import hashlib,json,math,sys


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def clock(t):return datetime.fromisoformat(str(t).replace('Z','+00:00'))
def stamp(t):return clock(t).strftime('%Y-%m-%d  %H:%M:%S')


class LocalBars:
    def __init__(self,root):self.root=Path(root);self.seeds={}
    def load(self,cs,symbol,now):
        from futures_trend_quant.data.gm_bars import bars_from_records,bar_from_mapping
        import pandas as pd
        root=self.root
        candidates=sorted((root/'data/cache/paper_market').glob(cs.replace('.','_')+'_*'),reverse=True)
        source=None
        for folder in candidates:
            manifest=read(folder/'input_manifest.json')
            if manifest['symbol']==symbol and clock(manifest['requested_at'])<=now:source=(folder,manifest);break
        if source is None:raise ValueError('NO_MATCHING_WARMUP')
        folder,manifest=source
        if folder not in self.seeds:
            path=folder/'initial_m1.parquet'
            if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest['sha256']:raise ValueError('WARMUP_HASH_MISMATCH')
            self.seeds[folder]=bars_from_records(pd.read_parquet(path).to_dict('records'))
        bars={b.identity:b for b in self.seeds[folder] if b.eob<=now}
        path=root/'evidence/paper_observation_five/products'/cs.replace('.','_')/'m1_observed.jsonl'
        if path.exists():
            lines=path.read_text(encoding='utf8').splitlines()
            for index,line in enumerate(lines):
                try:r=json.loads(line)
                except ValueError:
                    if index==len(lines)-1:break
                    raise ValueError('INVALID_M1_LOG')
                if r['symbol']!=symbol or clock(r['eob'])>now or clock(r['local_observed_at'])>now:continue
                b=bar_from_mapping(dict(r,frequency='60s'));old=bars.get(b.identity)
                if old and any(getattr(old,k)!=getattr(b,k) for k in ('open','high','low','close','volume')):raise ValueError('M1_REVISION')
                bars[b.identity]=b
        return sorted(bars.values(),key=lambda b:b.eob)[-24000:]


def entry_plan(root,row,now,quotes=None):
    """Describe the actual intent, or a labelled projection; never an order instruction."""
    from futures_trend_quant.core.risk import build_stop_plan
    root=Path(root);cs=row['csymbol'];entry=row['entry'];symbol=row['facts']['symbol'];direction=entry['direction']
    config=read(root/'config/paper_observation_five.local.json');rules=read(root/config['base_strategy'])['rules']
    registry=read(root/'state/paper_observation_five/market_registry.local.json')['contracts'][symbol]
    signal_at=clock(row['bar_eob']);deadline=signal_at+timedelta(seconds=config['signal_max_age_seconds'])
    windows=[(a,b) for a,b in registry['entry_windows'] if a<=signal_at.timestamp()<b]
    if windows:deadline=min(deadline,datetime.fromtimestamp(windows[0][1],signal_at.tzinfo))
    decision=row.get('paper_decision');allowed=bool(decision and decision[0]);block=None if allowed else str(decision[1] if decision else 'NO_EXECUTION_DECISION')
    result=dict(symbol=symbol,direction=direction,deadline=deadline.isoformat(),max_age_seconds=config['signal_max_age_seconds'],open_wait_seconds=config['open_wait_seconds'],
        remaining_seconds=max(0,(deadline-now).total_seconds()),eligible_window=bool(windows),allowed=allowed,block=block,basis='NO_PRICE_BASIS',entry=None,stop=None,target=None,risk_yuan=None)
    execution=read(root/'state/paper_observation_five/execution.json')
    evidence=cs+'|'+entry['evidence_id']
    intents=[i for i in execution.get('intents',{}).values() if i.get('evidence_id')==evidence and i.get('symbol')==symbol and i.get('effect')==1 and i.get('signal_at') and clock(i['signal_at'])==signal_at]
    if len(intents)==1:
        i=intents[0];price=float(i['price']);stop=float(i['stop']);target=float(i['target'])
        result.update(entry=price,stop=stop,target=target,risk_yuan=abs(price-stop)*float(i['multiplier']),basis='ACTUAL_INTENT',filled=int(i.get('filled',0)),order_status=i.get('status'))
        return result
    cp=read(root/('state/paper_observation_five/products/'+cs.replace('.','_')+'/core_checkpoint.json'))
    if cp['last_eob']!=row['bar_eob']:result['invalid']='ANCHOR_NOT_SAME_SIGNAL_BAR';return result
    anchor=cp['runtime']['anchors'].get(cs+'|'+direction)
    if not anchor:result['invalid']='ANCHOR_UNAVAILABLE';return result
    price=float(row['facts']['close']);basis='SIGNAL_CLOSE_PROJECTION'
    evaluated=clock(row.get('order_evaluated_at') or row['signal_ready_at'])
    for qrow in reversed(quotes or []):
        if qrow.get('symbol')!=symbol:continue
        q=qrow['quote'];received=clock(qrow['local_received_at']);created=clock(q['created_at'])
        if not (0<=(evaluated-received).total_seconds()<=config['quote_max_age_seconds'] and 0<=(evaluated-created).total_seconds()<=config['quote_max_age_seconds']):continue
        bid=float(q['bid']);ask=float(q['ask'])
        if not all(math.isfinite(float(q.get(k,0))) and float(q.get(k,0))>=1 for k in ('bid_size','ask_size')):continue
        if not 0<bid<=ask or ask-bid>registry['tick']*config['max_spread_ticks']+1e-9:continue
        price=ask if direction=='bull' else bid;basis='OBSERVED_OPPONENT_QUOTE_PROJECTION';result['reference_quote']=dict(bid=bid,ask=ask,created_at=created.isoformat(),received_at=received.isoformat());break
    result['entry']=price;result['basis']=basis
    try:
        p=build_stop_plan(direction=direction,entry=price,anchor=float(anchor['anchor_price']),atr=float(row['facts']['atr14']),tick=float(registry['tick']),buffer_atr=rules['stop_buffer_atr'],min_atr=rules['stop_min_atr'],max_atr=rules['stop_max_atr'])
        result.update(stop=p.stop,target=p.target,risk_yuan=p.distance*registry['multiplier'],anchor=p.anchor,anchor_bar_id=anchor.get('anchor_bar_id'),anchor_checkpoint_eob=cp['last_eob'],atr14=float(row['facts']['atr14']),tick=registry['tick'],multiplier=registry['multiplier'])
        if (direction=='bull' and p.anchor>=price) or (direction=='bear' and p.anchor<=price):result['anchor_warning']='ANCHOR_ON_OPPOSITE_SIDE_MIN_ATR_STOP_USED'
        status=read(root/'evidence/paper_observation_five/status.json');nav=(status.get('broker') or {}).get('nav')
        if config.get('risk_pct') and nav and 0<=(now-clock(status['at'])).total_seconds()<=20:
            result['current_risk_budget_yuan']=float(nav)*config['risk_pct'];result['within_current_one_lot_risk']=result['risk_yuan']<=result['current_risk_budget_yuan']
    except (ValueError,TypeError):result['invalid']='STRUCTURAL_STOP_INVALID'
    return result


def candidate_details(client,now):
    root=Path(client)/'_strategy_runtime';sys.path.insert(0,str(root))
    from futures_trend_quant.live.paper_notify import FeishuOutbox,message,atomic_json
    ev=root/'evidence/paper_observation_five';cursor=ev/'candidate_details_cursor.local.json'
    if not cursor.exists():atomic_json(cursor,dict(cutoff=now.isoformat(),processed=[]));return []
    state=read(cursor);done=set(state['processed']);sent=[];path=ev/'signals.jsonl'
    if not path.exists():return []
    pending=[]
    for line in path.read_text(encoding='utf8').splitlines():
        try:
            row=json.loads(line);entry=row.get('entry') or {};key='candidate-details:'+row['csymbol']+'|'+str(entry.get('evidence_id'))+'|'+row['bar_eob']
            if entry.get('accepted') and key not in done and clock(row['signal_ready_at'])>=clock(state['cutoff']) and clock(row['bar_eob'])<=now:pending.append(row)
        except (ValueError,KeyError,TypeError):continue
    if not pending:return []
    quotes=[]
    if (ev/'quotes.jsonl').exists():
        for line in (ev/'quotes.jsonl').read_text(encoding='utf8').splitlines():
            try:quotes.append(json.loads(line))
            except ValueError:continue
    for row in pending:
        try:
            entry=row.get('entry') or {}
            if not entry.get('accepted') or clock(row['signal_ready_at'])<clock(state['cutoff']) or clock(row['bar_eob'])>now:continue
            key='candidate-details:'+row['csymbol']+'|'+entry['evidence_id']+'|'+row['bar_eob']
            if key in done:continue
            p=entry_plan(root,row,now,quotes);fmt=lambda x:'UNKNOWN' if x is None else f'{float(x):.2f}'
            basis={'ACTUAL_INTENT':'实际委托计划','OBSERVED_OPPONENT_QUOTE_PROJECTION':'评估时已收到对手价，参考计算','SIGNAL_CLOSE_PROJECTION':'信号收盘价，参考计算，非可成交报价'}[p['basis']] if p['basis']!='NO_PRICE_BASIS' else '价位依据不足'
            status=('已报单；已成交'+str(p.get('filled',0))+'手' if p['basis']=='ACTUAL_INTENT' else '未报单 '+str(p['block']) if not p['allowed'] else '已允许，实际委托资料待核对')
            validity=('已过期；' if p['remaining_seconds']<=0 else '')+'本次已拦截，不自动补单' if not p['allowed'] else '信号评估窗口已结束；已生成委托以其当前状态为准' if p['remaining_seconds']<=0 else '生成时剩余 '+str(int(p['remaining_seconds']))+' 秒；仍须通过实时执行检查'
            fields=[('标的 / 方向',p['symbol']+' / '+('🟥 做多' if p['direction']=='bull' else '🟩 做空')),('执行状态',status),('参考入场价',fmt(p['entry'])),('止损价',fmt(p['stop'])),('2R目标价',fmt(p['target'])),('1手初始风险',fmt(p['risk_yuan'])+' 元'),('信号有效期',str(p['max_age_seconds'])+'秒（'+f"{p['max_age_seconds']/60:.1f}"+'分钟），截止 '+stamp(p['deadline'])),('当前有效性',validity),('计划手数','最多1手；同品种不加仓'),('计算口径',basis)]
            reasons=['入场路径 '+str(entry['path'])+'；'+str(entry['reason']),'价位与风险使用原策略 build_stop_plan、同根结构锚点、ATR及最小价位计算。']
            if p.get('invalid'):reasons.append('无法形成有效计划：'+p['invalid']+'；不展示虚构止损止盈。')
            if p.get('anchor_warning'):reasons.append('⚠️ 原核心保留的结构锚点位于入场价另一侧；原算子使用最小ATR止损距离。本卡不修改锚点或策略。')
            if 'current_risk_budget_yuan' in p:
                fields.append(('当前单笔风险预算',fmt(p['current_risk_budget_yuan'])+' 元（当前权益口径）'))
                if not p['within_current_one_lot_risk']:reasons.append('⚠️ 参考1手风险超过当前单笔预算；不具备直接开1手的风险余量。这是补充核算，不改写当时 BROKER_BUSY 等拦截原因。')
            out=FeishuOutbox(root/'config/paper_feishu.local.json',ev,start=False).enqueue(key,message('入场候选·价位与时效｜截至 '+stamp(row['bar_eob']),[],fields=fields,reasons=reasons,execution=['信号时效与挂单时效不同：未成交等待上限 '+str(p['open_wait_seconds'])+'秒，发出撤单不等于已经撤成。','被拦截候选仅展示参考计算；提醒不会触发重试或绕过风控。','实际成交后的保护止损、目标及截止以持仓和成交卡片为准。']))
            folder=ev/'candidate_details';folder.mkdir(exist_ok=True);atomic_json(folder/(out+'.json'),dict(at=now.isoformat(),source_signal_at=row['bar_eob'],plan=p,outbox_id=out,trade_api_called=False));sent.append(out);done.add(key)
        except (ValueError,KeyError,TypeError,OSError):continue
    if sent:state['processed']=sorted(done);atomic_json(cursor,state)
    return sent


class M5DivergenceMonitor:
    def __init__(self,client):
        self.root=Path(client)/'_strategy_runtime';self.feed=LocalBars(self.root);self.last_minute=None
    def poll(self,now):
        if self.last_minute==int(now.timestamp())//60:return []
        root=self.root;sys.path.insert(0,str(root))
        from futures_trend_quant.live.paper_notify import FeishuOutbox,message,atomic_json
        from futures_trend_quant.live.paper_data import complete_bars
        from futures_trend_quant.core.facts.indicators import calculate_indicators
        from futures_trend_quant.core.entry.evidence import _latest_divergence
        ev=root/'evidence/paper_observation_five';path=ev/'m5_divergence_cursor.local.json'
        if not path.exists():atomic_json(path,dict(cutoff=now.isoformat(),processed=[],last_checked={}))
        state=read(path);done=set(state['processed']);status=read(ev/'status.json');sent=[]
        config=read(root/'config/paper_observation_five.local.json');rules=read(root/config['base_strategy'])['rules']
        registry=read(root/'state/paper_observation_five/market_registry.local.json')['contracts']
        if not 0<=(now-clock(status['at'])).total_seconds()<=20:return []
        for cs,product in status['products'].items():
            if product.get('fault') or not product.get('ready'):continue
            target=int(now.timestamp())//300*300
            symbol=product['symbol'];windows=registry[symbol]['trade_windows']
            if not any(a<target<=b for a,b in windows):continue
            if state['last_checked'].get(cs,0)>=target:continue
            bars=self.feed.load(cs,symbol,now);m5=complete_bars(bars,5)[-1200:]
            if len(m5)<40 or m5[-1].eob.timestamp()<target:continue
            cp=read(root/('state/paper_observation_five/products/'+cs.replace('.','_')+'/core_checkpoint.json'))
            if clock(cp['last_eob'])>now:continue
            trend=cp['runtime']['trends'][cs]['trend_state']
            indicators=calculate_indicators(m5)
            for direction in ('bull','bear'):
                event=_latest_divergence(m5,indicators,direction,mode=rules['div_mode'])
                if event is None or clock(event.eob)<=clock(state['cutoff']):continue
                key='m5-divergence:'+cs+'|'+event.kind+'|'+event.bar_id
                if key in done:continue
                # Alert only a newly confirmed divergence opposing the current authorized trend.
                if (trend,direction) not in (('bull','bear'),('bear','bull')):continue
                kind='顶部背驰' if direction=='bear' else '底部背驰'
                fields=[('标的',product.get('name',cs)+' '+symbol),('M5确认','⚠️ '+kind),('当前M15方向','🟥 偏多' if trend=='bull' else '🟩 偏空'),('信号性质','小周期反向背驰预警，尚非M15反转确认')]
                out=FeishuOutbox(root/'config/paper_feishu.local.json',ev,start=False).enqueue(key,message('⚠️ M5反向背驰预警｜截至 '+stamp(event.eob),[],fields=fields,reasons=['复用运行策略的 M5 _latest_divergence，口径 '+str(rules['div_mode'])+'，仅使用已闭合完整K线。','背驰已满足原规则的枢轴与动能比较；同一确认事件只提醒一次，未识别到不推送。'],execution=['不改变M15正式趋势确认、入场时钟或开平仓规则。','M1仅作为闭合M5的底层数据；不改成M1背驰交易策略。']))
                folder=ev/'m5_divergence_alerts';folder.mkdir(exist_ok=True);atomic_json(folder/(out+'.json'),dict(at=now.isoformat(),csymbol=cs,symbol=symbol,event=dict(kind=event.kind,direction=event.direction,bar_id=event.bar_id,eob=event.eob.isoformat()),m5_last_eob=m5[-1].eob.isoformat(),trend=trend,outbox_id=out,trade_api_called=False));sent.append(out);done.add(key)
            state['last_checked'][cs]=target
        state['processed']=sorted(done);atomic_json(path,state);self.last_minute=int(now.timestamp())//60
        return sent
