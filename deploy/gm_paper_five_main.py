"""Authorized five-product GM simulation. Never a live route."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from futures_trend_quant.live.paper_five import main
if __name__=='__main__':main()
