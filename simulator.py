"""
simulator.py - 模拟交易引擎（增强版）
增加数据库降级价格获取，交易记录显示股票名称
"""

import sqlite3
import logging
import json
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
import pandas as pd
import akshare as ak

logger = logging.getLogger(__name__)

DB_PATH = "stock_data_1784791326780_0_09ym.db"
_price_cache = {}

def get_stock_price(code: str, date: str, price_type: str = 'open') -> Optional[float]:
    """
    获取股票价格，优先 akshare，失败则从数据库 latest_price 降级
    """
    cache_key = f"{code}_{date}_{price_type}"
    if cache_key in _price_cache:
        return _price_cache[cache_key]

    # 1. 尝试 akshare（带交易所后缀）
    try:
        if code.startswith(('60', '68')):
            symbol = f"{code}.SH"
        elif code.startswith(('00', '30')):
            symbol = f"{code}.SZ"
        else:
            symbol = code
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=date, end_date=date, adjust="qfq")
        if df is not None and not df.empty:
            row = df.iloc[0]
            price = row['开盘'] if price_type == 'open' else row['收盘']
            _price_cache[cache_key] = float(price)
            return float(price)
    except Exception as e:
        logger.warning(f"akshare获取 {code} {date} 价格失败: {e}")

    # 2. 降级：从数据库 akshare_limit_up 表获取 latest_price（涨停价）
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            "SELECT latest_price FROM akshare_limit_up WHERE code = ? AND date = ?",
            (code, date)
        )
        row = cursor.fetchone()
        conn.close()
        if row and row[0] is not None:
            price = float(row[0])
            _price_cache[cache_key] = price
            logger.info(f"[降级] 使用数据库 {code} {date} latest_price = {price}")
            return price
    except Exception as e:
        logger.warning(f"数据库获取 {code} {date} 价格失败: {e}")

    _price_cache[cache_key] = None
    return None

class Simulator:
    """模拟交易引擎（增强版）"""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        init_cash: float = 1000000,
        grade_filter: List[str] = ['S', 'A'],
        take_profit: float = 0.20,
        stop_loss: float = 0.07,
        max_positions: int = 5,
        position_pct: float = 0.2,
        commission: float = 0.001,
        slippage: float = 0.001
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.init_cash = init_cash
        self.grade_filter = grade_filter
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.max_positions = max_positions
        self.position_pct = position_pct
        self.commission = commission
        self.slippage = slippage

        self.cash = init_cash
        self.positions = {}  # {code: {'shares': int, 'buy_price': float, 'buy_date': str, 'buy_cost': float, 'name': str}}
        self.net_values = []
        self.trades = []
        self.dates = []

    def _get_trading_dates(self) -> List[str]:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute("""
            SELECT DISTINCT date FROM akshare_limit_up
            WHERE date >= ? AND date <= ?
            ORDER BY date
        """, (self.start_date, self.end_date))
        dates = [row[0] for row in cursor.fetchall()]
        conn.close()
        logger.info(f"[回测] 获取交易日: {len(dates)} 个")
        return dates

    def _get_recommendations(self, date: str) -> List[Dict]:
        try:
            import smart_recommender as sr
            recs = sr.generate_recommendations(date, top_n=20, db_path=DB_PATH)
            logger.info(f"[回测] {date} 原始推荐 {len(recs)} 只")
            for r in recs:
                if 'grade' not in r:
                    if 'confidence_level' in r:
                        r['grade'] = r['confidence_level']
                    else:
                        r['grade'] = 'B'
                if 'score' not in r and 'total_score' in r:
                    r['score'] = r['total_score']
                elif 'score' not in r:
                    r['score'] = 0
                # 确保 name 字段存在
                if 'name' not in r or not r['name']:
                    r['name'] = r.get('code', '')
            return recs
        except Exception as e:
            logger.error(f"[回测] 获取推荐失败: {e}")
            return []

    def _get_exit_signals(self, date: str, codes: List[str]) -> Dict[str, Dict]:
        signals = {}
        if not codes:
            return signals
        try:
            import exit_strategy as es
            for code in codes:
                signal = es.check_stock_exit_signals(code, holding_days=0, db_path=DB_PATH)
                if signal.get('exit_recommended'):
                    signals[code] = signal
        except Exception as e:
            logger.warning(f"[回测] 获取出场信号失败: {e}")
        return signals

    def _get_price(self, code: str, date: str, price_type: str = 'open') -> Optional[float]:
        return get_stock_price(code, date, price_type)

    def _buy(self, code: str, name: str, date: str, price: float, shares: int, reason: str):
        cost = price * shares * (1 + self.commission + self.slippage)
        if cost > self.cash:
            shares = int(self.cash / (price * (1 + self.commission + self.slippage)))
            if shares <= 0:
                logger.warning(f"[回测] 资金不足，无法买入 {name}({code})")
                return
            cost = price * shares * (1 + self.commission + self.slippage)
        self.cash -= cost
        self.positions[code] = {
            'shares': shares,
            'buy_price': price,
            'buy_date': date,
            'buy_cost': cost,
            'name': name
        }
        self.trades.append({
            'date': date,
            'code': code,
            'name': name,
            'action': 'BUY',
            'price': price,
            'shares': shares,
            'cost': cost,
            'reason': reason
        })
        logger.info(f"[回测] 买入 {name}({code}) {shares}股 @ {price:.2f}，成本 {cost:.2f}，剩余现金 {self.cash:.2f}")

    def _sell(self, code: str, date: str, price: float, reason: str):
        if code not in self.positions:
            return
        pos = self.positions.pop(code)
        name = pos['name']
        revenue = price * pos['shares'] * (1 - self.commission - self.slippage)
        self.cash += revenue
        profit = revenue - pos['buy_cost']
        self.trades.append({
            'date': date,
            'code': code,
            'name': name,
            'action': 'SELL',
            'price': price,
            'shares': pos['shares'],
            'revenue': revenue,
            'profit': profit,
            'reason': reason
        })
        logger.info(f"[回测] 卖出 {name}({code}) {pos['shares']}股 @ {price:.2f}，盈亏 {profit:.2f}")

    def _calc_position_size(self, grade: str) -> float:
        if grade == 'S':
            return min(self.position_pct * 1.5, 0.4)
        elif grade == 'A':
            return min(self.position_pct * 1.2, 0.3)
        elif grade == 'B':
            return self.position_pct * 0.8
        else:
            return self.position_pct * 0.5

    def run(self) -> Dict[str, Any]:
        self.dates = self._get_trading_dates()
        if not self.dates:
            return {"error": "无交易日数据"}

        self.net_values.append((self.dates[0], self.init_cash))

        for idx, date in enumerate(self.dates):
            logger.info(f"[回测] 处理日期 {date} ({idx+1}/{len(self.dates)})")

            # 处理卖出
            holding_codes = list(self.positions.keys())
            if holding_codes:
                exit_signals = self._get_exit_signals(date, holding_codes)
                for code in list(self.positions.keys()):
                    signal = exit_signals.get(code, {})
                    exit_urgency = signal.get('exit_urgency', 'NONE')
                    should_sell = False
                    reason = ''

                    pos = self.positions[code]
                    current_price = self._get_price(code, date, 'open')
                    if current_price is None:
                        current_price = self._get_price(code, date, 'close')
                    if current_price is None:
                        logger.warning(f"[回测] {date} 无法获取 {pos['name']}({code}) 价格，跳过卖出")
                        continue
                    profit_pct = (current_price - pos['buy_price']) / pos['buy_price']
                    if profit_pct >= self.take_profit:
                        should_sell = True
                        reason = f'止盈 ({profit_pct:.1%})'
                    elif profit_pct <= -self.stop_loss:
                        should_sell = True
                        reason = f'止损 ({profit_pct:.1%})'
                    if not should_sell and exit_urgency in ['CRITICAL', 'HIGH']:
                        should_sell = True
                        reason = f"出场信号({exit_urgency})"
                    if should_sell:
                        self._sell(code, date, current_price, reason)

            # 处理买入
            recs = self._get_recommendations(date)
            if not recs:
                logger.warning(f"[回测] {date} 无推荐")
                self._record_net_value(date)
                continue

            recs = [r for r in recs if r.get('grade') in self.grade_filter]
            logger.info(f"[回测] {date} 过滤后推荐 {len(recs)} 只")
            recs.sort(key=lambda x: x.get('score', 0), reverse=True)

            buy_count = 0
            for rec in recs[:self.max_positions]:
                code = rec['code']
                name = rec.get('name', code)
                if code in self.positions:
                    continue
                price = self._get_price(code, date, 'open')
                if price is None:
                    price = self._get_price(code, date, 'close')
                if price is None:
                    logger.warning(f"[回测] {date} 无法获取 {name}({code}) 任何价格，跳过")
                    continue
                grade = rec.get('grade', 'C')
                pct = self._calc_position_size(grade)
                max_shares = int((self.cash * pct) / (price * (1 + self.commission + self.slippage)))
                if max_shares <= 0:
                    logger.warning(f"[回测] {name}({code}) 可买股数为0，跳过")
                    continue
                self._buy(code, name, date, price, max_shares, f"推荐({grade}级)")
                buy_count += 1
                if buy_count >= self.max_positions:
                    break

            self._record_net_value(date)

        # 最终清仓
        if self.positions:
            last_date = self.dates[-1]
            for code in list(self.positions.keys()):
                price = self._get_price(code, last_date, 'close')
                if price is None:
                    price = self.positions[code]['buy_price']
                self._sell(code, last_date, price, '回测结束清仓')

        return self._generate_report()

    def _record_net_value(self, date: str):
        total_value = self.cash
        for code, pos in self.positions.items():
            price = self._get_price(code, date, 'close')
            if price is None:
                price = pos['buy_price']
            total_value += price * pos['shares']
        self.net_values.append((date, total_value))
        logger.info(f"[回测] {date} 总资产 {total_value:.2f}")

    def _generate_report(self) -> Dict[str, Any]:
        if len(self.net_values) < 2:
            return {"error": "数据不足"}

        # 将 trades 中的 code 替换为名称（前端显示用），同时保留原始代码
        trades_for_display = []
        for t in self.trades:
            t_copy = t.copy()
            t_copy['stock_code'] = t_copy['code']   # 保存原始代码
            t_copy['code'] = t_copy['name']         # 用名称覆盖 code，前端将显示名称
            trades_for_display.append(t_copy)

        dates = [nv[0] for nv in self.net_values]
        values = [nv[1] for nv in self.net_values]
        initial = values[0]
        final = values[-1]
        total_return = (final - initial) / initial
        days = (datetime.strptime(dates[-1], "%Y-%m-%d") - datetime.strptime(dates[0], "%Y-%m-%d")).days
        years = max(days / 365.25, 0.01)
        annual_return = (1 + total_return) ** (1 / years) - 1

        peak = values[0]
        max_drawdown = 0
        for v in values:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak != 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd

        buy_trades = [t for t in self.trades if t['action'] == 'BUY']
        sell_trades = [t for t in self.trades if t['action'] == 'SELL']
        total_trades = len(sell_trades)
        win_trades = [t for t in sell_trades if t.get('profit', 0) > 0]
        win_count = len(win_trades)
        win_rate = win_count / total_trades if total_trades > 0 else 0
        avg_profit = sum(t.get('profit', 0) for t in sell_trades) / total_trades if total_trades > 0 else 0
        avg_win = sum(t.get('profit', 0) for t in win_trades) / win_count if win_count > 0 else 0
        avg_loss = sum(t.get('profit', 0) for t in sell_trades if t.get('profit', 0) < 0) / (total_trades - win_count) if total_trades - win_count > 0 else 0
        profit_loss_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else 0

        returns = [(values[i] - values[i-1]) / values[i-1] for i in range(1, len(values))]
        avg_return = sum(returns) / len(returns)
        std_return = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5 if returns else 0
        sharpe = avg_return / std_return * (252 ** 0.5) if std_return > 0 else 0

        return {
            'total_return': total_return,
            'annual_return': annual_return,
            'max_drawdown': max_drawdown,
            'win_rate': win_rate,
            'profit_loss_ratio': profit_loss_ratio,
            'sharpe_ratio': sharpe,
            'total_trades': total_trades,
            'total_buys': len(buy_trades),
            'total_sells': len(sell_trades),
            'net_values': [{'date': d, 'value': v} for d, v in self.net_values],
            'trades': trades_for_display,   # 使用替换后的列表
            'start_date': self.start_date,
            'end_date': self.end_date,
            'init_cash': self.init_cash,
            'final_value': final,
            'grade_filter': self.grade_filter,
            'take_profit': self.take_profit,
            'stop_loss': self.stop_loss,
            'max_positions': self.max_positions,
            'position_pct': self.position_pct
        }