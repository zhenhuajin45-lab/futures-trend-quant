"""Official quickstart-shaped single quote; no loop or trading instructions."""
import argparse

from tools.supplemental_tq.credentials import authentication
from tools.supplemental_tq.collect import actual_symbols


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--symbol",default="SHFE.au2612")
    args=parser.parse_args()
    symbol=actual_symbols([args.symbol])[0]
    from tqsdk import TqApi,TqAuth
    # Authentication is local environment or Windows-user-bound encrypted file.
    # No real credentials are present in this example.
    api=TqApi(auth=TqAuth(*authentication()),debug=False)
    try:
        quote=api.get_quote(symbol)
        print(quote)
    finally:
        api.close()


if __name__=="__main__":
    main()
