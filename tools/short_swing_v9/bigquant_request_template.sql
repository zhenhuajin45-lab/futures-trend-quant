-- TEMPLATE ONLY: no authenticated export was run. Split by actual contract and day.
-- date is M1 END; L1 date is event time; trading_day supplies night ownership.
SELECT date,instrument,trading_day,open,high,low,close,volume,amount,open_interest
FROM cn_future_bar1m_c
WHERE instrument IN ('lh2511.DCE','lh2601.DCE','lh2603.DCE','m2601.DCE','m2605.DCE','CF601.CZC','CF605.CZC','lc2511.GFE','lc2601.GFE','lc2605.GFE','SA601.CZC','SA605.CZC','jd2510.DCE','jd2511.DCE','jd2512.DCE','jd2601.DCE','jd2602.DCE','v2601.DCE','v2605.DCE','eg2601.DCE','eg2605.DCE','au2510.SHF','au2512.SHF','au2602.SHF','ag2510.SHF','ag2512.SHF','ag2602.SHF','ag2604.SHF','CJ601.CZC','CJ605.CZC','SR601.CZC','SR605.CZC')
AND date >= TIMESTAMP '2025-03-03 00:00:00' AND date < TIMESTAMP '2026-01-01 00:00:00'
ORDER BY instrument,date;

-- First confirm entitlement and exact L1 column names against the table schema.
-- Required: date,trading_day,instrument,bid/ask one price AND size,last,upper/lower limit.
-- Request selected windows plus next-trading-day old-leg exit tails from request CSV.
