from pathlib import Path
import base64,hashlib,hmac,json
from urllib.error import HTTPError,URLError
import pytest
from futures_trend_quant.live.paper_notify import message,signed_payload,valid_webhook,send_once,FeishuOutbox

HOOK='https://open.feishu.cn/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000'


@pytest.mark.parametrize('url',['http://open.feishu.cn/open-apis/bot/v2/hook/abcd','https://evil.example/hook/abc',
    HOOK+'?key=unsafe','https://open.feishu.cn@evil.example/hello',HOOK+'#fragment'])
def test_webhook_destination_is_strict_and_never_redirects(url):
    with pytest.raises(ValueError):valid_webhook(url)


def test_card_has_clear_type_two_columns_reasons_and_execution_boundary():
    p=message('入场已成交',['not used'],fields=[('标的','黄金 SHFE.au2612'),('成交','1手')],
        reasons=['M15方向已确认','H1为已闭合状态'],execution=['成交来源：GM模拟回报'])
    assert p['msg_type']=='interactive' and p['card']['header']['template']=='green'
    assert len(p['card']['elements'][0]['fields'])==2
    text=json.dumps(p,ensure_ascii=False)
    assert '交易理由' in text and '执行与数据依据' in text and '实盘关闭' in text


def test_private_hook_and_full_identifiers_are_removed_without_breaking_json():
    p=message('错误',['URL '+HOOK,'account 12345678-1234-1234-1234-123456789abc <at id=all>'])
    text=json.dumps(p,ensure_ascii=False)
    assert HOOK not in text and '12345678-1234' not in text and '<at' not in text
    json.loads(text)


def test_signature_preserves_interactive_card():
    card=message('检查',['test']);p=signed_payload(card,'secret',123)
    assert p['msg_type']=='interactive' and p['timestamp']=='123' and 'sign' in p
    expected=base64.b64encode(hmac.new(b'123\nsecret',b'',hashlib.sha256).digest()).decode()
    assert p['sign']==expected and 'timestamp' not in card


class Response:
    def __init__(self,body):self.body=body
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,*args):return self.body


class Opener:
    def __init__(self,response=None,error=None):self.response=response;self.error=error;self.requests=[]
    def open(self,request,timeout):
        self.requests.append((request,timeout))
        if self.error:raise self.error
        return self.response


def test_http_success_is_vendor_ack_and_not_a_trade_ack():
    o=Opener(Response(b'{"code":0}'));r=send_once(dict(webhook=HOOK),message('测试',['test']),o)
    assert r==dict(status='SENT',vendor_code=0)
    assert json.loads(o.requests[0][0].data)['msg_type']=='interactive'


@pytest.mark.parametrize('error,status',[(URLError('secret address'),'UNKNOWN_DELIVERY'),
    (HTTPError(HOOK,500,'unknown',{},None),'UNKNOWN_DELIVERY'),(HTTPError(HOOK,429,'rate',{},None),'RATE_LIMITED')])
def test_unknown_delivery_is_not_claimed_sent_or_blindly_replayed(error,status):
    r=send_once(dict(webhook=HOOK),'test',Opener(error=error))
    assert r['status']==status and HOOK not in json.dumps(r)


def test_outbox_deduplicates_and_does_not_replay_unconfigured_history(tmp_path):
    cfg=tmp_path/'private.json';box=FeishuOutbox(cfg,tmp_path,start=False)
    key=box.enqueue('same_event',message('入场候选',['test']))
    box.enqueue('same_event',message('different',['test']))
    assert len(list(box.root.glob('*.json')))==1
    row=json.loads((box.root/(key+'.json')).read_text())
    assert row['status']=='LOCAL_ONLY_NOT_CONFIGURED'
    cfg.write_text(json.dumps(dict(enabled=True,webhook=HOOK)))
    assert box.enqueue('same_event','ignored')==key
    assert json.loads((box.root/(key+'.json')).read_text())['status']=='LOCAL_ONLY_NOT_CONFIGURED'
