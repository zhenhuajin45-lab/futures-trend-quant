"""Frozen, read-only card plans. These functions never authorize or send orders."""
from datetime import datetime,timedelta,timezone
import math
from .paper_notify import message
from .paper_explain import PATHS

POLICY='ACTIONABLE_CARDS_V4_20261106'
TZ=timezone(timedelta(hours=8))


def clock(value):
    t=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if t.tzinfo is None:raise ValueError('notification clock timezone unknown')
    return t


def display(value):return clock(value).astimezone(TZ).strftime('%Y-%m-%d  %H:%M:%S')
def time_text(value):
    try:return display(value)+' 北京时间'
    except (ValueError,TypeError):return '未取得时间'
def number(value):
    try:return f'{float(value):.2f}' if math.isfinite(float(value)) else '未取得'
    except (ValueError,TypeError):return '未取得'


REASONS={
 'CANDIDATE_WAITING':'等待实时风控与执行通道检查，尚未下单',
 'BROKER_BUSY':'本地执行通道忙；仅原有效期内等待',
 'PENDING_RECON':'账户尚待对账，不能新开',
 'STOP_NEW_OPEN':'当前已停开，不会下单',
 'PRODUCT_NOT_READY_OR_READONLY':'品种尚未就绪或仅观察，不能下单',
 'PRODUCT_POSITION_OR_OPEN_INTENT':'已有该品种持仓或开仓委托，不加仓',
 'PORTFOLIO_LIMIT':'组合仓位已达到上限',
 'ONE_LOT_RISK_LIMIT':'即使只开1手，也超过单笔风险预算',
 'MARGIN_LIMIT':'可用资金或保证金占用超限',
 'STRUCTURAL_STOP_INVALID':'结构止损不符合原规则，未形成有效计划',
 'INVALID_ANCHOR':'结构锚点身份、确认时间或有效性不合格',
 'SHARED_PERMISSION_CHANGED':'共享开仓权限已改变，原候选终止',
 'PRE_LONG_CLOSURE_NO_REOPEN':'周末或长休市清仓窗口，禁止重开',
 'DAILY_CLOSE_WINDOW_NO_ENTRY':'已进入日盘收盘清仓窗口，禁止新开',
 'DAILY_LOSS_LIMIT':'触及日亏损熔断，停止新开并监督撤单',
 'OBSERVATION_DRAWDOWN_LIMIT':'触及观察回撤熔断，停止新开并监督撤单',
 'CONSECUTIVE_LOSS_COOLDOWN':'连续亏损冷却中，不能新开',
 'CONSECUTIVE_LOSS_NEXT_TRADING_DAY':'连亏限制当日新开，下一交易日再按原规则核验',
 'ORIGINAL_DEADLINE':'原30秒有效期已结束，不补单、不续期',
 'OUTSIDE_CONTINUOUS_SESSION':'当前不在该合约连续交易小节',
 'NO_DIRECTION_AUTHORIZATION':'方向背景尚未确认',
 'M15_CLOSE_ADVERSE':'M15收盘已在原方向均线反侧',
 'M15_SLOPE_NOT_ALIGNED':'M15均线斜率不再支持原方向',
 'M15_MACD_NOT_ALIGNED':'M15动能不再支持原方向',
 'M15_OPPOSITE_DIVERGENCE_ACTIVE':'仍有有效的反向M15背驰',
 'CLOSED_H1_UNKNOWN':'尚无合格的已闭合H1',
 'H1_CALENDAR_UNQUALIFIED':'H1历史交易时段依据未通过',
}


def reason_text(reason):
    value=str(reason or 'CANDIDATE_WAITING')
    if value.startswith('CANDIDATE_EXPIRED') or value.startswith('SIGNAL_EXPIRED'):return REASONS['ORIGINAL_DEADLINE']
    return REASONS.get(value,value)


def freeze_plan(signal,close,quote,quote_reason,now,config,nav=None):
    """Capture the exact signal's structure/spec/known quote, never a later CP."""
    from futures_trend_quant.core.risk import build_stop_plan,_round_down,_round_up
    from .paper_anchor_guard import validate_anchor_proof
    signal=dict(signal or {});q=dict(quote or {})
    first=signal.get('first_eligible_at',signal.get('signal_at'))
    expiry=signal.get('expires_at') or (clock(first)+timedelta(seconds=config['signal_max_age_seconds'])).isoformat()
    direction=signal.get('direction');price=float(q['ask'] if direction=='bull' else q['bid']) if q else float(close)
    result=dict(schema='FROZEN_CANDIDATE_PLAN_V4',symbol=signal.get('symbol'),direction=direction,path=signal.get('path'),
        first_eligible_at=first,expires_at=expiry,evaluated_at=str(now),entry=price,stop=None,target=None,risk_yuan=None,
        basis='OBSERVED_OPPONENT_QUOTE' if q else 'SIGNAL_CLOSE_REFERENCE_ONLY',quote_reason=quote_reason,
        reference_quote=q or None,anchor=signal.get('anchor'),anchor_bar_id=signal.get('anchor_bar_id'),
        anchor_confirmed_at=signal.get('anchor_confirmed_at'),anchor_episode_id=signal.get('anchor_episode_id'),
        atr=signal.get('atr'),tick=signal.get('tick') or q.get('tick'),multiplier=signal.get('multiplier'),
        structural_valid=False,quote_valid=bool(q),open_wait_seconds=config['open_wait_seconds'],
        max_age_seconds=config['signal_max_age_seconds'],trade_reason=signal.get('trade_reason',{}))
    if signal.get('anchor') is None:
        result.update(invalid='ANCHOR_UNAVAILABLE',invalid_text='原信号没有可用的已确认结构锚点')
        return result
    try:
        validate_anchor_proof(signal,signal['symbol'],1 if direction=='bull' else 2,price)
        r=signal['rules'];atr=float(signal['atr']);tick=float(result['tick']);anchor=float(signal['anchor'])
        result.update(stop_min_distance=r['stop_min_atr']*atr,stop_max_distance=r['stop_max_atr']*atr,
                      stop_buffer_atr=r['stop_buffer_atr'])
        structural=_round_down(anchor-r['stop_buffer_atr']*atr,tick) if direction=='bull' else _round_up(anchor+r['stop_buffer_atr']*atr,tick)
        result.update(structural_stop=structural,structural_distance=abs(price-structural))
        p=build_stop_plan(direction=direction,entry=price,anchor=anchor,atr=atr,tick=tick,
                          buffer_atr=r['stop_buffer_atr'],min_atr=r['stop_min_atr'],max_atr=r['stop_max_atr'])
        result.update(stop=p.stop,target=p.target,risk_yuan=p.distance*float(signal['multiplier']),
                      risk_distance=p.distance,reward_distance=abs(p.target-price),structural_valid=True)
        if nav is not None:
            result['risk_budget_yuan']=float(nav)*config['risk_pct']
            result['within_risk_budget']=result['risk_yuan']<=result['risk_budget_yuan']
    except (ValueError,TypeError,KeyError) as exc:
        result.update(invalid='INVALID_ANCHOR' if 'anchor' in str(exc).lower() else 'STRUCTURAL_STOP_INVALID',invalid_detail=str(exc))
        result['invalid_text']=('结构锚点不合格，不能用ATR下限替代失效结构' if result['invalid']=='INVALID_ANCHOR' else
                                '结构止损距离超过原规则上限' if 'maximum ATR' in str(exc) else '原信号的结构或合约资料不合格')
    return result


def candidate_card(plan,decision,now,name='',stage='CANDIDATE',tag='STRATEGY'):
    p=plan;remaining=max(0,(clock(p['expires_at'])-clock(now)).total_seconds())
    valid=bool(p.get('structural_valid'));quotes=bool(p.get('quote_valid'))
    code=None if decision and decision[0] else str(decision[1] if decision else 'CANDIDATE_WAITING')
    invalid=not valid or p.get('within_risk_budget') is False
    blocked=bool(code and code not in ('CANDIDATE_WAITING','BROKER_BUSY','PENDING_RECON'))
    title=('⛔ 信号拦截' if invalid or blocked else '⚪ 信号观察·待报价' if not quotes else '🟦 入场候选')
    if stage=='ORDER_INTENT':title='🟦 入场委托·待受理'
    status='已保存本地委托意图；尚未确认柜台受理' if stage=='ORDER_INTENT' else reason_text(code)
    if remaining<=0 and stage!='ORDER_INTENT':status=REASONS['ORIGINAL_DEADLINE']
    direction='🟥 做多' if p.get('direction')=='bull' else '🟩 做空'
    fields=[('标的 / 方向',(name+' '+str(p.get('symbol'))).strip()+' / '+direction),('执行状态',status),
            ('计划限价' if stage=='ORDER_INTENT' else '参考入场价',number(p.get('entry'))),
            ('止损价',number(p.get('stop')) if valid else '未生成：结构校验未通过'),
            ('2R目标价',number(p.get('target')) if valid else '未生成：结构校验未通过')]
    if valid:
        fields += [('1手初始风险',number(p.get('risk_yuan'))+' 元；距离 '+number(p.get('risk_distance'))+' 点'),
                   ('目标空间',number(p.get('reward_distance'))+' 点；2R目标按tick取整')]
    else:
        fields += [('拦截原因',p.get('invalid_text','原信号资料不足，不能形成有效结构计划')),
                   ('结构锚点 / ATR',number(p.get('anchor'))+' / '+number(p.get('atr')))]
        if p.get('structural_distance') is not None:
            fields.append(('结构距离 / 允许上限',number(p['structural_distance'])+' / '+number(p.get('stop_max_distance'))+' 点'))
    if p.get('risk_budget_yuan') is not None:fields.append(('单笔风险预算',number(p['risk_budget_yuan'])+' 元'+('；1手超预算，禁止开仓' if p.get('within_risk_budget') is False else '')))
    fields += [('信号有效期','原 '+str(p['max_age_seconds'])+'秒（'+f"{p['max_age_seconds']/60:.1f}"+'分钟）；生成时剩余 '+str(int(remaining))+'秒'),
               ('本信号截止',clock(p['expires_at']).astimezone(TZ).strftime('%H:%M:%S')+' 北京时间；不续期'),
               ('计划手数','最多1手；同品种不加仓'),('价位依据','本地实际委托计划，受理与成交另行确认' if stage=='ORDER_INTENT' else '评估时已收到的对手价，参考计算，委托时须重新核验' if quotes else '信号收盘价，仅参考计算，尚无合格可成交报价')]
    c=p.get('trade_reason') or {};path=p.get('path')
    reasons=[PATHS.get(path,'原路径 '+str(path)+'，详细条件见原信号记录')]
    if c:
        reasons += ['M15方向 '+str(c.get('trend'))+'；斜率 '+str(c.get('slope'))+'；已闭合H1 '+str(c.get('h1_entry'))]
        if path=='D':reasons.append('D路径动能门控：'+str(c.get('gate')))
        permission=c.get('permission') or {}
        if permission:reasons.append('共享新开权限：'+('允许继续执行检查' if permission.get('new_entry_allowed') else '禁止新开；'+'；'.join(reason_text(x) for x in permission.get('reasons',[]))))
    if invalid:reasons.append('⛔ 本消息不是有效开仓计划；不会放宽止损、风险预算或补发历史信号。')
    else:reasons.append('参考价位与实际委托、实际成交分别记录；提示不代替实时风控通过。')
    if not quotes:reasons.append('报价检查：'+str(p.get('quote_reason') or '未取得合格报价'))
    if p.get('invalid_detail'):reasons.append('校验：'+str(p['invalid_detail']))
    card=message(title+('｜'+name if name else '')+'｜截至 '+display(p['first_eligible_at']),[],tag,
        fields=fields,reasons=reasons,execution=['未成交挂单等待上限 '+str(p['open_wait_seconds'])+'秒；发出撤单不代表已经撤成。',
        '结构与ATR冻结在本信号；不读取下一根checkpoint补算。','成交后按成交均价更新2R目标，实际保护与截止由成交卡片确认。'])
    card['card']['header']['template']='grey' if tag=='CONNECTIVITY_TEST' else 'orange' if invalid or blocked else 'grey' if not quotes else 'blue'
    return card


def intent_plan(intent,config):
    q=intent.get('decision_quote') or {}
    return dict(schema='FROZEN_CANDIDATE_PLAN_V4',symbol=intent['symbol'],direction=intent['direction'],path=intent.get('path'),
        first_eligible_at=intent.get('first_eligible_at',intent.get('signal_at',intent['submitted_at'])),
        expires_at=intent.get('expires_at') or (clock(intent['submitted_at'])+timedelta(seconds=30)).isoformat(),
        entry=intent['price'],stop=intent.get('stop'),target=intent.get('target'),
        risk_distance=abs(intent['price']-intent['stop']),reward_distance=abs(intent['target']-intent['price']),
        risk_yuan=abs(intent['price']-intent['stop'])*intent['multiplier'],structural_valid=True,quote_valid=True,
        max_age_seconds=config['signal_max_age_seconds'],open_wait_seconds=config['open_wait_seconds'],trade_reason=intent.get('trade_reason',{}))


def fill_protection(intent,meta,fill):
    """Never borrow protection from a later position in the same symbol."""
    if intent['effect']==1:
        saved=intent.get('filled_protection')
        if saved:return dict(saved)
        if meta.get('open_intent_id')==intent['id']:return dict(meta)
        stop=intent.get('stop');price=float(fill['price']);sign=1 if intent['direction']=='bull' else -1
        return dict(stop=stop,target=None if stop is None else price+sign*2*abs(price-stop),deadline=intent.get('deadline','未取得'))
    return dict(stop=intent.get('initial_stop'),target=intent.get('initial_target'),deadline=intent.get('deadline','未取得'))
