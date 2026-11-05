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
from futures_trend_quant.live.gm_paper_host import init,on_timer,on_error,on_shutdown,launch_parameters


if __name__=='__main__':
    strategy_id,token=launch_parameters(sys.argv[1:],PROJECT_ROOT,Path(__file__).with_name('paper_platform.local.json'))
    os.environ['GM_TOKEN']=token  # Propagate the same credential to isolated GM data/SIM workers.
    # Official run() accepts the client's standard arguments; init rechecks the effective mode.
    run(strategy_id=strategy_id,filename=__file__.replace('\\','/'),mode=MODE_LIVE,token=token)
