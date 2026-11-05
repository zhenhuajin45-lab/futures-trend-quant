"""Frozen authorized paper pool. Standard library only: also imported by GM SIM Python 3.10."""
import math
import re

FIVE_POOL=('DCE.M','SHFE.AU','DCE.EG','DCE.JD','CZCE.CJ')
PRODUCTS={
    'DCE.M':dict(name='豆粕',exchange='DCE',pattern=r'DCE\.m\d{4}'),
    'SHFE.AU':dict(name='黄金',exchange='SHFE',pattern=r'SHFE\.au\d{4}'),
    'DCE.EG':dict(name='乙二醇',exchange='DCE',pattern=r'DCE\.eg\d{4}'),
    'DCE.JD':dict(name='鸡蛋',exchange='DCE',pattern=r'DCE\.jd\d{4}'),
    'CZCE.CJ':dict(name='红枣',exchange='CZCE',pattern=r'CZCE\.CJ\d{3}'),
}


def product_of(symbol):
    for cs,meta in PRODUCTS.items():
        if re.fullmatch(meta['pattern'],str(symbol)):return cs
    raise ValueError('instrument outside authorized paper pool')


def validate_actual(csymbol,symbol):
    if product_of(symbol)!=csymbol:raise ValueError('actual contract/product mismatch')
    return symbol


def close_effect(symbol,position):
    """GM: Close=2, CloseToday=3, CloseYesterday=4. Never infer an unknown SHFE bucket."""
    if not symbol.startswith('SHFE.'):return 2
    available=int(position.get('available',0))
    if 'available_today' not in position or 'volume_today' not in position:raise ValueError('SHFE today/yesterday availability unknown')
    today=int(position['available_today']);volume_today=int(position['volume_today'])
    if not 0<=today<=min(available,volume_today):raise ValueError('invalid SHFE position buckets')
    if today>=1:return 3
    if available-today>=1:return 4
    raise ValueError('no confirmed SHFE close bucket')


def bucket_available(position,effect):
    get=lambda key,default=0:getattr(position,key,default) if not isinstance(position,dict) else position.get(key,default)
    available=int(get('available'))
    if effect==2:return available
    today=get('available_today',None)
    if today is None:raise ValueError('close bucket unknown at final broker exit')
    today=int(today)
    if not 0<=today<=available:raise ValueError('invalid available position bucket')
    return today if effect==3 else available-today if effect==4 else 0
