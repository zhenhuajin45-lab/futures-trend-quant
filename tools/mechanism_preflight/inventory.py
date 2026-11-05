"""Inspect metadata and allowlisted old sparse-quote schemas, never price values."""
from pathlib import Path
import argparse,csv,json
import pandas as pd
import pyarrow.parquet as pq
from .common import read,write,sha,publish,result_columns,authorize_price_span,output_target


def parquet_metadata(path):
    f=pq.ParquetFile(path);columns=f.schema_arrow.names;times=[];symbols=set();frequencies=set()
    for i in range(f.metadata.num_row_groups):
        group=f.metadata.row_group(i)
        for j in range(group.num_columns):
            c=group.column(j);s=c.statistics
            if s is None or not s.has_min_max:continue
            if c.path_in_schema=='eob':times.extend([pd.Timestamp(s.min),pd.Timestamp(s.max)])
            elif c.path_in_schema=='symbol':symbols.update([str(s.min),str(s.max)])
            elif c.path_in_schema=='frequency':frequencies.update([str(s.min),str(s.max)])
    return dict(fields=columns,rows=f.metadata.num_rows,min_event_at=min(times).isoformat() if times else None,
        max_event_at=max(times).isoformat() if times else None,contracts=sorted(symbols),resolution='|'.join(sorted(frequencies)) or 'UNVERIFIED',
        schema_evidence='PARQUET_FOOTER_OBSERVED_NO_PRICE_BODY_READ')


def trusted_sparse_quote_schema(path,item,policy):
    if not item.get('expected_sha256') or sha(path)!=item['expected_sha256'] or not authorize_price_span(policy,item.get('scope_start'),item.get('scope_end'),item.get('purpose')):
        raise ValueError('untrusted or protected sparse pickle refused before deserialization')
    f=pd.read_pickle(path)  # Only own previously hashed/authorized, bounded old-study samples.
    if result_columns(f.columns):raise ValueError('outcome columns in quote source')
    levels=sorted({len(q) for q in f.quotes}) if 'quotes' in f.columns else []
    times=pd.to_datetime(f.created_at,utc=True) if 'created_at' in f.columns else None
    return dict(fields=list(f.columns),rows=len(f),min_event_at=times.min().isoformat() if times is not None and len(f) else None,
        max_event_at=times.max().isoformat() if times is not None and len(f) else None,contracts=item.get('contracts',[]),
        resolution='SPARSE_TICK_5_SECOND_WINDOWS',schema_evidence='TRUSTED_OLD_QUOTE_COLUMNS_CLOCK_AND_DEPTH_COUNTS_ONLY; NO_PRICE_ANALYSIS',
        observed_book_levels=json.dumps(levels),received_at_field_present='received_at' in f.columns,
        continuous_book_verified=False)


def run(source_index,protected,out):
    output_target(out)
    index=read(source_index);policy=read(protected);rows=[]
    for item in index['sources']:
        path=Path(item['path']);row=dict(source_id=item['source_id'],path=str(path),provider=item.get('provider','UNKNOWN'),
            kind=item['kind'],purpose=item.get('purpose','METADATA_ONLY'),authorization=item.get('authorization','UNVERIFIED'),
            redistribution='NOT_AUTHORIZED; RAW_LOCAL_ONLY',arrival_clock=item.get('arrival_clock','UNKNOWN'),
            license_evidence=item.get('license_evidence','USER_LOCAL_RESEARCH_SCOPE; PROVIDER_CONTRACT_NOT_REAUDITED'),
            exists=path.is_file(),price_body_read=False)
        meta=dict(fields=[],rows=None,min_event_at=None,max_event_at=None,contracts=item.get('contracts',[]),resolution='UNKNOWN',schema_evidence='UNVERIFIED_NO_DESERIALIZATION')
        if path.is_file():
            row.update(sha256=sha(path),bytes=path.stat().st_size)
            if item.get('expected_sha256') and row['sha256']!=item['expected_sha256']:raise ValueError('original source digest mismatch')
            if path.suffix.lower()=='.parquet':meta=parquet_metadata(path)
            elif path.suffix.lower()=='.csv':
                for encoding in ('utf-8-sig','gb18030'):
                    try:
                        with path.open(encoding=encoding,newline='') as f:meta['fields']=next(csv.reader(f),[])
                        meta['schema_evidence']='CSV_HEADER_ONLY_'+encoding;break
                    except UnicodeDecodeError:continue
            elif item['kind'] in ('CALENDAR','MAPPING'):
                obj=read(path)
                if item['kind']=='CALENDAR' and isinstance(obj,list) and obj and all(isinstance(r,dict) and 'eob' in r for r in obj):
                    times=[pd.Timestamp(r['eob']) for r in obj]
                    meta.update(fields=['eob','trading_date'],rows=len(obj),min_event_at=min(times).isoformat(),max_event_at=max(times).isoformat(),resolution='EXPECTED_60s')
                elif item['kind']=='MAPPING' and isinstance(obj,dict):
                    meta.update(fields=['trading_date','actual_contract'],rows=len(obj),contracts=sorted({x for x in obj.values() if isinstance(x,str) and x}))
                meta['schema_evidence']='NONPRICE_CALENDAR_OR_MAPPING_OBSERVED'
            elif item['kind']=='SPARSE_ENTRY_QUOTES':
                meta=trusted_sparse_quote_schema(path,item,policy);row['price_body_read']=True
        else:row.update(sha256=None,bytes=None)
        permitted=authorize_price_span(policy,meta['min_event_at'],meta['max_event_at'],row['purpose'])
        forbidden=result_columns(meta['fields'])
        row.update(min_event_at=meta['min_event_at'],max_event_at=meta['max_event_at'],rows=meta['rows'],
            fields=json.dumps(meta['fields']),contracts=json.dumps(meta['contracts']),finest_resolution=meta['resolution'],schema_evidence=meta['schema_evidence'],
            engineering_price_access=bool(permitted and not forbidden and path.suffix.lower()=='.parquet'),
            new_development_access=False,independent_access=False,
            observed_book_levels=meta.get('observed_book_levels','UNKNOWN'),received_at_field_present=meta.get('received_at_field_present',False),
            access_reason='RESULT_COLUMNS_FORBIDDEN' if forbidden else 'OLD_ENGINEERING_EXCEPTION_ONLY' if permitted else 'PROTECTED_OR_UNKNOWN_RANGE_METADATA_ONLY')
        rows.append(row)
    frame=pd.DataFrame(rows)
    def build(stage):
        frame.to_csv(stage/'inventory.csv',index=False)
        write(stage/'index_binding.json',dict(source_index=str(Path(source_index).resolve()),source_index_sha256=sha(source_index),
            protected=str(Path(protected).resolve()),protected_sha256=sha(protected),inventory_sha256=sha(stage/'inventory.csv'),
            price_bodies_read=int(frame.price_body_read.sum()),price_analysis_performed=False,unique_source_hashes=int(frame.sha256.nunique()),indexed_files=len(frame)))
    publish(out,build)
    return dict(indexed_files=len(frame),unique_source_hashes=int(frame.sha256.nunique()),trusted_quote_schema_bodies=int(frame.price_body_read.sum()),price_analysis_performed=False)


def main():
    p=argparse.ArgumentParser();p.add_argument('--source-index',type=Path,required=True);p.add_argument('--protected',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();print(json.dumps(run(a.source_index,a.protected,a.out)))


if __name__=='__main__':main()
