"""Read-only startup check: never sends the authorized connectivity orders."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from futures_trend_quant.live.paper_runner import main

if __name__=='__main__':
    if '--preflight' not in sys.argv:sys.argv.append('--preflight')
    main()
