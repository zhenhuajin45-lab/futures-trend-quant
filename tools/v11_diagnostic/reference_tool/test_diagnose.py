import unittest
from datetime import datetime
from types import SimpleNamespace
import numpy as np
import pandas as pd
from diagnose import Clock, NS, bucket, grid, first_touch, measure_space

class Fixture(unittest.TestCase):
    def setUp(self):
        self.days=pd.date_range('2026-05-01',periods=24)
        self.opens=[]; self.dates=[]; deadlines={}
        for day in self.days:
            date=str(day.date())
            start=pd.Timestamp(date+'T09:00:00+08:00').value
            self.opens.extend((start,start+NS,start+4*NS))
            self.dates.extend([date]*3); deadlines[date]=start+5*NS
        self.clock=Clock(dict(ns=np.array(self.opens)+NS,days=self.dates),
                         dict(deadlines=deadlines,blackouts=[]))
        self.i=3*21; self.now=self.opens[self.i]
        self.leg=dict(index={x+NS:i for i,x in enumerate(self.opens)},
                      values=np.tile([100.,103.,99.,101.,1.],(len(self.opens),1)),
                      facts={self.now:dict(core={'close':100.},eligible=True)})
    def space(self):
        return measure_space(self.leg,self.clock,self.i,self.now,self.clock.expiry(self.i),98.,'bull')
    def test_minute_gap(self):
        self.assertEqual(self.clock.span(self.now,self.clock.expiry(self.i)),3)
    def test_old_only_and_r0(self):
        self.assertEqual(self.space()[0],2.)
        self.leg['values'][self.i:,1]=999.
        self.assertEqual(self.space()[0],2.)
    def test_missing_no_replacement(self):
        self.leg['index'].pop(self.opens[self.i-4]+NS)
        self.assertEqual(self.space()[1],'MISSING_HISTORICAL_MINUTE')
    def test_insufficient(self):
        self.i=0; self.now=self.opens[0];self.leg['facts'][self.now]=dict(core={'close':100},eligible=True)
        self.assertEqual(self.space()[1],'FEWER_THAN_20_WINDOWS')
    def test_tied_buckets(self):
        b,_=bucket(pd.Series([1,1,1,1,2,2]),3)
        self.assertEqual(b.iloc[0],b.iloc[3]); self.assertEqual(b.iloc[4],b.iloc[5])
    def test_first_touch(self):
        end=self.clock.expiry(self.i)
        self.assertEqual(first_touch(self.leg,self.clock,self.i,end,'bull',99.,102.),'AMBIGUOUS')
        self.leg['values'][self.i,0]=99.
        self.assertEqual(first_touch(self.leg,self.clock,self.i,end,'bull',99.,102.),'STOP')
        self.assertEqual(first_touch(self.leg,self.clock,self.i,end,'bear',102.,99.),'TARGET')
        self.leg['index'].pop(self.now+NS)
        self.assertEqual(first_touch(self.leg,self.clock,self.i,end,'bear',102.,99.),'MISSING')
    def test_grid(self):
        f=pd.DataFrame(dict(remaining_minutes=[1,2,3],range_ratio=[1,2,3],gross_r=[-1,0,1],
                            hold_minutes=[1,2,2],csymbol=['AG']*3,entry_day=['d']*3,price_path=['NEITHER']*3))
        rows,*_=grid(f,3)
        self.assertEqual(len(rows),9); self.assertEqual(sum(r['n'] for r in rows),3)
        self.assertTrue(np.isnan(rows[1]['mean_gross_r']))

if __name__=='__main__':unittest.main()
