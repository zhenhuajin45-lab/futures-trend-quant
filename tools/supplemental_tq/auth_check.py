"""One SDK authentication probe. Prints only whitelisted status, never tokens."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import re

from .credentials import authentication


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        from tqsdk import TqAuth
    user,password=authentication()
    try:
        auth=TqAuth(user,password)
        auth.login()
        result=dict(authentication="PASS",tokens_displayed=False,market_session_tested=False)
    except Exception as exc:
        message=str(exc)
        match=re.search(r"认证失败\s*\((\d{3}),",message)
        known=[code for code in ("invalid_grant","unauthorized_client","Invalid user credentials",
            "Account is not fully set up","Account disabled","invalid_client") if code in message]
        result=dict(authentication="REJECTED_OR_UNAVAILABLE",http_status=int(match.group(1)) if match else None,
                    known_server_codes=known,error_type=type(exc).__name__,tokens_displayed=False,market_session_tested=False)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2),encoding="utf8")
    print(json.dumps(result))


if __name__=="__main__":
    main()
