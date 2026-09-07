"""Offline audit of exported daily bundles. Never connects to a broker.

All P&L fields are explicitly quote/order-price ESTIMATES, not realized fills.
Position lifecycle quantity changes corroborate execution state, not prices.
Usage: python tools/audit_trade_bundles.py --input ../analysis/input --output ../analysis/results
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import re
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def num(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def stamp(value):
    return datetime.fromisoformat(value)


def quantile(values, p):
    vals = sorted(values)
    if not vals:
        return None
    loc = (len(vals) - 1) * p
    lo = int(loc)
    return vals[lo] + (vals[min(lo + 1, len(vals) - 1)] - vals[lo]) * (loc - lo)


def stats(rows, key="net_base_estimate"):
    vals = [r[key] for r in rows]
    wins, losses = [v for v in vals if v > 0], [-v for v in vals if v < 0]
    return dict(n=len(vals), wins=len(wins), losses=len(losses), flats=vals.count(0),
                win_rate=len(wins) / len(vals) if vals else None,
                total=sum(vals), expectancy=st.mean(vals) if vals else None,
                average_win=st.mean(wins) if wins else None,
                average_loss=st.mean(losses) if losses else None,
                payoff_ratio=st.mean(wins) / st.mean(losses) if wins and losses else None,
                profit_factor=sum(wins) / sum(losses) if losses else None)


def lifecycle_info(events, order_id, side, quantity):
    matches = [i for i, r in enumerate(events) if r["event"] == "ORDER_ID_CONFIRMED"
               and r["detail"] == f"order_id={order_id}"]
    if len(matches) != 1:
        return {"join_status": "UNAVAILABLE"}
    start = matches[0]
    end = next((i for i in range(start + 1, len(events))
                if events[i]["event"] == "ORDER_ID_CONFIRMED"), len(events))
    segment = events[start:end]
    observed = [(stamp(r["timestamp"]), num(r["broker_quantity"])) for r in segment
                if r["event"] in ("BUY_CONFIRMED", "SELL_RESULT", "ORPHAN_CLEARED", "SYNC")
                and num(r["broker_quantity"]) is not None]
    partial = any(0 < q < quantity for _, q in observed)
    target = quantity if side == "BUY" else 0
    terminal = [t for t, q in observed if q == target]
    first = [t for t, q in observed if (q > 0 if side == "BUY" else q < quantity)]
    return dict(join_status="MATCHED", partial_quantity_observed=partial,
                first_observed_at=first[0].isoformat() if first else None,
                terminal_observed_at=terminal[0].isoformat() if terminal else None,
                timeout_count=sum(r["event"] == "PENDING_TIMEOUT" for r in segment))


def band(value, cuts, labels):
    if value is None:
        return "UNAVAILABLE"
    for threshold, label in zip(cuts, labels):
        if value < threshold:
            return label
    return labels[-1]


def write_csv(path, rows):
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def analyze(input_dir, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    trades, provenance, shadow_checks, prices_by_day_symbol = [], [], [], {}
    day_quality, candidate_blocks = [], []
    for day in sorted(input_dir.glob("bundle_*")):
        raw = day / "raw"
        rows = read_csv(next(raw.glob("trades_*.csv")))
        signals = read_csv(next(raw.glob("signal_log_*.csv")))
        lifecycle = read_csv(next(raw.glob("position_lifecycle_*.csv")))
        extensions = read_csv(next(raw.glob("min_profit_extension_shadow_*.csv")))
        shadows = read_csv(next(raw.glob("entry_watch_shadow_*.csv")))
        low_upside = read_csv(next(raw.glob("low_upside_shadow_*.csv")))
        by_sym, life_sym = defaultdict(list), defaultdict(list)
        for r in signals:
            by_sym[r["symbol"]].append(r)
        for r in lifecycle:
            life_sym[r["symbol"]].append(r)
        for sym, sr in by_sym.items():
            sr.sort(key=lambda r: r["timestamp"])
            prices_by_day_symbol[(day.name, sym)] = sr
        quality = (day / "metadata/collection_quality.txt").read_text()
        app_log = next(raw.glob("app_analysis_*.log")).read_text()
        block_rows = [r for r in low_upside if r.get("order_block_reason") == "SKIP_CANDIDATE_A_GUARD"]
        candidate_blocks.extend(dict(date=day.name[-8:], symbol=r["symbol"], timestamp=r["timestamp"],
                                     independent_trade=False) for r in block_rows)
        day_quality.append(dict(
            date=day.name[-8:], order_rows=len(rows), signal_rows=len(signals),
            collection_status=re.search(r"^collection_status\s*=\s*(\S+)", quality, re.M).group(1),
            process_start_count=int(re.search(r"^process_start_count\s*=\s*(\d+)", quality, re.M).group(1)),
            actual_candidate_a_block_events=len(block_rows),
            lifecycle_timeouts=sum(r["event"] == "PENDING_TIMEOUT" for r in lifecycle),
            order_balance_mismatch_events=app_log.count("[ORDER_STATUS_BALANCE_MISMATCH]"),
        ))
        for path in sorted(day.rglob("*")):
            if path.is_file():
                provenance.append(dict(path=str(path.relative_to(input_dir)), bytes=path.stat().st_size,
                                       sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        open_buys, entry_counts = {}, Counter()
        for row_no, row in enumerate(rows, 2):
            if row["accepted"].lower() != "true":
                continue
            sym = row["symbol"]
            qty, price = int(row["quantity"]), int(row["price"])
            if row["side"] == "BUY":
                if sym in open_buys:
                    raise ValueError("Overlapping buy lots require a fill ledger; do not guess")
                open_buys[sym] = (row, row_no)
                entry_counts[sym] += 1
                continue
            if sym not in open_buys:
                raise ValueError("Unmatched sale; opening position data is required")
            buy, buy_line = open_buys.pop(sym)
            if int(buy["quantity"]) != qty:
                raise ValueError("Order quantity mismatch; actual partial fills are required")
            start, end = stamp(buy["timestamp"]), stamp(row["timestamp"])
            candidates = [s for s in by_sym[sym] if s["final_decision"] == "BUY"
                          and abs((stamp(s["timestamp"]) - start).total_seconds()) < 2]
            sig = candidates[0] if len(candidates) == 1 else {}
            bprice = int(buy["price"])
            avg = num(row["avg_buy_price"])
            capital, gross = qty * bprice, qty * (price - bprice)
            info_buy = lifecycle_info(life_sym[sym], buy["order_id"], "BUY", qty)
            info_sell = lifecycle_info(life_sym[sym], row["order_id"], "SELL", qty)
            e = dict(id=f"{day.name[-8:]}-{buy['order_id']}", date=start.date().isoformat(),
                     symbol=sym, buy_order_id=buy["order_id"], sell_order_id=row["order_id"],
                     entry_at=start.isoformat(), exit_order_at=end.isoformat(), quantity=qty,
                     buy_quote=bprice, exit_quote=price, entry_average_snapshot=avg,
                     actual_buy_fill_price=None, actual_sell_fill_price=None,
                     actual_fees=None, actual_tax=None, actual_net_pnl=None,
                     entry_notional=capital, gross_order_price_estimate=gross,
                     cost_base_estimate=capital * .0035, cost_stress_estimate=capital * .009,
                     net_base_estimate=gross-capital*.0035, net_stress_estimate=gross-capital*.009,
                     gross_pct_estimate=(price/bprice-1)*100,
                     gross_entry_avg_exit_quote_estimate=qty*(price-avg) if avg else None,
                     entry_quote_vs_average_difference=qty*(avg-bprice) if avg else None,
                     reported_hold_minutes=num(row["hold_minutes"]),
                     order_to_order_minutes=(end-start).total_seconds()/60,
                     market_regime=buy["market_regime"], entry_score=num(buy["entry_score"]),
                     entry_reason=buy["entry_reason"], exit_reason=row["exit_reason"],
                     exit_class=("MIN_PROFIT_5M" if "최소수익미달" in row["exit_reason"] else
                                 "CRASH_CUT" if "급락청산" in row["exit_reason"] else
                                 "VWAP_BREAK" if "VWAP이탈" in row["exit_reason"] else "TRAILING"),
                     entry_time_bucket=band(start.hour*60+start.minute, [600,660,810,890],
                                           ["09:00-10:00","10:00-11:00","11:00-13:30","13:30-14:50","14:50+"]),
                     weekday=start.strftime("%A"), reentry_index=entry_counts[sym],
                     signal_join_status="MATCHED" if sig else "UNAVAILABLE",
                     patterns=sig.get("detected_patterns", "UNAVAILABLE"),
                     vwap_distance_pct=num(buy["current_vs_vwap_pct"]),
                     upside_to_recent_high_pct=num(buy["upside_to_recent_high_pct"]),
                     v_pattern_volume_ratio=num(buy["volume_ratio"]),
                     liquidity_proxy=num(buy["bar_amount"]),
                     rebound_volume_spike=buy["rebound_volume_spike"],
                     change_rate_pct=num(sig.get("change_rate_pct")),
                     rebound_volume_ratio=num(sig.get("rebound_volume_ratio")),
                     macd_above_signal=sig.get("macd_above_signal") or "UNAVAILABLE",
                     macd_hist_direction=sig.get("macd_hist_direction") or "UNAVAILABLE",
                     ma5_above_ma20=sig.get("ma5_above_ma20") or "UNAVAILABLE",
                     atr_14_pct=num(sig.get("atr_14_pct")),
                     bb_percent_b=num(sig.get("bb_percent_b")),
                     partial_quantity_observed=bool(info_buy.get("partial_quantity_observed") or info_sell.get("partial_quantity_observed")),
                     timeout_count=info_buy.get("timeout_count",0)+info_sell.get("timeout_count",0),
                     buy_first_observed_at=info_buy.get("first_observed_at"),
                     buy_full_observed_at=info_buy.get("terminal_observed_at"),
                     sell_first_observed_at=info_sell.get("first_observed_at"),
                     sell_full_observed_at=info_sell.get("terminal_observed_at"),
                     source=f"{day.name}/raw/trades_{day.name[-8:]}.csv", buy_line=buy_line, sell_line=row_no,
                     causal_loss_attribution="UNDETERMINED", regime_is_market_index=False,
                     price_basis="ORDER_QUOTES_NOT_ACTUAL_FILLS")
            for kind, at, anchor in [("buy_first", e["buy_first_observed_at"], start),
                                     ("buy_full", e["buy_full_observed_at"], start),
                                     ("sell_full", e["sell_full_observed_at"], end)]:
                e[kind+"_observation_delay_seconds"] = (stamp(at)-anchor).total_seconds() if at else None
            e["observed_holding_minutes"] = ((stamp(e["sell_full_observed_at"])-stamp(e["buy_first_observed_at"])).total_seconds()/60
                                              if e["sell_full_observed_at"] and e["buy_first_observed_at"] else None)
            during = [num(s["price"]) for s in by_sym[sym] if start < stamp(s["timestamp"]) <= end and num(s["price"])]
            e["quote_samples_during_position"] = len(during)
            e["sampled_mfe_pct"] = (max([avg, *during])/avg-1)*100 if during and avg else None
            e["sampled_mae_pct"] = (min([avg, *during])/avg-1)*100 if during and avg else None
            e["sampled_peak_giveback_ratio"] = (
                (e["sampled_mfe_pct"] - (price/avg-1)*100)/e["sampled_mfe_pct"]
                if e["sampled_mfe_pct"] and avg else None)
            ext = [r for r in extensions if r["symbol"] == sym and abs((stamp(r["timestamp"])-end).total_seconds())<2]
            e["logged_peak_pnl_pct"] = num(ext[0]["peak_pnl_pct"]) if len(ext)==1 else None
            for horizon in [1,3,5,10,20]:
                future = [s for s in by_sym[sym] if start < stamp(s["timestamp"]) <= start+timedelta(minutes=horizon) and num(s["price"])]
                tail = [s for s in future if (start+timedelta(minutes=horizon)-stamp(s["timestamp"])).total_seconds()<=60]
                e[f"sampled_mfe_{horizon}m_pct"] = (max([bprice, *[float(s["price"]) for s in future]])/bprice-1)*100 if tail else None
                e[f"sampled_mae_{horizon}m_pct"] = (min([bprice, *[float(s["price"]) for s in future]])/bprice-1)*100 if tail else None
                e[f"quote_return_{horizon}m_pct"] = (float(tail[-1]["price"])/bprice-1)*100 if tail else None
                e[f"quote_return_{horizon}m_age_seconds"] = (start+timedelta(minutes=horizon)-stamp(tail[-1]["timestamp"])).total_seconds() if tail else None
            for cp in [5,10,20]:
                matches = [r for r in shadows if r["symbol"]==sym and num(r["checkpoint_min"])==cp
                           and abs((stamp(r["trigger_at"])-end).total_seconds())<2
                           and num(r["trigger_price"])==price and num(r["entry_price"])==avg]
                if len(matches)==1:
                    r=matches[0]
                    shadow_checks.append(dict(trade_id=e["id"],symbol=sym,date=e["date"],exit_class=e["exit_class"],
                                              checkpoint_minutes=cp, trigger_price=price, checkpoint_price=num(r["checkpoint_price"]),
                                              quoted_extension_difference=qty*(float(r["checkpoint_price"])-price),
                                              estimated_pct_difference=(float(r["checkpoint_price"])-price)/avg*100,
                                              hypothetical=True, execution_price_available=False,
                                              checkpoint_observed_timestamp_available=False))
            trades.append(e)
        if open_buys:
            raise ValueError("Unclosed order pairs require a separate open-position report")

    trades.sort(key=lambda r: r["exit_order_at"])
    equity, peak, longest_streak, streak = 0., 0., 0, 0
    drawdown, peak_index, max_dd_start, max_dd_end = 0., -1, None, None
    recovery_episodes, active_recovery = [], None
    for i,t in enumerate(trades):
        equity += t["net_base_estimate"]
        if equity >= peak:
            if active_recovery is not None:
                recovery_episodes.append(dict(start=active_recovery, end=i, trades=i-active_recovery,
                                              seconds=(stamp(t["exit_order_at"])-stamp(trades[max(active_recovery,0)]["exit_order_at"])).total_seconds()))
                active_recovery=None
            peak, peak_index = equity, i
        elif active_recovery is None:
            active_recovery=peak_index
        if equity-peak < drawdown:
            drawdown, max_dd_start, max_dd_end = equity-peak, peak_index, i
        streak=streak+1 if t["net_base_estimate"]<0 else 0
        longest_streak=max(longest_streak,streak)
        t.update(cumulative_net_estimate=equity, drawdown_estimate=equity-peak, consecutive_net_losses=streak)
    daily=[]
    for day in sorted({t["date"] for t in trades}):
        group=[t for t in trades if t["date"]==day]
        daily.append(dict(date=day, **stats(group), gross=sum(t["gross_order_price_estimate"] for t in group),
                          estimated_cost=sum(t["cost_base_estimate"] for t in group)))
    weeks=defaultdict(list)
    for t in trades:
        weeks[stamp(t["entry_at"]).strftime("%G-W%V")].append(t)
    weekly=[dict(week=k, **stats(v)) for k,v in sorted(weeks.items())]
    groups=[]
    group_keys=["entry_time_bucket","weekday","market_regime","entry_score","patterns","macd_above_signal",
                "macd_hist_direction","ma5_above_ma20","rebound_volume_spike","reentry_index","exit_class","partial_quantity_observed"]
    specs={"upside_band":("upside_to_recent_high_pct",[.5,1,2],["<0.5%","0.5-1%","1-2%","2%+"]),
           "day_change_band":("change_rate_pct",[2,5,10],["<2%","2-5%","5-10%","10%+"]),
           "vwap_distance_band":("vwap_distance_pct",[0,1,2],["below","0-1%","1-2%","2%+"]),
           "holding_band":("reported_hold_minutes",[3,6,10],["<3m","3-6m","6-10m","10m+"]),
           "liquidity_proxy_band":("liquidity_proxy",[100e9,500e9],["<100bn","100-500bn","500bn+"]),
           "entry_confirmation_delay_band":("buy_first_observation_delay_seconds",[15,30,60],["<15s","15-30s","30-60s","60s+"]),
           "volume_spike_ratio_band":("rebound_volume_ratio",[1,1.5,2],["<1","1-1.5","1.5-2","2+"])}
    for k,(field,cuts,labels) in specs.items():
        for t in trades:t[k]=band(t[field],cuts,labels)
        group_keys.append(k)
    for field in group_keys:
        grouped=defaultdict(list)
        for t in trades:grouped[str(t[field])].append(t)
        for value,items in sorted(grouped.items()):
            groups.append(dict(dimension=field, group=value, **stats(items), inferential_status="DESCRIPTIVE_SMALL_SAMPLE"))
    concentration=[dict(symbol=sym, **stats([t for t in trades if t["symbol"]==sym])) for sym in sorted({t['symbol'] for t in trades})]
    top=sorted(trades,key=lambda t:t["net_base_estimate"],reverse=True)
    robustness=[dict(scenario=f"remove_best_{n}_trades",**stats(top[n:])) for n in [1,3,5]]
    best_day=max(daily,key=lambda d:d['total'])['date']
    robustness.append(dict(scenario=f"remove_best_day_{best_day}",**stats([t for t in trades if t['date']!=best_day])))
    for symbol in sorted({t['symbol'] for t in trades}):
        robustness.append(dict(scenario=f"leave_out_symbol_{symbol}",**stats([t for t in trades if t['symbol']!=symbol])))
    rng=random.Random(20260907)
    grouped_days=[[t for t in trades if t['date']==d['date']] for d in daily]
    bootstrap=[]
    for _ in range(20000):
        sample=[t for g in rng.choices(grouped_days,k=len(grouped_days)) for t in g]
        bootstrap.append(st.mean(t['net_base_estimate'] for t in sample))
    shuffled_dd=[]
    vals=[t['net_base_estimate'] for t in trades]
    for _ in range(5000):
        rng.shuffle(vals); cumulative=high=dd=0.
        for v in vals:
            cumulative+=v;high=max(high,cumulative);dd=min(dd,cumulative-high)
        shuffled_dd.append(-dd)
    benchmarks=[]
    for horizon in [1,3,5,10,20]:
        paired=[t for t in trades if t[f"quote_return_{horizon}m_pct"] is not None]
        alternatives=[dict(net_base_estimate=t["entry_notional"]*t[f"quote_return_{horizon}m_pct"]/100-t["cost_base_estimate"]) for t in paired]
        benchmarks.append(dict(horizon_minutes=horizon, **stats(alternatives),
                               current_same_subset_total=sum(t["net_base_estimate"] for t in paired),
                               basis="LAST_LOGGED_QUOTE_AT_OR_BEFORE_HORIZON_MAX_AGE_60S",
                               execution_backtest=False))
    gross_stats=stats(trades,"gross_order_price_estimate")
    net_stats=stats(trades)
    summary=dict(
        disclaimer="QUOTED ORDER ESTIMATES ONLY; actual sell fill prices/fees/taxes unavailable. Not a validated strategy backtest.",
        matched_round_trips=len(trades), actual_trading_days=len(daily), order_rows=sum(d['order_rows'] for d in day_quality),
        unique_symbols=len(concentration), entry_signal_joins=sum(t['signal_join_status']=='MATCHED' for t in trades),
        lifecycle_buy_terminal_joins=sum(t['buy_full_observed_at'] is not None for t in trades),
        lifecycle_sell_terminal_joins=sum(t['sell_full_observed_at'] is not None for t in trades),
        trades_with_actual_exit_fill_price=0, trades_with_actual_costs=0,
        gross=gross_stats, base=net_stats, stress=stats(trades,'net_stress_estimate'),
        total_entry_notional=sum(t['entry_notional'] for t in trades),
        cost_base_estimate=sum(t['cost_base_estimate'] for t in trades),
        avg_cost_base_estimate=st.mean(t['cost_base_estimate'] for t in trades),
        cost_share_of_positive_gross_gains=sum(t['cost_base_estimate'] for t in trades)/sum(t['gross_order_price_estimate'] for t in trades if t['gross_order_price_estimate']>0),
        gross_entry_avg_exit_quote_estimate=sum(t['gross_entry_avg_exit_quote_estimate'] for t in trades),
        entry_quote_vs_average_difference=sum(t['entry_quote_vs_average_difference'] for t in trades),
        sign_changed_using_entry_average=sum((t['gross_order_price_estimate']>0)!=(t['gross_entry_avg_exit_quote_estimate']>0) for t in trades),
        max_closed_trade_drawdown_estimate=-drawdown, max_drawdown_percent=None,
        max_drawdown_start_index=max_dd_start, max_drawdown_end_index=max_dd_end,
        maximum_consecutive_net_losses=longest_streak,
        maximum_consecutive_gross_losses=max((sum(1 for _ in run) for loss,run in __import__('itertools').groupby(t['gross_order_price_estimate']<0 for t in trades) if loss), default=0),
        recovered_episodes=recovery_episodes, unfinished_recovery=active_recovery is not None,
        trades_since_last_peak=len(trades)-1-active_recovery if active_recovery is not None else 0,
        recovery_elapsed_calendar_days=(stamp(trades[-1]['exit_order_at'])-stamp(trades[max(active_recovery,0)]['exit_order_at'])).total_seconds()/86400 if active_recovery is not None else None,
        daily_pnl_sample_sd=st.stdev(d['total'] for d in daily), weekly_pnl_sample_sd=st.stdev(w['total'] for w in weekly),
        weekly_observation_count=len(weekly), avg_trades_per_day=len(trades)/len(daily),
        partial_quantity_observed_trades=sum(t['partial_quantity_observed'] for t in trades),
        timeout_affected_trades=sum(t['timeout_count']>0 for t in trades),
        reentry_trades=sum(t['reentry_index']>1 for t in trades),
        cluster_bootstrap_95pct_mean_interval=[quantile(bootstrap,.025),quantile(bootstrap,.975)],
        cluster_bootstrap_fraction_positive=sum(v>0 for v in bootstrap)/len(bootstrap),
        cluster_bootstrap_note="8 resampled trading days; quote/cost assumptions and changing strategy invalidate confirmatory inference.",
        trade_order_permutation_drawdown_95pct=quantile(shuffled_dd,.95),
        mean_hold_net_winners=st.mean(t['reported_hold_minutes'] for t in trades if t['net_base_estimate']>0),
        mean_hold_net_losers=st.mean(t['reported_hold_minutes'] for t in trades if t['net_base_estimate']<0),
        unresolved_causal_attributions=len(trades),
        candidate_a_block_events=len(candidate_blocks),
        candidate_a_unique_blocked_symbols=len({r['symbol'] for r in candidate_blocks}),
        candidate_a_blocked_symbol_days=len({(r['date'],r['symbol']) for r in candidate_blocks}),
        candidate_a_block_days=len({r['date'] for r in candidate_blocks}),
        candidate_a_clean_block_days=sum(d['actual_candidate_a_block_events']>0 and d['collection_status']=='COMPLETE' for d in day_quality),
    )
    write_csv(output_dir/'trades_estimates.csv',trades)
    write_csv(output_dir/'daily_estimates.csv',daily)
    write_csv(output_dir/'weekly_estimates.csv',weekly)
    write_csv(output_dir/'condition_groups.csv',groups)
    write_csv(output_dir/'symbol_concentration.csv',concentration)
    write_csv(output_dir/'robustness_estimates.csv',robustness)
    write_csv(output_dir/'extension_quote_comparisons.csv',shadow_checks)
    write_csv(output_dir/'data_quality.csv',day_quality)
    write_csv(output_dir/'source_manifest.csv',provenance)
    write_csv(output_dir/'candidate_a_block_events.csv',candidate_blocks)
    write_csv(output_dir/'fixed_horizon_quote_comparisons.csv',benchmarks)
    (output_dir/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    analyze(args.input,args.output)
