"""Local SHADOW card, no sender and no permission implication."""


def card(snapshot):
    s=snapshot;room=s['room'];candidate=s.get('original_plan') or {}
    rr='UNKNOWN' if room.get('rr_space_net') is None else format(room['rr_space_net'],'.3f')
    fields=[('模式','SHADOW；不参与实际开平仓'),('合约 / 时点',s['symbol']+' / '+s['asof_at']),
        ('M15 / M5结构',s['structure']['structure_state']+' / '+s['m5_structure']['structure_state']),
        ('区间位置',s['location']['location_class']),('第一障碍前净空间R',rr+'；'+room['status']),
        ('原候选 / 路径',str(s.get('candidate_evidence_id') or '无自然合格候选')+' / '+str(candidate.get('path') or 'NOT_EVALUATED')),
        ('原执行结果',str(candidate.get('original_decision') or 'NOT_EVALUATED')),
        ('数据完整性',s['completeness']+'；known_at='+str(s['known_at'] or 'UNKNOWN'))]
    return dict(schema='PA_LOCAL_SHADOW_CARD_V1',event_id=s['event_id'],deliver=False,trade_api_called=False,
        card=dict(header=dict(template='grey',title=dict(tag='plain_text',content='价格行为影子观察｜'+s['symbol'])),
            elements=[dict(tag='div',fields=[dict(is_short=False,text=dict(tag='lark_md',content='**'+k+'**\n'+v)) for k,v in fields]),
                      dict(tag='note',elements=[dict(tag='plain_text',content='无成交模型，不报告收益；不续期、不补单，不解除账户保护。')])]))
