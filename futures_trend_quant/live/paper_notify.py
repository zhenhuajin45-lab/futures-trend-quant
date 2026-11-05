"""Private Feishu outbox. HTTP lives in a separate process; alerts never send orders."""
from pathlib import Path
from datetime import datetime,timezone
import base64,hashlib,hmac,json,multiprocessing,os,re,tempfile,time
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError
from urllib.parse import urlparse


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:json.dump(value,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
        for attempt in range(5):
            try:os.replace(tmp,path);break
            except PermissionError as exc:
                if getattr(exc,'winerror',None) not in (5,32,33) or attempt==4:raise
                time.sleep(.01*(attempt+1))
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def valid_webhook(url):
    p=urlparse(url)
    if p.scheme!='https' or p.hostname not in ('open.feishu.cn','open.larksuite.com') or p.username or p.password or p.port not in (None,443) or p.query or p.fragment:
        raise ValueError('official HTTPS bot endpoint required')
    if not re.fullmatch(r'/open-apis/bot/v2/hook/[A-Za-z0-9-]{20,80}',p.path):raise ValueError('invalid bot hook path')
    return url


def signed_payload(text,secret=None,timestamp=None):
    p=json.loads(json.dumps(text)) if isinstance(text,dict) else dict(msg_type='text',content=dict(text=text))
    if secret:
        timestamp=str(int(time.time()) if timestamp is None else timestamp)
        p.update(timestamp=timestamp,sign=base64.b64encode(hmac.new((timestamp+'\n'+secret).encode(),b'',hashlib.sha256).digest()).decode())
    return p


def clean(text):
    text=re.sub(r'https://(?:open\.feishu\.cn|open\.larksuite\.com)/open-apis/bot/v2/hook/[^\s]+','[私有机器人地址]',str(text))
    return re.sub(r'\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b','[私有标识]',text).replace('<at','&lt;at')


def sanitize(value):
    if isinstance(value,str):return clean(value)
    if isinstance(value,list):return [sanitize(v) for v in value]
    if isinstance(value,dict):return {k:sanitize(v) for k,v in value.items()}
    return value


def message(title,lines,tag='STRATEGY',fields=None,reasons=None,execution=None):
    label='联调，不计策略成绩' if tag=='CONNECTIVITY_TEST' else '模拟观察，实盘关闭'
    color='grey' if tag=='CONNECTIVITY_TEST' else 'red' if any(w in title for w in ('异常','停止','不可用','风险')) else 'orange' if any(w in title for w in ('退出待','离场','拒单')) else 'green' if any(w in title for w in ('已成交','确认已平')) else 'blue'
    fields=fields or [('消息类型',title),('执行环境',label)]
    elements=[dict(tag='div',fields=[dict(is_short=True,text=dict(tag='lark_md',content='**'+clean(k)+'**\n'+clean(v))) for k,v in fields[offset:offset+10]]) for offset in range(0,len(fields),10)]
    elements.extend([dict(tag='hr'),dict(tag='div',text=dict(tag='lark_md',content='**交易理由／风控依据**\n'+'\n'.join('• '+clean(x) for x in (reasons or lines))))])
    if execution:elements.extend([dict(tag='hr'),dict(tag='div',text=dict(tag='lark_md',content='**执行与数据依据**\n'+'\n'.join('• '+clean(x) for x in execution)))])
    elements.extend([dict(tag='hr'),dict(tag='note',elements=[dict(tag='plain_text',content='期货模拟观察｜真实行情，GM模拟账户｜平台模拟扣费｜实盘关闭；策略有效性仍待验证。')])])
    payload=dict(msg_type='interactive',card=dict(config=dict(wide_screen_mode=True),header=dict(template=color,
        title=dict(tag='plain_text',content='期货模拟观察 | '+title+(' | 联调' if tag=='CONNECTIVITY_TEST' else ''))),elements=elements))
    # Sanitize the entire structured card, including vendor rejection text.
    return sanitize(payload)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def send_once(config,text,opener=None):
    url=valid_webhook(config['webhook'])
    secret=config.get('signing_secret') or os.environ.get(config.get('signing_secret_env','FEISHU_SIGNING_SECRET'))
    body=json.dumps(signed_payload(text,secret),ensure_ascii=False).encode()
    if len(body)>19000:raise ValueError('notification exceeds bot payload limit')
    request=Request(url,data=body,headers={'Content-Type':'application/json'},method='POST')
    try:
        response=(opener or build_opener(NoRedirect())).open(request,timeout=3)
        with response:result=json.loads(response.read(65536).decode('utf8'))
        code=result.get('code',result.get('StatusCode'))
        return dict(status='SENT' if code==0 else 'REJECTED_VENDOR',vendor_code=code)
    except HTTPError as exc:
        return dict(status='RATE_LIMITED' if exc.code==429 else 'UNKNOWN_DELIVERY' if exc.code>=500 else 'HTTP_REJECTED',http_code=exc.code)
    except Exception:
        # A timeout can occur after delivery. Do not replay automatically and claim exactly-once.
        return dict(status='UNKNOWN_DELIVERY',reason='NETWORK_OR_RESPONSE_OUTCOME_UNKNOWN')


class FeishuOutbox:
    def __init__(self,config_path,evidence_dir,start=True):
        self.config_path=str(config_path);self.root=Path(evidence_dir)/'notifications';self.root.mkdir(parents=True,exist_ok=True)
        self.process=None
        if start:
            self.process=multiprocessing.get_context('spawn').Process(target=sender,args=(self.config_path,str(self.root)),daemon=True,name='paper-feishu')
            self.process.start()
    def enabled(self):
        p=Path(self.config_path)
        if not p.exists():return False
        try:c=json.loads(p.read_text(encoding='utf-8-sig'));return c.get('enabled') is True and bool(c.get('webhook'))
        except (ValueError,OSError):return False
    def enqueue(self,key,text):
        key=hashlib.sha256(key.encode()).hexdigest();path=self.root/(key+'.json')
        if path.exists():return key
        atomic_json(path,dict(id=key,created_at=datetime.now(timezone.utc).isoformat(),text=sanitize(text),
            status='PENDING' if self.enabled() else 'LOCAL_ONLY_NOT_CONFIGURED',attempts=0))
        return key
    def health(self):
        path=self.root/'_health.json';counts={}
        if path.exists():
            try:counts=json.loads(path.read_text(encoding='utf8')).get('outbox_statuses',{})
            except (ValueError,OSError):pass
        return dict(configured=self.enabled(),worker_alive=self.process is not None and self.process.is_alive(),outbox_statuses=counts)


def sender(config_path,folder):
    folder=Path(folder);terminal=set();counts={};states={};last_health=0.
    def files():return [p for p in folder.glob('*.json') if len(p.stem)==64 and all(x in '0123456789abcdef' for x in p.stem)]
    def count(row):
        previous=states.get(row['id'])
        if previous==row['status']:return
        if previous:counts[previous]=counts.get(previous,1)-1
        counts[row['status']]=counts.get(row['status'],0)+1;states[row['id']]=row['status']
    for p in files():
        row=json.loads(p.read_text(encoding='utf8'))
        if row['status']=='SENDING':row.update(status='UNKNOWN_DELIVERY',reason='SENDER_RESTART_AFTER_SEND_INTENT');atomic_json(p,row)
        count(row)
        if row['status'] not in ('PENDING','RATE_LIMITED'):terminal.add(p.stem)
    while True:
        try:
            config=json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
            if config.get('enabled') is not True:time.sleep(1);continue
            valid_webhook(config['webhook'])
        except Exception:time.sleep(1);continue
        for p in sorted(files()):
            if p.stem in terminal:continue
            row=json.loads(p.read_text(encoding='utf8'))
            count(row)
            if row['status'] not in ('PENDING','RATE_LIMITED'):terminal.add(p.stem);continue
            if row['status'] not in ('PENDING','RATE_LIMITED') or row.get('retry_at',0)>time.time():continue
            row.update(status='SENDING',attempts=row['attempts']+1);atomic_json(p,row)
            result=send_once(config,row['text']);row.update(result,finished_at=datetime.now(timezone.utc).isoformat())
            if row['status']=='RATE_LIMITED':
                if row['attempts']>=3:row['status']='RATE_LIMIT_EXHAUSTED'
                else:row['retry_at']=time.time()+min(60,2**row['attempts'])
            atomic_json(p,row);count(row)
            if row['status'] not in ('PENDING','RATE_LIMITED'):terminal.add(p.stem)
            time.sleep(.6)
        if time.monotonic()-last_health>2:
            atomic_json(folder/'_health.json',dict(at=datetime.now(timezone.utc).isoformat(),outbox_statuses=counts));last_health=time.monotonic()
        time.sleep(.5)
