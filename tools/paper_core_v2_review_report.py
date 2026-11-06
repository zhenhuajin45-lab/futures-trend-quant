"""Whitelist a fixed incident's derived facts. Originals and private state stay local."""
from pathlib import Path
import argparse,csv,json,hashlib


def export(source,output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans']
    plt.rcParams['axes.unicode_minus']=False
    r=json.loads((source/'gold_replay.local.json').read_text(encoding='utf8'))
    chart=json.loads((source/'chart_sources.local.json').read_text(encoding='utf8'))
    output.mkdir(parents=True,exist_ok=True);rows=[]
    for x in r['rows']:
        p=x['permission'];e=x['entry'] or {};h=x['h1_source']
        rows.append(dict(eob=x['eob'],close=x['close'],ema34=x['ema34'],atr14=x['atr14'],background=x['background'],
            proposed_action=x['proposal'],h1_absolute=x['h1'],h1_end=h['trend_eob'],h1_duration_minutes=h['duration_minutes'],
            h1_short_tail=h['short_session_tail'],top_div=x['top_div'],bottom_div=x['bottom_div'],
            new_entry_allowed=p['new_entry_allowed'],entry_permission_reasons='|'.join(p['reasons']),
            technical_exit_direction=p['technical_exit_direction'],reverse_authorized=p['reverse_authorized'],
            technical_candidate_path=e.get('path'),final_entry_accepted=e.get('accepted'),final_entry_reason=e.get('reason'),
            anchor_price=(x['anchor'] or {}).get('anchor_price'),anchor_bar_id=(x['anchor'] or {}).get('anchor_bar_id')))
    with (output/'gold_m15_comparison_v2.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    fig,axes=plt.subplots(4,1,figsize=(13,11),layout='constrained')
    for offset,freq in [(0,'M15'),(2,'SESSION_H1')]:
        bars=[x for x in chart if x['frequency']==freq];ix=range(len(bars));price,macd=axes[offset:offset+2]
        price.plot(ix,[x['close'] for x in bars],label='已闭合收盘',color='#202b39')
        price.plot(ix,[x['ema34'] for x in bars],label='EMA34',color='#d48a08')
        price.set_title('au2612 '+('M15' if offset==0 else '会话 H1：固定60分钟＋显式15/30分钟尾桶'))
        price.legend(loc='upper left');price.grid(alpha=.2)
        hist=[x['hist'] or 0 for x in bars]
        macd.bar(ix,hist,color=['#c64141' if y>=0 else '#168276' for y in hist],alpha=.55,label='HIST')
        macd.plot(ix,[x['dif'] for x in bars],label='DIF',color='#2367a5')
        macd.plot(ix,[x['dea'] for x in bars],label='DEA',color='#d48a08')
        macd.axhline(0,color='grey',linewidth=.6);macd.legend(loc='upper left',ncol=3);macd.grid(alpha=.2)
        ticks=list(range(0,len(bars),max(1,len(bars)//9)))
        for ax in (price,macd):
            ax.set_xticks(ticks,[bars[i]['eob'][5:16].replace('T',' ') for i in ticks],rotation=15)
    fig.suptitle('固定夜盘原件复放｜不含未闭合柱｜单一执行方自检，非盈利验证',fontsize=14)
    fig.savefig(output/'gold_m15_h1_v2.png',dpi=150);plt.close(fig)
    summary=dict(core_sha256_lf=r['core_sha256_lf'],fixed_incident_rows=r['night_rows'],
        natural_orders_sent=0,background_bull_rows=sum(x['background']=='bull' for x in rows),
        new_entry_allowed_rows=sum(x['new_entry_allowed'] for x in rows),
        first_technical_exit=next((x['eob'] for x in rows if x['technical_exit_direction']),None),
        old_veto_times=['2026-11-05T22:45:00+08:00','2026-11-06T00:00:00+08:00','2026-11-06T00:15:00+08:00'],
        live_recovery_passed=False,independent_profitability_test=False,
        raw_sources=[dict(logical_name='gold_initial_m1.parquet',sha256=r['source']['seed_sha256']),
                     dict(logical_name='gold_observed_m1.jsonl',prefix_bytes=r['source']['observed_prefix_bytes'],sha256=r['source']['observed_prefix_sha256'])],
        review='SINGLE_AGENT_SELF_CHECK',private_data_uploaded=False)
    (output/'replay_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(dict(derived_rows=len(rows),output=str(output)),ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();export(a.source,a.output)
