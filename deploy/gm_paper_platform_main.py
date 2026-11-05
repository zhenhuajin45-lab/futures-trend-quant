# coding=utf-8
"""Copy into a GM client strategy as main.py. Tokens stay in the local environment.

Client supplies its registered strategy_id via run arguments, or set GM_STRATEGY_ID.
This is a realtime SIM-only entry. Do not use the old backtest entry for paper trading.
"""
import os,sys
from pathlib import Path
PROJECT_ROOT=Path(os.environ.get('FUTURES_TREND_QUANT_ROOT',r'E:\掘金\期货-模拟五品种')).resolve()
if str(PROJECT_ROOT) not in sys.path:sys.path.insert(0,str(PROJECT_ROOT))
from gm.api import MODE_LIVE,run
from futures_trend_quant.live.gm_paper_host import init,on_timer,on_error,on_shutdown


if __name__=='__main__':
    # Official run() accepts the client's --strategy_id/--token arguments; init rechecks effective mode.
    run(strategy_id=os.environ.get('GM_STRATEGY_ID',''),filename=__file__.replace('\\','/'),
        mode=MODE_LIVE,token=os.environ.get('GM_TOKEN',''))
