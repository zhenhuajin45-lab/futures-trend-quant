"""Deterministic explanations from actual core facts; no generated trading rationale."""
PATHS={
    'A':'A：M15方向已确认；M5背驰、均线走平、形态三项中至少两项到齐。',
    'B':'B：M5回调与形态到齐，无M5反向背驰；H1反向强势否决按原规则检查。',
    'C':'C：同向空仓；M5横盘后，顺势突破或形态确认。',
    'D':'D：已闭合H1同向强势、M15斜率同向、无反向背驰、动能闸门通过，并有近期M5触发证据。',
}
EXITS={
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


def context(result):
    a=result.analysis;e=result.entry
    return dict(trend=result.trend.confirmed_state,slope=result.facts_row.get('slope_state'),
        h1_entry=a.finalized_h1_absolute,h1_closed_at=None if a.finalized_h1_eob is None else a.finalized_h1_eob.isoformat(),
        gate=None if result.gate is None else result.gate.reason,path=None if e is None else e.path,
        path_rule=PATHS.get(None if e is None else e.path,'尚无有效路径'),
        trigger_bar_id=None if e is None else e.trigger_bar_id,signal_eob=a.bar.eob.isoformat())


def lines(c):
    return [DIRECTIONS.get(c.get('trend'),str(c.get('trend')))+'方向授权；M15斜率 '+str(c.get('slope')),
        '已闭合H1 '+str(c.get('h1_entry'))+'，闭合时点 '+str(c.get('h1_closed_at') or 'UNKNOWN'),
        c.get('path_rule','路径未知'),'动能门控 '+str(c.get('gate') or '未提供')+'（具体门槛按所选路径适用）',
        '触发证据 '+str(c.get('trigger_bar_id') or 'UNKNOWN')]
