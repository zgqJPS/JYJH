"""
backtester.py - 策略回测引擎
用67天历史数据验证5个高价值信号的准确性
同时支持自定义策略回测
统一使用 xgt_limit_up_detail 和 smash_coefficients 表
"""

import sqlite3
import json
from datetime import datetime, timedelta
from collections import defaultdict

from config import DB_PATH


class DataStore:
    """数据库加载器，一次性加载所有需要的数据到内存"""
    
    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._load_all()
    
    def _load_all(self):
        """加载所有数据到内存（使用 xgt 表）"""
        rows = self.conn.execute("""
            SELECT date, code, name, limit_up_days as continuous_boards, 
                   seal_ratio as seal_amount, break_times as seal_style, 
                   turnover_rate, price as latest_price, change_percent
            FROM xgt_limit_up_detail
            WHERE date >= '2026-01-21' AND date <= '2026-05-11'
            ORDER BY date, limit_up_days DESC
        """).fetchall()
        self.daily_stocks = defaultdict(list)
        self.stock_daily = defaultdict(dict)
        for r in rows:
            d = dict(r)
            self.daily_stocks[d['date']].append(d)
            self.stock_daily[(d['code'], d['date'])] = d
        
        rows = self.conn.execute("""
            SELECT trade_date as date, smash_coefficient, 
                   max_continuous_days as max_continuous_boards
            FROM smash_coefficients
            WHERE date >= '2026-01-21' AND date <= '2026-05-11'
            ORDER BY date
        """).fetchall()
        self.daily_smash = {}
        for r in rows:
            d = dict(r)
            self.daily_smash[d['date']] = d
        
        rows = self.conn.execute("""
            SELECT date, concept, count
            FROM concept_statistics
            WHERE date >= '2026-01-21' AND date <= '2026-05-11'
            ORDER BY date, count DESC
        """).fetchall()
        self.daily_concepts = defaultdict(list)
        for r in rows:
            d = dict(r)
            self.daily_concepts[d['date']].append(d)
        
        self.trading_days = sorted(set(
            list(self.daily_stocks.keys()) + list(self.daily_smash.keys())
        ))
        self.trading_days_with_stocks = sorted(self.daily_stocks.keys())
        self.conn.close()
    
    def get_date_index(self, date_str):
        try:
            return self.trading_days_with_stocks.index(date_str)
        except ValueError:
            return -1
    
    def get_prev_date(self, date_str):
        idx = self.get_date_index(date_str)
        if idx > 0:
            return self.trading_days_with_stocks[idx - 1]
        return None
    
    def get_next_date(self, date_str):
        idx = self.get_date_index(date_str)
        if idx >= 0 and idx < len(self.trading_days_with_stocks) - 1:
            return self.trading_days_with_stocks[idx + 1]
        return None
    
    def get_daily_stats(self, date_str):
        stocks = self.daily_stocks.get(date_str, [])
        smash = self.daily_smash.get(date_str, {})
        concepts = self.daily_concepts.get(date_str, [])
        total = len(stocks)
        max_boards = max((s.get('continuous_boards', 1) or 1 for s in stocks), default=0)
        board_dist = defaultdict(int)
        for s in stocks:
            b = s.get('continuous_boards', 1) or 1
            board_dist[b] += 1
        return {
            'date': date_str,
            'total_limit_up': total,
            'max_boards': max_boards,
            'smash_coefficient': smash.get('smash_coefficient', 0),
            'max_continuous_boards': smash.get('max_continuous_boards', 0),
            'board_dist': dict(board_dist),
            'top_concepts': concepts[:10],
        }


class SignalChecker:
    SIGNALS = {
        1: {
            'name': '5→6突破+砸盘下降',
            'stars': 3,
            'description': '最高板从5突破到6且砸盘系数下降，次日涨停数大涨'
        },
        2: {
            'name': '砸盘骤降>3+连板≤3',
            'stars': 3,
            'description': '砸盘系数骤降超3且最高连板≤3，见底反弹信号'
        },
        3: {
            'name': '连续2天砸盘<3+连板≤3',
            'stars': 2,
            'description': '连续2天低砸盘+低连板，底部确认信号'
        },
        4: {
            'name': '7板+砸盘>6',
            'stars': 2,
            'description': '最高板达7且砸盘>6，见顶崩塌信号'
        },
        5: {
            'name': '4板+涨停数<35+砸盘<3',
            'stars': 1,
            'description': '最高板仅4、涨停数少、砸盘低，假突破预警'
        },
    }
    
    def __init__(self, data: DataStore):
        self.data = data
    
    def check_signal_1(self, date_str):
        prev_date = self.data.get_prev_date(date_str)
        if not prev_date:
            return None
        prev_stats = self.data.get_daily_stats(prev_date)
        curr_stats = self.data.get_daily_stats(date_str)
        if prev_stats['max_boards'] == 5 and curr_stats['max_boards'] >= 6:
            sc_curr = curr_stats['smash_coefficient']
            sc_prev = prev_stats['smash_coefficient']
            if sc_curr < sc_prev:
                return {
                    'triggered': True,
                    'details': {
                        'prev_max_boards': prev_stats['max_boards'],
                        'curr_max_boards': curr_stats['max_boards'],
                        'sc_prev': sc_prev,
                        'sc_curr': sc_curr,
                        'sc_change': round(sc_prev - sc_curr, 2),
                    }
                }
        return {'triggered': False, 'details': {}}
    
    def check_signal_2(self, date_str):
        prev_date = self.data.get_prev_date(date_str)
        if not prev_date:
            return None
        prev_stats = self.data.get_daily_stats(prev_date)
        curr_stats = self.data.get_daily_stats(date_str)
        sc_drop = prev_stats['smash_coefficient'] - curr_stats['smash_coefficient']
        if sc_drop > 3 and curr_stats['max_boards'] <= 3:
            return {
                'triggered': True,
                'details': {
                    'sc_prev': prev_stats['smash_coefficient'],
                    'sc_curr': curr_stats['smash_coefficient'],
                    'sc_drop': round(sc_drop, 2),
                    'max_boards': curr_stats['max_boards'],
                }
            }
        return {'triggered': False, 'details': {}}
    
    def check_signal_3(self, date_str):
        prev_date = self.data.get_prev_date(date_str)
        if not prev_date:
            return None
        prev_stats = self.data.get_daily_stats(prev_date)
        curr_stats = self.data.get_daily_stats(date_str)
        if (curr_stats['smash_coefficient'] < 3 and prev_stats['smash_coefficient'] < 3
                and curr_stats['max_boards'] <= 3 and prev_stats['max_boards'] <= 3):
            return {
                'triggered': True,
                'details': {
                    'sc_today': curr_stats['smash_coefficient'],
                    'sc_prev': prev_stats['smash_coefficient'],
                    'max_boards_today': curr_stats['max_boards'],
                    'max_boards_prev': prev_stats['max_boards'],
                }
            }
        return {'triggered': False, 'details': {}}
    
    def check_signal_4(self, date_str):
        curr_stats = self.data.get_daily_stats(date_str)
        if curr_stats['max_boards'] >= 7 and curr_stats['smash_coefficient'] > 6:
            return {
                'triggered': True,
                'details': {
                    'max_boards': curr_stats['max_boards'],
                    'sc': curr_stats['smash_coefficient'],
                }
            }
        return {'triggered': False, 'details': {}}
    
    def check_signal_5(self, date_str):
        curr_stats = self.data.get_daily_stats(date_str)
        if (curr_stats['max_boards'] == 4 
                and curr_stats['total_limit_up'] < 35 
                and curr_stats['smash_coefficient'] < 3):
            return {
                'triggered': True,
                'details': {
                    'max_boards': curr_stats['max_boards'],
                    'total_limit_up': curr_stats['total_limit_up'],
                    'sc': curr_stats['smash_coefficient'],
                }
            }
        return {'triggered': False, 'details': {}}
    
    def check_all_signals(self, date_str):
        results = {}
        for sig_id in range(1, 6):
            method = getattr(self, f'check_signal_{sig_id}')
            results[sig_id] = method(date_str)
        return results


class Backtester:
    SIGNAL_DIRECTION = {
        1: 'bullish',
        2: 'bullish',
        3: 'bullish',
        4: 'bearish',
        5: 'bearish',
    }
    
    def __init__(self, data: DataStore = None):
        self.data = data or DataStore()
        self.checker = SignalChecker(self.data)
    
    def _evaluate_next_day(self, trigger_date):
        next_date = self.data.get_next_date(trigger_date)
        if not next_date:
            return None
        curr_stats = self.data.get_daily_stats(trigger_date)
        next_stats = self.data.get_daily_stats(next_date)
        limit_up_change = next_stats['total_limit_up'] - curr_stats['total_limit_up']
        boards_change = next_stats['max_boards'] - curr_stats['max_boards']
        return {
            'next_date': next_date,
            'next_total_limit_up': next_stats['total_limit_up'],
            'next_max_boards': next_stats['max_boards'],
            'next_sc': next_stats['smash_coefficient'],
            'limit_up_change': limit_up_change,
            'boards_change': boards_change,
        }
    
    def _is_signal_correct(self, signal_id, trigger_date, next_day_result):
        if next_day_result is None:
            return None
        direction = self.SIGNAL_DIRECTION[signal_id]
        lu_change = next_day_result['limit_up_change']
        boards_change = next_day_result['boards_change']
        if direction == 'bullish':
            return lu_change > 0 or boards_change > 0
        else:
            return lu_change < 0 or boards_change < 0
    
    def run_backtest(self, start_date=None, end_date=None):
        days = self.data.trading_days_with_stocks
        if start_date:
            days = [d for d in days if d >= start_date]
        if end_date:
            days = [d for d in days if d <= end_date]
        if days and days[-1] == self.data.trading_days_with_stocks[-1]:
            days = days[:-1]
        signal_results = {i: {
            'name': SignalChecker.SIGNALS[i]['name'],
            'stars': SignalChecker.SIGNALS[i]['stars'],
            'triggers': [],
            'correct': 0,
            'incorrect': 0,
            'unverified': 0,
            'total_limit_up_changes': [],
        } for i in range(1, 6)}
        for date_str in days:
            signal_checks = self.checker.check_all_signals(date_str)
            next_day = self._evaluate_next_day(date_str)
            for sig_id, check_result in signal_checks.items():
                if check_result is None:
                    continue
                if not check_result['triggered']:
                    continue
                trigger_record = {
                    'date': date_str,
                    'details': check_result['details'],
                    'next_day': next_day,
                }
                if next_day:
                    is_correct = self._is_signal_correct(sig_id, date_str, next_day)
                    trigger_record['correct'] = is_correct
                    if is_correct is True:
                        signal_results[sig_id]['correct'] += 1
                    elif is_correct is False:
                        signal_results[sig_id]['incorrect'] += 1
                    else:
                        signal_results[sig_id]['unverified'] += 1
                    signal_results[sig_id]['total_limit_up_changes'].append(
                        next_day['limit_up_change']
                    )
                else:
                    trigger_record['correct'] = None
                    signal_results[sig_id]['unverified'] += 1
                signal_results[sig_id]['triggers'].append(trigger_record)
        report = {
            'backtest_period': f"{days[0]} ~ {days[-1]}",
            'total_trading_days': len(days),
            'signals': {},
            'synergy_analysis': {},
        }
        for sig_id in range(1, 6):
            sr = signal_results[sig_id]
            total = sr['correct'] + sr['incorrect']
            hit_rate = (sr['correct'] / total * 100) if total > 0 else 0
            avg_change = (sum(sr['total_limit_up_changes']) / len(sr['total_limit_up_changes'])
                         ) if sr['total_limit_up_changes'] else 0
            max_drawdown = min(sr['total_limit_up_changes']) if sr['total_limit_up_changes'] else 0
            max_gain = max(sr['total_limit_up_changes']) if sr['total_limit_up_changes'] else 0
            report['signals'][sig_id] = {
                'name': sr['name'],
                'stars': sr['stars'],
                'total_triggers': len(sr['triggers']),
                'correct': sr['correct'],
                'incorrect': sr['incorrect'],
                'unverified': sr['unverified'],
                'hit_rate': round(hit_rate, 1),
                'avg_limit_up_change': round(avg_change, 1),
                'max_gain': max_gain,
                'max_drawdown': max_drawdown,
                'trigger_dates': [t['date'] for t in sr['triggers']],
            }
        all_triggers_by_date = defaultdict(list)
        for sig_id in range(1, 6):
            for t in signal_results[sig_id]['triggers']:
                all_triggers_by_date[t['date']].append(sig_id)
        multi_signal_dates = {d: sigs for d, sigs in all_triggers_by_date.items() if len(sigs) > 1}
        report['synergy_analysis'] = {
            'multi_signal_dates_count': len(multi_signal_dates),
            'details': {d: {
                'signals': sigs,
                'signal_names': [SignalChecker.SIGNALS[s]['name'] for s in sigs],
            } for d, sigs in multi_signal_dates.items()},
        }
        bullish_changes = []
        for sig_id in range(1, 6):
            if self.SIGNAL_DIRECTION[sig_id] == 'bullish':
                bullish_changes.extend(signal_results[sig_id]['total_limit_up_changes'])
        if bullish_changes:
            report['overall_bullish_strategy'] = {
                'total_operations': len(bullish_changes),
                'avg_limit_up_change': round(sum(bullish_changes) / len(bullish_changes), 1),
                'win_days': sum(1 for c in bullish_changes if c > 0),
                'lose_days': sum(1 for c in bullish_changes if c <= 0),
                'win_rate': round(sum(1 for c in bullish_changes if c > 0) / len(bullish_changes) * 100, 1),
            }
        return report
    
    def run_backtest_single_signal(self, signal_id):
        if signal_id not in range(1, 6):
            raise ValueError(f"signal_id must be 1~5, got {signal_id}")
        days = self.data.trading_days_with_stocks[:-1]
        method = getattr(self.checker, f'check_signal_{signal_id}')
        triggers = []
        correct = 0
        incorrect = 0
        for date_str in days:
            result = method(date_str)
            if result is None or not result['triggered']:
                continue
            next_day = self._evaluate_next_day(date_str)
            is_correct = self._is_signal_correct(signal_id, date_str, next_day) if next_day else None
            record = {
                'trigger_date': date_str,
                'details': result['details'],
                'next_day': next_day,
                'correct': is_correct,
            }
            triggers.append(record)
            if is_correct is True:
                correct += 1
            elif is_correct is False:
                incorrect += 1
        total = correct + incorrect
        return {
            'signal_id': signal_id,
            'signal_name': SignalChecker.SIGNALS[signal_id]['name'],
            'stars': SignalChecker.SIGNALS[signal_id]['stars'],
            'description': SignalChecker.SIGNALS[signal_id]['description'],
            'total_triggers': len(triggers),
            'correct': correct,
            'incorrect': incorrect,
            'hit_rate': round(correct / total * 100, 1) if total > 0 else 0,
            'triggers': triggers,
        }
    
    def save_to_db(self, report):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                signal_name TEXT,
                trigger_date TEXT,
                details TEXT,
                next_date TEXT,
                next_total_limit_up INTEGER,
                next_max_boards INTEGER,
                limit_up_change INTEGER,
                boards_change INTEGER,
                correct INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for sig_id in range(1, 6):
            single = self.run_backtest_single_signal(sig_id)
            for t in single['triggers']:
                nd = t.get('next_day') or {}
                conn.execute("""
                    INSERT INTO backtest_results 
                    (signal_id, signal_name, trigger_date, details, next_date,
                     next_total_limit_up, next_max_boards, limit_up_change, boards_change, correct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sig_id,
                    single['signal_name'],
                    t['trigger_date'],
                    json.dumps(t['details'], ensure_ascii=False),
                    nd.get('next_date'),
                    nd.get('next_total_limit_up'),
                    nd.get('next_max_boards'),
                    nd.get('limit_up_change'),
                    nd.get('boards_change'),
                    1 if t['correct'] else (0 if t['correct'] is False else None),
                ))
        conn.commit()
        conn.close()
        print("回测结果已保存到 backtest_results 表")


def format_report(report):
    lines = []
    lines.append("=" * 70)
    lines.append("📊 策略回测报告")
    lines.append("=" * 70)
    lines.append(f"回测区间: {report['backtest_period']}")
    lines.append(f"交易天数: {report['total_trading_days']}")
    lines.append("")
    for sig_id in range(1, 6):
        s = report['signals'][sig_id]
        stars = '⭐' * s['stars']
        lines.append(f"{'─' * 60}")
        lines.append(f"信号{sig_id} {stars}: {s['name']}")
        lines.append(f"  触发次数: {s['total_triggers']}")
        lines.append(f"  正确/错误: {s['correct']}/{s['incorrect']} (未验证: {s['unverified']})")
        lines.append(f"  命中率: {s['hit_rate']}%")
        lines.append(f"  平均涨停数变化: {s['avg_limit_up_change']}")
        lines.append(f"  最大单日增益: +{s['max_gain']}")
        lines.append(f"  最大单日回撤: {s['max_drawdown']}")
        if s['trigger_dates']:
            lines.append(f"  触发日期: {', '.join(s['trigger_dates'])}")
        lines.append("")
    if 'overall_bullish_strategy' in report:
        bs = report['overall_bullish_strategy']
        lines.append(f"{'─' * 60}")
        lines.append("📈 看多策略总体表现:")
        lines.append(f"  操作次数: {bs['total_operations']}")
        lines.append(f"  平均涨停数变化: {bs['avg_limit_up_change']}")
        lines.append(f"  胜率: {bs['win_rate']}% ({bs['win_days']}胜/{bs['lose_days']}负)")
    sa = report.get('synergy_analysis', {})
    if sa.get('multi_signal_dates_count', 0) > 0:
        lines.append("")
        lines.append(f"{'─' * 60}")
        lines.append(f"🔗 多信号协同 (共{sa['multi_signal_dates_count']}天多信号同时触发):")
        for d, info in list(sa.get('details', {}).items())[:5]:
            lines.append(f"  {d}: {', '.join(info['signal_names'])}")
    lines.append("")
    lines.append("=" * 70)
    return '\n'.join(lines)


if __name__ == '__main__':
    print("正在加载数据...")
    data = DataStore()
    print(f"加载完成: {len(data.trading_days_with_stocks)} 个交易日")
    bt = Backtester(data)
    print("\n正在执行全量回测...")
    report = bt.run_backtest()
    bt.save_to_db(report)
    print(format_report(report))
    print("\n\n" + "=" * 70)
    print("📋 各信号详细回测")
    print("=" * 70)
    for sig_id in range(1, 6):
        result = bt.run_backtest_single_signal(sig_id)
        print(f"\n信号{sig_id}: {result['signal_name']}")
        print(f"  命中率: {result['hit_rate']}% ({result['correct']}/{result['total_triggers']})")
        for t in result['triggers']:
            nd = t.get('next_day') or {}
            correct_mark = '✅' if t['correct'] else ('❌' if t['correct'] is False else '⏳')
            print(f"  {correct_mark} {t['trigger_date']}: "
                  f"次日涨停数={nd.get('next_total_limit_up', 'N/A')}, "
                  f"变化={nd.get('limit_up_change', 'N/A')}")