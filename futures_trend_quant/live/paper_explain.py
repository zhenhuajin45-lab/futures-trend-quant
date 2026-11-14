"""Deterministic explanations from actual core facts; no generated trading rationale."""
PATHS={
    'A':'A：M15方向已确认；M5背驰、均线走平、形态三项中至少两项到齐。',
    'B':'B：M5回调与形态到齐，无M5反向背驰；H1反向强势否决按原规则检查。',
    'C':'C：同向空仓；M5横盘后，顺势突破或形态确认。',
    'D':'D：已闭合H1同向强势、M15斜率同向、无反向背驰、动能闸门通过，并有近期M5触发证据。',
}
EXITS={
    'TECHNICAL_WEAKNESS_EXIT_V1':'连续两根已闭合M15收盘在均线反侧且MACD反向，原方向退出；不等于授权反手。',
    'TREND_EXIT':'趋势状态确认切换，原方向按规则退出。',
    'STOP_TICK_TRIGGER':'最新有效快照触及保护止损；成交价取平台回报。',
    'TARGET_TICK_TRIGGER':'最新有效快照触及2R目标；成交价取平台回报。',
    'STOP_M1_TRIGGER':'已闭合M1高低价触及止损；不回填历史止损成交价。',
    'TARGET_M1_TRIGGER':'已闭合M1高低价触及目标；实际成交仍须柜台确认。',
    'NEXT_TRADING_DAY_DEADLINE':'到达开仓后下一交易日日盘收盘前5分钟清仓时点。',
    'PRE_LONG_CLOSURE_EXIT':'周末或交易所长休市前进入清仓窗口。',
    'USER_CONTROL_FLATTEN':'执行本机人工停开／清仓指令。',
    'CONNECTIVITY_TEST_CLOSE':'联调测试平仓，不计策略成绩。',
}
DIRECTIONS={'bull':'做多','bear':'做空','none':'无方向'}


def h1_clock_context(source):
    """Core known_at is a logical bucket end, not a received-at proof.

Keep the original core object untouched. No complete constituent receipt map
is supplied here, so actual availability must remain UNKNOWN.
"""
    if not source:return source
    return dict(source,logical_closed_at=source.get('known_at'),known_at=None,
                availability='UNKNOWN_SOURCE_RECEIPTS',
                known_at_basis='LOGICAL_BUCKET_END_IS_NOT_RECEIPT')


def closing_gross(intent,fill):
    """Price-difference attribution only; never label it verified net profit."""
    if intent['effect']==1:return []
    sign=1 if intent['direction']=='bull' else -1
    distance=abs(float(intent['entry_price'])-float(intent['initial_stop']))
    move=sign*(float(fill['price'])-float(intent['entry_price']))
    gross=move*float(intent['multiplier'])*int(fill['volume'])
    return [('本次价差毛盈亏',f'{gross:+.2f} 元'),('本次毛R',f'{move/distance:+.4f}R' if distance>0 else 'UNKNOWN')]


def context(result,observation=None):
    a=result.analysis;e=result.entry
    return dict(trend=result.trend.confirmed_state,slope=result.facts_row.get('slope_state'),
        h1_entry=a.finalized_h1_absolute,h1_closed_at=None if a.finalized_h1_eob is None else a.finalized_h1_eob.isoformat(),
        gate=None if result.gate is None else result.gate.reason,path=None if e is None else e.path,
        background_gate=None if result.gate is None else result.gate.reason,
        gate_scope='BACKGROUND_MOMENTUM;SEE_PATH_ENTRY_PERMISSION',
        path_rule=('B：同一次回调开始后由后续闭合M5形态确认；M15柱符号仅提示，H1及反向背驰限制保留。' if e and e.path=='B' and result.facts_row.get('entry_policy_version')=='PAPER_ENTRY_V5' else PATHS.get(None if e is None else e.path,'尚无有效路径')),
        trigger_bar_id=None if e is None else e.trigger_bar_id,signal_eob=a.bar.eob.isoformat(),
        permission=result.facts_row.get('entry_permission') or result.facts_row.get('trade_permission'),background_permission=result.facts_row.get('trade_permission'),h1_source=h1_clock_context(result.facts_row.get('h1_source')),
        entry_policy_proof=result.facts_row.get('entry_policy_proof'),candidate_audit=result.facts_row.get('candidate_audit',[]),
        trend_observation=observation)


def lines(c):
    from .trend_observation import lines as observation_lines
    return [DIRECTIONS.get(c.get('trend'),str(c.get('trend')))+'趋势背景；M15斜率 '+str(c.get('slope')),
        '已闭合H1 '+str(c.get('h1_entry'))+'，闭合时点 '+str(c.get('h1_closed_at') or 'UNKNOWN'),
        c.get('path_rule','路径未知'),'背景动能提示 '+str(c.get('background_gate',c.get('gate')) or '未提供')+('；B路径M15柱符号仅提示，不作为入场硬否决。' if c.get('path')=='B' else '；入场是否通过见所选路径许可。'),
        '触发证据 '+str(c.get('trigger_bar_id') or 'UNKNOWN'),
        '路径入场许可 '+str((c.get('permission') or {}).get('new_entry_allowed','UNKNOWN'))+'；原因 '+str((c.get('permission') or {}).get('reasons',[])),
        '技术退出方向 '+str((c.get('permission') or {}).get('technical_exit_direction') or '无')+'；反向开仓仍需独立授权。']+observation_lines(c.get('trend_observation'))
