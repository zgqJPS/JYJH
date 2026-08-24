"""
board_calculator.py - 真实连板数计算引擎

核心发现：xgt_limit_up_detail.limit_up_days 字段存在严重数据质量问题：
1. 2026年1-6月数据为填充/重复数据（74只股票每个交易日都"涨停"，first_limit_up_time固定不变）
2. 7-8月真实数据中，API返回的limit_up_days与实际连续涨停天数有14.6%不匹配
3. 该字段可能表示"近期涨停次数窗口"而非"连续涨停天数"（如001267: 8/4=1板→8/5=4板，不可能连续）

本模块通过遍历个股在xgt_limit_up_detail中的出现记录，结合交易日历，
计算真实的连续涨停天数。
"""

import sqlite3
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# ★ 关键修复：从 config 导入正确的 DB_PATH ★
from config import DB_PATH

logger = logging.getLogger(__name__)

# 数据质量分界线：此日期之前的数据为填充/重复数据，不可信
VALID_DATA_START = '2026-07-01'


class BoardCalculator:
    """真实连板数计算器"""

    def __init__(self, conn: sqlite3.Connection = None):
        self._conn = conn
        self._trading_days: Optional[List[str]] = None
        self._day_idx: Optional[Dict[str, int]] = None
        self._consec_map: Optional[Dict[Tuple[str, str], int]] = None
        self._daily_max: Optional[Dict[str, int]] = None
        self._daily_board_dist: Optional[Dict[str, Dict[int, int]]] = None
        self._loaded = False

    def _get_conn(self, conn=None):
        """获取数据库连接"""
        c = conn or self._conn
        if c is None:
            c = sqlite3.connect(DB_PATH)
        return c

    def _ensure_loaded(self, conn=None):
        """延迟加载所有交易日和连板映射"""
        if self._loaded:
            return

        c = self._get_conn(conn)
        rows = c.execute(
            "SELECT DISTINCT date FROM xgt_limit_up_detail "
            "WHERE date >= ? ORDER BY date",
            (VALID_DATA_START,)
        ).fetchall()
        self._trading_days = [r[0] for r in rows]
        self._day_idx = {d: i for i, d in enumerate(self._trading_days)}

        all_rows = c.execute(
            "SELECT date, code FROM xgt_limit_up_detail "
            "WHERE date >= ? ORDER BY code, date",
            (VALID_DATA_START,)
        ).fetchall()

        stock_dates = defaultdict(list)
        for date, code in all_rows:
            stock_dates[code].append(date)

        self._consec_map = {}
        for code, dates in stock_dates.items():
            prev_idx = -2
            consec = 0
            for d in dates:
                idx = self._day_idx[d]
                if idx == prev_idx + 1:
                    consec += 1
                else:
                    consec = 1
                prev_idx = idx
                self._consec_map[(d, code)] = consec

        self._daily_max = {}
        self._daily_board_dist = defaultdict(lambda: defaultdict(int))
        for (date, code), cb in self._consec_map.items():
            self._daily_max[date] = max(self._daily_max.get(date, 0), cb)
            self._daily_board_dist[date][cb] += 1

        self._loaded = True
        logger.info(
            f"连板计算器加载完成: {len(self._trading_days)}个交易日, "
            f"{len(self._consec_map)}条记录"
        )

    def get_consecutive_boards(self, date: str, code: str,
                                conn: sqlite3.Connection = None) -> int:
        """获取个股在指定日期的真实连续涨停天数。"""
        self._ensure_loaded(conn)
        return self._consec_map.get((date, code), 0)

    def get_daily_max_boards(self, date: str,
                              conn: sqlite3.Connection = None) -> int:
        """获取指定日期的真实最高连板数"""
        self._ensure_loaded(conn)
        return self._daily_max.get(date, 0)

    def get_daily_board_distribution(self, date: str,
                                      conn: sqlite3.Connection = None) -> Dict[int, int]:
        """获取指定日期的板分布"""
        self._ensure_loaded(conn)
        return dict(self._daily_board_dist.get(date, {}))

    def get_trading_days(self, start_date: Optional[str] = None,
                         end_date: Optional[str] = None,
                         conn: sqlite3.Connection = None) -> List[str]:
        """获取交易日列表"""
        self._ensure_loaded(conn)
        days = self._trading_days
        if start_date:
            days = [d for d in days if d >= start_date]
        if end_date:
            days = [d for d in days if d <= end_date]
        return days

    def get_next_trading_day(self, date: str,
                              conn: sqlite3.Connection = None) -> Optional[str]:
        """获取下一个交易日"""
        self._ensure_loaded(conn)
        idx = self._day_idx.get(date, -1)
        if idx < 0 or idx + 1 >= len(self._trading_days):
            return None
        return self._trading_days[idx + 1]

    def get_prev_trading_day(self, date: str,
                              conn: sqlite3.Connection = None) -> Optional[str]:
        """获取上一个交易日"""
        self._ensure_loaded(conn)
        idx = self._day_idx.get(date, -1)
        if idx <= 0:
            return None
        return self._trading_days[idx - 1]

    def get_stock_history(self, code: str, end_date: str,
                          days: int = 10,
                          conn: sqlite3.Connection = None) -> List[Dict]:
        """获取个股近期涨停历史（含真实连板数）。"""
        self._ensure_loaded(conn)
        c = self._get_conn(conn)
        rows = c.execute(
            "SELECT date, code, name, limit_up_days as api_boards, "
            "seal_ratio, break_times, turnover_rate, volume_bias, "
            "first_limit_up_time, concept, concept_rank, seal_amount, "
            "flow_capital, change_percent "
            "FROM xgt_limit_up_detail "
            "WHERE code=? AND date < ? AND date >= ? "
            "ORDER BY date DESC LIMIT ?",
            (code, end_date, VALID_DATA_START, days)
        ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            d['consecutive_boards'] = self._consec_map.get(
                (d['date'], code), 0)
            result.append(d)
        return result

    def get_daily_stocks(self, date: str,
                          conn: sqlite3.Connection = None) -> List[Dict]:
        """获取指定日期所有涨停股（含真实连板数）。"""
        self._ensure_loaded(conn)
        c = self._get_conn(conn)
        rows = c.execute(
            "SELECT * FROM xgt_limit_up_detail WHERE date=?",
            (date,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['consecutive_boards'] = self._consec_map.get(
                (date, d['code']), 1)
            d['api_boards'] = d.get('limit_up_days', 1)
            result.append(d)
        return result

    def calculate_promotion_rates(self, date: str,
                                   conn: sqlite3.Connection = None) -> Dict:
        """计算指定日期的真实晋级率。"""
        self._ensure_loaded(conn)
        prev_date = self.get_prev_trading_day(date, conn)
        if not prev_date:
            return {}

        today_dist = self._daily_board_dist.get(date, {})
        prev_dist = self._daily_board_dist.get(prev_date, {})

        rates = {}
        max_board = self._daily_max.get(date, 0)
        for n in range(2, max_board + 1):
            today_n = today_dist.get(n, 0)
            prev_n1 = prev_dist.get(n - 1, 0)
            if prev_n1 > 0 and today_n > 0:
                rates[n] = {
                    'today_count': today_n,
                    'prev_count': prev_n1,
                    'rate': round(today_n / prev_n1, 3)
                }
        return rates

    def get_market_health(self, date: str,
                           conn: sqlite3.Connection = None) -> Dict:
        """获取市场健康度指标（基于真实连板数据）。"""
        self._ensure_loaded(conn)
        c = self._get_conn(conn)

        max_board = self._daily_max.get(date, 0)
        board_dist = dict(self._daily_board_dist.get(date, {}))
        total_limit_up = sum(board_dist.values())

        summary = c.execute(
            "SELECT * FROM xgt_daily_summary WHERE date=?", (date,)
        ).fetchone()
        limit_down = 0
        break_count = 0
        if summary:
            s = dict(summary)
            limit_down = s.get('limit_down_count', 0) or 0
            break_count = s.get('break_limit_up_count', 0) or 0

        explosion_rate = 0
        total_with_break = total_limit_up + break_count
        if total_with_break > 0:
            explosion_rate = break_count / total_with_break

        promotion_rates = self.calculate_promotion_rates(date, conn)
        avg_promotion = (
            sum(v['rate'] for v in promotion_rates.values()) /
            len(promotion_rates)
        ) if promotion_rates else 0

        smash_row = c.execute(
            "SELECT smash_coefficient FROM smash_coefficients WHERE trade_date=?",
            (date,)
        ).fetchone()
        smash_coef = smash_row[0] if smash_row else 0

        return {
            'date': date,
            'max_board': max_board,
            'board_distribution': board_dist,
            'limit_up_count': total_limit_up,
            'limit_down_count': limit_down,
            'break_count': break_count,
            'explosion_rate': round(explosion_rate, 3),
            'avg_promotion_rate': round(avg_promotion, 3),
            'promotion_rates': promotion_rates,
            'smash_coefficient': smash_coef,
        }


# 模块级单例
_instance: Optional[BoardCalculator] = None


def get_board_calculator(conn: sqlite3.Connection) -> BoardCalculator:
    """获取BoardCalculator单例"""
    global _instance
    if _instance is None:
        _instance = BoardCalculator(conn)
    return _instance


def reset_instance():
    """重置单例（测试用）"""
    global _instance
    _instance = None


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO)

    db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    calc = BoardCalculator(conn)

    # 测试
    test_date = sys.argv[1] if len(sys.argv) > 1 else '2026-08-18'
    print(f"\n=== {test_date} 市场健康度 ===")
    health = calc.get_market_health(test_date)
    for k, v in health.items():
        if k != 'promotion_rates' and k != 'board_distribution':
            print(f"  {k}: {v}")
    print(f"  板分布: {health['board_distribution']}")
    print(f"  晋级率: {health['promotion_rates']}")

    # 显示当日高板股
    stocks = calc.get_daily_stocks(test_date)
    high_boards = [s for s in stocks if s['consecutive_boards'] >= 2]
    high_boards.sort(key=lambda x: -x['consecutive_boards'])
    print(f"\n=== {test_date} 连板股（{len(high_boards)}只）===")
    for s in high_boards[:15]:
        mismatch = "" if s['consecutive_boards'] == s['api_boards'] else \
            f" ⚠️API={s['api_boards']}"
        print(f"  {s['consecutive_boards']}板 {s['code']} {s['name']:8s} "
              f"封单{s.get('seal_ratio',0):.1%} "
              f"炸板{s.get('break_times',0)}次 "
              f"{s.get('first_limit_up_time','')}{mismatch}")