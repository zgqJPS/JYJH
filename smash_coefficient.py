"""
smash_coefficient.py - 砸盘系数计算模块（核心主导因素）
砸盘系数衡量市场抛压强度：
- 系数越高 = 晋级率越低 = 抛压越重
- 系数越低 = 晋级率越高 = 市场做多氛围好

核心算法：
- 统计每日各板级（1-10板）的连板分布
- 计算板级晋升比率：今日N板股票数 / 昨日N-1板股票数（N从2到10）
- 取所有有效比率的平均值，放大10倍得到砸盘系数

数据源：统一使用 xgt_limit_up_detail 表
配置：从 config.SMASH_CONFIG 读取阈值
"""
import logging
import sqlite3
from collections import Counter
from functools import lru_cache

from config import SMASH_CONFIG, DB_PATH

logger = logging.getLogger(__name__)


class SmashCoefficientCalculator:
    """砸盘系数计算器"""

    def __init__(self, db=None):
        self.db = db
        self.db_path = getattr(db, 'db_path', DB_PATH) if db else DB_PATH
        # 从配置读取阈值
        self.THRESHOLD_LOW_PRESSURE = SMASH_CONFIG.get("low_pressure_threshold", 4.0)
        self.THRESHOLD_HIGH_PRESSURE = SMASH_CONFIG.get("high_pressure_threshold", 7.0)
        self.THRESHOLD_CLIMAX = SMASH_CONFIG.get("climax_threshold", 4.5)
        self.MAX_BOARD_LEVEL = SMASH_CONFIG.get("max_board_level", 10)

    def _get_conn(self):
        """获取数据库连接"""
        if self.db and hasattr(self.db, 'conn'):
            return self.db.conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_limit_up_data(self, date):
        """
        从 xgt_limit_up_detail 表获取涨停数据
        返回股票字典列表，包含 code, name, limit_up_days 等字段
        """
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT code, name, limit_up_days FROM xgt_limit_up_detail WHERE date = ?",
                (date,)
            )
            rows = cursor.fetchall()
            # 如果连接是独立创建的，不关闭（由调用方管理）
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"从 xgt_limit_up_detail 获取 {date} 数据失败: {e}")
            # 降级：尝试从 akshare_limit_up 获取
            try:
                conn = self._get_conn()
                stocks = conn.execute(
                    "SELECT code, name, continuous_boards as limit_up_days "
                    "FROM akshare_limit_up WHERE date = ?",
                    (date,)
                ).fetchall()
                return [dict(r) for r in stocks]
            except Exception as e2:
                logger.error(f"降级获取 {date} 数据也失败: {e2}")
                return []

    def calculate(self, start_date, end_date):
        """计算指定日期范围内每个交易日的砸盘系数"""
        try:
            conn = self._get_conn()
            all_dates = [r['date'] for r in conn.execute(
                "SELECT DISTINCT date FROM xgt_limit_up_detail ORDER BY date"
            ).fetchall()]
            if not all_dates:
                all_dates = [r['date'] for r in conn.execute(
                    "SELECT DISTINCT date FROM akshare_limit_up ORDER BY date"
                ).fetchall()]

            target_dates = [d for d in all_dates if start_date <= d <= end_date]
            if not target_dates:
                logger.warning(f"日期范围 {start_date}~{end_date} 内无交易日数据")
                return {}

            results = {}
            for date in target_dates:
                date_idx = all_dates.index(date)
                if date_idx <= 0:
                    continue

                prev_date = all_dates[date_idx - 1]
                coef, max_boards = self._calc_single_date(date, prev_date)

                if coef is not None:
                    results[date] = {
                        "smash_coefficient": coef,
                        "max_continuous_boards": max_boards,
                    }
                    self._save_coefficient(date, coef, max_boards)

            logger.info(f"砸盘系数计算完成: {len(results)} 个交易日")
            return results

        except Exception as e:
            logger.error(f"砸盘系数批量计算异常: {e}", exc_info=True)
            return {}

    @lru_cache(maxsize=128)
    def _get_cached_single_date(self, date, prev_date):
        """缓存单日计算结果"""
        return self._calc_single_date_impl(date, prev_date)

    def calculate_daily(self, date):
        """计算单日砸盘系数"""
        try:
            conn = self._get_conn()
            all_dates = [r['date'] for r in conn.execute(
                "SELECT DISTINCT date FROM xgt_limit_up_detail ORDER BY date"
            ).fetchall()]
            if not all_dates:
                all_dates = [r['date'] for r in conn.execute(
                    "SELECT DISTINCT date FROM akshare_limit_up ORDER BY date"
                ).fetchall()]

            if date not in all_dates:
                logger.warning(f"{date} 不在交易日列表中")
                return None, None

            date_idx = all_dates.index(date)
            if date_idx <= 0:
                logger.warning(f"{date} 是第一个交易日，无前日数据可对比")
                return None, None

            prev_date = all_dates[date_idx - 1]
            coef, max_boards = self._calc_single_date(date, prev_date)

            if coef is not None:
                self._save_coefficient(date, coef, max_boards)
                logger.info(f"{date} 砸盘系数: {coef}，最高连板: {max_boards}")

            return coef, max_boards

        except Exception as e:
            logger.error(f"单日砸盘系数计算异常: {e}", exc_info=True)
            return None, None

    def _calc_single_date(self, date, prev_date):
        """计算单日砸盘系数的核心算法（带缓存）"""
        return self._get_cached_single_date(date, prev_date)

    def _calc_single_date_impl(self, date, prev_date):
        """实际的单日计算实现"""
        try:
            today_stocks = self._get_limit_up_data(date)
            if not today_stocks:
                return None, None

            prev_stocks = self._get_limit_up_data(prev_date)
            if not prev_stocks:
                return None, None

            today_boards = [int(s.get("limit_up_days", 1) or 1) for s in today_stocks]
            prev_boards = [int(s.get("limit_up_days", 1) or 1) for s in prev_stocks]

            today_dist = Counter(today_boards)
            prev_dist = Counter(prev_boards)

            max_boards = max(today_boards) if today_boards else 0

            ratios = []
            for n in range(2, self.MAX_BOARD_LEVEL + 1):
                today_n = today_dist.get(n, 0)
                prev_n_minus_1 = prev_dist.get(n - 1, 0)
                if prev_n_minus_1 > 0:
                    ratio = today_n / prev_n_minus_1
                    ratios.append(ratio)
                    logger.debug(f"  {n}板晋升: 今日{n}板={today_n}只, 昨日{n-1}板={prev_n_minus_1}只, 比率={ratio:.3f}")

            if not ratios:
                first_board = today_dist.get(1, 1)
                high_board = sum(today_dist.get(n, 0) for n in range(2, self.MAX_BOARD_LEVEL + 1))
                if first_board > 0:
                    simplified_ratio = high_board / first_board
                    smash_coef = round(simplified_ratio * 10, 2)
                else:
                    smash_coef = 5.0
            else:
                mean_ratio = sum(ratios) / len(ratios)
                smash_coef = round(mean_ratio * 10, 2)

            return max(0.0, min(20.0, smash_coef)), max_boards

        except Exception as e:
            logger.error(f"单日砸盘系数计算失败({date}): {e}")
            return None, None

    def _save_coefficient(self, date, coefficient, max_boards):
        """保存砸盘系数到数据库"""
        try:
            conn = self._get_conn()
            # 写入统一表 smash_coefficients
            conn.execute(
                "INSERT OR REPLACE INTO smash_coefficients (trade_date, smash_coefficient, max_continuous_days) VALUES (?, ?, ?)",
                (date, coefficient, max_boards)
            )
            # 同时写入旧表（兼容，后续可移除）
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO smash_coefficient_results (date, smash_coefficient, max_continuous_boards) VALUES (?, ?, ?)",
                    (date, coefficient, max_boards)
                )
            except Exception:
                pass
            if not hasattr(self.db, 'conn'):
                conn.commit()
                conn.close()
        except Exception as e:
            logger.error(f"保存砸盘系数失败: {e}")

    def get_trend(self, date, days=5):
        """获取砸盘系数趋势"""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT trade_date, smash_coefficient FROM smash_coefficients "
                "WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?",
                (date, days)
            ).fetchall()
            if not rows:
                return {"values": [], "trend": "数据不足", "change": 0, "analysis": "无数据"}

            values = [{"date": r['trade_date'], "value": r['smash_coefficient']} for r in reversed(rows)]
            if len(values) < 2:
                return {"values": values, "trend": "数据不足", "change": 0, "analysis": "历史数据不足"}

            first_val = values[0]["value"]
            last_val = values[-1]["value"]
            change = last_val - first_val
            val_list = [v["value"] for v in values]
            avg_val = sum(val_list) / len(val_list)

            # 使用配置的阈值
            rise_threshold = SMASH_CONFIG.get("trend_rise_threshold", 1.0)
            fall_threshold = SMASH_CONFIG.get("trend_fall_threshold", -1.0)

            if change > rise_threshold:
                trend = "上升"
                analysis = f"砸盘系数从{first_val:.1f}升至{last_val:.1f}，抛压加剧"
            elif change < fall_threshold:
                trend = "下降"
                analysis = f"砸盘系数从{first_val:.1f}降至{last_val:.1f}，抛压减轻"
            else:
                trend = "平稳"
                analysis = f"砸盘系数稳定在{avg_val:.1f}附近"

            return {
                "values": values,
                "trend": trend,
                "change": round(change, 2),
                "analysis": analysis,
            }
        except Exception as e:
            logger.error(f"砸盘系数趋势分析异常: {e}")
            return {"values": [], "trend": "未知", "change": 0, "analysis": f"分析异常: {e}"}

    def get_signal(self, date):
        """基于砸盘系数给出市场信号"""
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT smash_coefficient FROM smash_coefficients WHERE trade_date = ?",
                (date,)
            ).fetchone()

            if not row:
                coef, _ = self.calculate_daily(date)
                if coef is None:
                    return {"signal": "未知", "value": None, "advantage": "数据缺失", "disadvantage": "", "trade_advice": "数据不足"}
            else:
                coef = row['smash_coefficient']

            # 使用配置的阈值
            low_threshold = SMASH_CONFIG.get("low_pressure_threshold", 4.0)
            high_threshold = SMASH_CONFIG.get("high_pressure_threshold", 7.0)

            if coef < low_threshold:
                return {
                    "signal": "抛压轻",
                    "value": coef,
                    "advantage": f"砸盘系数{coef}，市场抛压较轻，晋级率高",
                    "disadvantage": "",
                    "trade_advice": "抛压轻，适合主动进攻，可适当提高仓位"
                }
            elif coef > high_threshold:
                return {
                    "signal": "抛压重",
                    "value": coef,
                    "advantage": "",
                    "disadvantage": f"砸盘系数{coef}，市场抛压较重，晋级率低",
                    "trade_advice": "市场抛压重，严格控制仓位，避免追高，只做低吸，止损放宽2%"
                }
            else:
                return {
                    "signal": "正常",
                    "value": coef,
                    "advantage": f"砸盘系数{coef}，市场抛压适中",
                    "disadvantage": "",
                    "trade_advice": "市场正常运作，按常规策略操作"
                }
        except Exception as e:
            logger.error(f"砸盘系数信号判断异常: {e}")
            return {"signal": "未知", "value": None, "advantage": "", "disadvantage": "", "trade_advice": "数据异常"}

    def get_market_score_impact(self, date):
        """获取砸盘系数对个股推荐评分的影响"""
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT smash_coefficient FROM smash_coefficients WHERE trade_date = ?",
                (date,)
            ).fetchone()
            if not row:
                return 0
            coef = row['smash_coefficient']
            high_threshold = SMASH_CONFIG.get("high_pressure_threshold", 7.0)
            low_threshold = SMASH_CONFIG.get("low_pressure_threshold", 4.0)

            if coef > high_threshold:
                return -5
            elif coef < low_threshold:
                return 3
            elif coef > 6.0:
                return -3
            elif coef > 5.0:
                return -1
            else:
                return 0
        except Exception as e:
            logger.error(f"砸盘系数评分影响计算异常: {e}")
            return 0