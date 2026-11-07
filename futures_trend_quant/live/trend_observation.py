"""Read-only semantics attached to existing closed-fact results and cards.

No permission is recomputed here. Historical validity, current warning scope and
actual veto are distinct, including UNKNOWN when no warning policy is registered.
"""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import hashlib
import math

VERSION='S0_TREND_OBSERVATION_V1'
UNKNOWN=None


def clock(value):
    t=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if t.tzinfo is None:raise ValueError('unknown timezone')
    return t


def event_id(event):
    # Same identity fields as core.facts_state.divergence_event_id, without import.
    return '|'.join(str(event[k]) for k in ('kind','mode','previous_bar_id','current_bar_id'))


def event_details(event,eob,background,permission,episode_start=None):
    if not event:return None
    confirmed=event['defined_at_bar_id'].rsplit('|',1)[-1]
    occurred=event['current_bar_id'].rsplit('|',1)[-1]
    if clock(confirmed)>clock(eob):raise ValueError('future divergence confirmation')
    opposite=(background=='bull' and event['kind']=='top') or (background=='bear' and event['kind']=='bottom')
    actual=bool(opposite and permission is not None and 'M15_OPPOSITE_DIVERGENCE_ACTIVE' in permission.get('reasons',[]))
    return dict(event_id=event_id(event),kind=event['kind'],extreme_price=event['extreme_price'],
                event_occurred_at=occurred,event_confirmed_at=confirmed,
                age_wall_minutes=(clock(eob)-clock(confirmed)).total_seconds()/60,
                confirmed_before_current_episode=None if not episode_start else clock(confirmed)<clock(episode_start),
                history_exists=True,structural_valid=True,warning_active=None,
                warning_reason='CURRENT_EPISODE_WARNING_POLICY_NOT_REGISTERED',entry_veto_active=actual,
                production_policy=None if permission is None else permission.get('policy'),
                structure_end_condition='HIGH_STRICTLY_ABOVE_EXTREME' if event['kind']=='top' else 'LOW_STRICTLY_BELOW_EXTREME',
                fixed_expiry_at=None,release_requires='STRUCTURE_INVALIDATED_OR_PERMISSION_CONTEXT_CHANGED;ALL_OTHER_GUARDS_STILL_REQUIRED')


def build(facts,*,background,episode_start=None,hist=None,previous_hist=None,first_received_at=None,computed_at=None,closed=None,complete=None):
    """Descriptive S0 decomposition, explicitly NOT the GoldTrend233 algorithm."""
    facts=deepcopy(facts)
    eob=facts['eob'];symbol=facts['symbol'];p=facts.get('trade_permission')
    side=(facts.get('trend_inputs') or {}).get('close_side')
    valid_hist=hist is not None and math.isfinite(float(hist))
    # No independently confirmed local structure exists in this version.
    # Histogram sign is reported as momentum, never as a newly confirmed trend.
    local=None
    if not valid_hist:momentum=None
    elif hist==0:momentum='ZERO'
    elif previous_hist is None:momentum='POSITIVE' if hist>0 else 'NEGATIVE'
    elif hist*previous_hist<0:momentum='CROSSED_UP' if hist>0 else 'CROSSED_DOWN'
    elif abs(hist)>abs(previous_hist):momentum='POSITIVE_EXPANDING' if hist>0 else 'NEGATIVE_EXPANDING'
    elif abs(hist)<abs(previous_hist):momentum='POSITIVE_CONTRACTING' if hist>0 else 'NEGATIVE_CONTRACTING'
    else:momentum='POSITIVE_FLAT' if hist>0 else 'NEGATIVE_FLAT'
    d=1 if background=='bull' else -1 if background=='bear' else 0
    decision=facts.get('trend_decision') or {}
    aligned=bool(d and side==('above' if d>0 else 'below') and facts.get('slope_state')==('up' if d>0 else 'down') and valid_hist and d*hist>0)
    weakening=bool((p or {}).get('weak_closed_bars',0)>0 or decision.get('proposed_action')=='veto')
    if closed is not True or complete is not True:phase='UNKNOWN'
    elif not d:phase='UNESTABLISHED'
    elif p and p.get('technical_exit_direction'):phase='TECHNICAL_EXIT_PENDING'
    elif weakening:phase='WEAKENING'
    elif not valid_hist or side not in ('above','below','equal') or facts.get('slope_state') not in ('up','down','flat'):phase='UNKNOWN'
    elif aligned:phase='ALIGNED'
    else:phase='MIXED'
    episode=None if not episode_start else symbol+'|900s|S0|'+episode_start
    events=[event_details(facts.get(k+'_divergence_event'),eob,background,p,episode_start) for k in ('top','bottom')]
    events=[x for x in events if x]
    for event in events:
        if event['event_id'].split('|')[2]!=symbol:raise ValueError('cross-contract divergence')
    h1=facts.get('h1_absolute');h1_direction='bull' if str(h1).startswith('bull') else 'bear' if str(h1).startswith('bear') else 'none' if h1 in ('flat','none') else None
    return dict(algorithm_version=VERSION,input_convention_version='SMA_SEEDED_EMA34_MACD_EMA9_SESSION_H1_MIXED_DURATION_V2',
                actual_symbol=symbol,frequency='900s',bar_eob=eob,bar_bob=None,source_bar_ids=[facts['bar_id']],
                first_received_at=first_received_at,computed_at=computed_at or datetime.now(timezone.utc).isoformat(),
                known_at_basis='CLOSED_FACTS;FIRST_RECEIVED_AT_UNKNOWN_UNLESS_DIRECTLY_RECORDED',
                is_closed=closed,is_complete=complete,data_quality='CLOSED_COMPLETE_CORE_INPUT' if closed is True and complete is True else 'QUALITY_UNCONFIRMED',
                background_direction=h1_direction,h1_structure_class=h1,production_m15_background=background,
                local_direction=local,local_direction_basis='UNKNOWN_NO_INDEPENDENT_LOCAL_STRUCTURE',
                momentum_direction=(1 if hist>0 else -1 if hist<0 else 0) if valid_hist else None,
                momentum_state=momentum,momentum_hist=hist,momentum_previous_hist=previous_hist,
                phase=phase,phase_definition_version='EXISTING_FACTS_MAPPING_V1',
                fact_alignment=aligned if closed is True and complete is True and valid_hist else None,
                observer_eligible=None,observer_eligible_reason='NO_INDEPENDENT_FOLLOW_POLICY_REGISTERED',
                event_type='EXISTING_CORE_FACTS',episode_id=episode,episode_started_at=episode_start,episode_known_at=episode_start,
                range_id=None,range_born_at=None,box_high=None,box_low=None,box_atr=None,
                event_occurred_at=None,event_confirmed_at=eob,event_ended_at=None,end_reason=None,
                divergence_history_valid=bool(events) if facts.get('top_div') is not None and facts.get('bottom_div') is not None else None,
                warning_active=None,entry_veto_active=any(x['entry_veto_active'] for x in events) if p is not None else None,
                divergence_events=events,production_permission=p,shadow_permission=None,
                entry_policy_version=facts.get('entry_policy_version'),selected_entry_permission=facts.get('entry_permission'),candidate_audit=facts.get('candidate_audit',[]),
                production_reason_codes=None if p is None else list(p.get('reasons',[])),
                shadow_reason_codes=['NO_PERMISSION_CANDIDATE_REGISTERED'],h1_source=facts.get('h1_source'),
                field_sources={'background_direction':['h1_absolute','h1_source'],
                  'production_m15_background':['trend_decision.confirmed_state','trend_decision.state.since_bar_id'],
                  'momentum_state':['analysis.indicators.hist[-1]','analysis.indicators.hist[-2]'],
                  'phase':['trade_permission.weak_closed_bars','trade_permission.technical_exit_direction','trend_decision.proposed_action','trend_inputs.close_side','slope_state','analysis.indicators.hist[-1]'],
                  'entry_veto_active':['trade_permission.reasons','top_divergence_event','bottom_divergence_event'],
                  'production_permission':['trade_permission:EXACT_COPY'],
                  'local_direction':['NO_REGISTERED_INDEPENDENT_LOCAL_STRUCTURE']})


def from_result(result):
    a=result.analysis
    observation=build(result.facts_row,background=result.trend.confirmed_state,
                 episode_start=None if not result.trend.state.since_bar_id else result.trend.state.since_bar_id.rsplit('|',1)[-1],
                 hist=a.indicators.hist[-1],previous_hist=a.indicators.hist[-2] if len(a.indicators.hist)>1 else None,
                 closed=True,complete=True)
    observation['bar_bob']=a.bar.bob.isoformat()
    return observation


def safe_from_result(result):
    # A reporting failure must not abort account/risk supervision.
    try:return from_result(result)
    except Exception as exc:
        return dict(algorithm_version=VERSION,data_quality='OBSERVATION_ERROR',error_type=type(exc).__name__,
                    background_direction=None,local_direction=None,momentum_state=None,phase='UNKNOWN',observer_eligible=None,
                    production_permission=None,shadow_permission=None,divergence_events=[])


LABELS={1:'🟥 偏多',-1:'🟩 偏空',0:'⬜ 零轴',None:'UNKNOWN（资料不足）'}
PHASE_LABELS={'UNKNOWN':'状态待定（资料未齐）','UNESTABLISHED':'背景未建立','WEAKENING':'短线转弱',
              'MIXED':'状态待定（现有证据冲突）','ALIGNED':'现有方向证据同向',
              'TECHNICAL_EXIT_PENDING':'原方向技术退出条件满足（有仓须核对减仓）'}
MOMENTUM_LABELS={'POSITIVE':'柱为正','NEGATIVE':'柱为负','ZERO':'零轴',
 'CROSSED_UP':'刚转正','CROSSED_DOWN':'刚转负','POSITIVE_EXPANDING':'正柱增强','NEGATIVE_EXPANDING':'负柱增强',
 'POSITIVE_CONTRACTING':'正柱缩短','NEGATIVE_CONTRACTING':'负柱缩短','POSITIVE_FLAT':'正柱持平','NEGATIVE_FLAT':'负柱持平'}


def _lines(observation):
    if not observation:return []
    o=observation;p=o.get('production_permission')
    out=['已确认局部方向：UNKNOWN（未另立结构算法）；阶段 '+PHASE_LABELS.get(o.get('phase'),'未知')+'；短动能 '+LABELS.get(o.get('momentum_direction'),'UNKNOWN')+'，MACD '+MOMENTUM_LABELS.get(o.get('momentum_state'),'UNKNOWN'),
         'H1仅为背景结构 '+str(o.get('h1_structure_class') or 'UNKNOWN')+'；强势分类不等于当前动能强或允许追单。']
    if p is not None:
        if o.get('entry_policy_version')=='PAPER_ENTRY_V5':
            selected=o.get('selected_entry_permission')
            out.append('V5背景技术门控：'+('同向' if p.get('new_entry_allowed') else ','.join(p.get('reasons',[])))+'；入场按路径分别核验，B柱符号仅提示。')
            out.append('本M15路径选择：'+('已选合格路径，仍须报价、账户及原生检查' if selected and selected.get('new_entry_allowed') else '无合格路径，不代表行情没有方向。'))
            out.extend('路径 '+x['path']+'：'+('资格通过' if x['eligible'] else '／'.join(x['reasons'])) for x in o.get('candidate_audit',[]))
        else:out.append('生产新开：'+('可继续执行检查（尚未检查账户、报价和清仓期限）' if p.get('new_entry_allowed') else '关闭；'+','.join(p.get('reasons',[]))))
    for event in o.get('divergence_events',[]):
        kind='顶背驰' if event['kind']=='top' else '底背驰'
        digest=hashlib.sha256(event['event_id'].encode()).hexdigest()[:10]
        confirmed=clock(event['event_confirmed_at']).astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
        out.append(f"{kind} #{digest}：结构仍有效，极值 {event['extreme_price']:.2f}，确认柱 {confirmed}；距本根 {event['age_wall_minutes']:.0f} 自然分钟；"+
                   ('正在否决当前方向新开。' if event['entry_veto_active'] else '尚无授权方向，不计作可入场机会。' if o.get('production_m15_background') not in ('bull','bear') else '未否决当前方向新开。')+
                   '当前阶段预警适用性未登记，不能由历史有效性推定。')
        out.append(('该顶背驰结构终止：后续最高价严格突破 ' if event['kind']=='top' else '该底背驰结构终止：后续最低价严格跌破 ')+
                   f"{event['extreme_price']:.2f}；无固定到期分钟。结构失效不保证其余开仓条件通过。")
    return out


def lines(observation):
    try:return _lines(observation)
    except Exception:return ['状态说明资料异常：UNKNOWN；不改变生产权限及风险退出。']
