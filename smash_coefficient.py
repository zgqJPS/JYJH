"""
smash_coefficient.py - 砸盘系数计算模块（核心主导因素）
砸盘系数衡量市场抛压强度：
- 系数越高 = 晋级率越低 = 抛压越重
- 系数越低 = 晋级率越高 = 市场做多氛围好

核心算法（与 realtime_fetcher 完全一致）：
- 统计每日各板级的连板分布（limit_up_days）
- 计算板级晋升比率：今日N板股票数 / 昨日N-1板股票数（N 从 2 到 当日最高板）
- 取所有有效比率的平均值，放大10倍得到砸盘系数
"""
import logging
import sqlite3
from collections import Counter

try:
    from board_calculator import BoardCalculator
    _HAS_BOARD_CALC = True
except ImportError:
    _HAS_BOARD_CALC = False

logger = logging.getLogger(__name__)


class SmashCoefficientCalculator:
    """砸盘系数计算器"""

    # 砸盘系数阈值（可被self_corrector动态调整）
    THRESHOLD_LOW_PRESSURE = 4.0    # 低于此值：抛压轻
    THRESHOLD_HIGH_PRESSURE = 7.0   # 高于此值：抛压重
    THRESHOLD_CLIMAX = 4.5          # 高潮期判断用

    def __init__(self, db):
        self.db = db
        self.db_path = getattr(db, 'db_path', None)
        self._board_calc = None
        if _HAS_BOARD_CALC and self.db_path:
            try:
                self._board_calc = BoardCalculator(sqlite3.connect(self.db_path))
            except Exception as e:
                logger.warning(f"BoardCalculator初始化失败，将使用API字段: {e}")

    def _get_limit_up_data(self, date):
        """
        从 xgt_limit_up_detail 表获取涨停数据（与 smart_recommender 同源）
        返回股票字典列表，包含 code, name, limit_up_days 等字段
        """
        try:
            if self.db_path is None:
                conn = self.db.conn
                cursor = conn.execute(
                    "SELECT code, name, limit_up_days FROM xgt_limit_up_detail WHERE date = ?",
                    (date,)
                )
                rows = cursor.fetchall()
                return [dict(r) for r in rows]
            else:
                conn = sqlite3.connect(self.db_path)
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT code, name, limit_up_days FROM xgt_limit_up_detail WHERE date = ?",
                    (date,)
                )
                rows = cursor.fetchall()
                conn.close()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"从 xgt_limit_up_detail 获取 {date} 数据失败: {e}")
            # 降级：尝试从 akshare_limit_up 获取（兼容旧数据）
            try:
                stocks = self.db.get_limit_up_data(date)
                for s in stocks:
                    if 'limit_up_days' not in s:
                        s['limit_up_days'] = s.get('continuous_boards', 1)
                return stocks
            except Exception as e2:
                logger.error(f"降级获取 {date} 数据也失败: {e2}")
                return []

    def calculate(self, start_date, end_date):
        """计算指定日期范围内每个交易日的砸盘系数"""
        try:
            all_dates = self.db.get_all_dates()
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
                    self.db.save_smash_coefficient(date, coef, max_boards)

            logger.info(f"砸盘系数计算完成: {len(results)} 个交易日")
            return results
        except Exception as e:
            logger.error(f"砸盘系数批量计算异常: {e}", exc_info=True)
            return {}

    def calculate_daily(self, date):
        """计算单日砸盘系数（对比前一交易日）"""
        try:
            all_dates = self.db.get_all_dates()
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
                self.db.save_smash_coefficient(date, coef, max_boards)
                logger.info(f"{date} 砸盘系数: {coef}，最高连板: {max_boards}")

            return coef, max_boards
        except Exception as e:
            logger.error(f"单日砸盘系数计算异常: {e}", exc_info=True)
            return None, None

    def _calc_single_date(self, date, prev_date):
        """
        计算单日砸盘系数的核心算法（与 realtime_fetcher 保持一致）
        优先使用 BoardCalculator 真实连板数，降级使用 API limit_up_days 字段
        """
        try:
            # 优先使用 BoardCalculator 获取真实板分布
            if self._board_calc:
                try:
                    today_dist_data = self._board_calc.get_daily_board_distribution(date)
                    prev_dist_data = self._board_calc.get_daily_board_distribution(prev_date)
                    if today_dist_data:
                        today_dist = Counter({int(k): int(v) for k, v in today_dist_data.items()})
                        prev_dist = Counter({int(k): int(v) for k, v in prev_dist_data.items()})
                        max_boards = self._board_calc.get_daily_max_boards(date)
                        return self._compute_smash(today_dist, prev_dist, max_boards)
                except Exception as e:
                    logger.warning(f"BoardCalculator获取{date}板分布失败，降级API: {e}")

            today_stocks = self._get_limit_up_data(date)
            if not today_stocks:
                return None, None

            prev_stocks = self._get_limit_up_data(prev_date)
            if not prev_stocks:
                return None, None

            # 提取连板数（降级：使用 limit_up_days 字段）
            today_boards = [int(s.get("limit_up_days", 1) or 1) for s in today_stocks]
            prev_boards = [int(s.get("limit_up_days", 1) or 1) for s in prev_stocks]

            today_dist = Counter(today_boards)
            prev_dist = Counter(prev_boards)

            max_boards = max(today_boards) if today_boards else 0
            return self._compute_smash(today_dist, prev_dist, max_boards)

        except Exception as e:
            logger.error(f"单日砸盘系数计算失败({date}): {e}")
            return None, None

    def _compute_smash(self, today_dist, prev_dist, max_boards):
        """从板分布计算砸盘系数（公共逻辑抽取）"""
        try:
            ratios = []

            # 只遍历到当日最高板（与 realtime_fetcher 一致）
            for n in range(2, max_boards + 1):
                today_n = today_dist.get(n, 0)
                prev_n1 = prev_dist.get(n - 1, 0)
                if prev_n1 > 0 and today_n > 0:   # 两者都非零才计算有效晋升
                    ratios.append(today_n / prev_n1)
                    logger.debug(f"  {n}板晋升: 今日{n}板={today_n}只, 昨日{n-1}板={prev_n1}只, 比率={today_n/prev_n1:.3f}")

            if ratios:
                mean_ratio = sum(ratios) / len(ratios)
                smash_coef = round(mean_ratio * 10, 2)
            else:
                # 无有效晋升比率时，使用简化的首板/高板比值
                first_board = today_dist.get(1, 1)
                high_board = sum(today_dist.get(n, 0) for n in range(2, max_boards + 1))
                if first_board > 0:
                    simplified_ratio = high_board / first_board
                    smash_coef = round(simplified_ratio * 10, 2)
                else:
                    smash_coef = 5.0

            smash_coef = max(0.0, min(20.0, smash_coef))
            return smash_coef, max_boards

        except Exception as e:
            logger.error(f"砸盘系数计算失败: {e}")
            return None, None

    # ---------- 以下辅助方法保持不变 ----------
    def get_trend(self, date, days=5):
        """获取砸盘系数趋势"""
        try:
            all_dates = self.db.get_all_dates()
            if date not in all_dates:
                return {"values": [], "trend": "未知", "change": 0, "analysis": "日期无效"}
            date_idx = all_dates.index(date)
            start_idx = max(0, date_idx - days + 1)
            recent_dates = all_dates[start_idx:date_idx + 1]

            values = []
            for d in recent_dates:
                row = self.db.get_smash_coefficient(d)
                if row:
                    values.append({"date": d, "value": dict(row).get("smash_coefficient", 0)})
                else:
                    coef, _ = self.calculate_daily(d)
                    if coef is not None:
                        values.append({"date": d, "value": coef})

            if len(values) < 2:
                return {
                    "values": values,
                    "trend": "数据不足",
                    "change": 0,
                    "analysis": "砸盘系数历史数据不足，无法判断趋势"
                }

            first_val = values[0]["value"]
            last_val = values[-1]["value"]
            change = last_val - first_val
            val_list = [v["value"] for v in values]
            avg_val = sum(val_list) / len(val_list) if val_list else 0
            max_deviation = max(abs(v - avg_val) for v in val_list) if val_list else 0

            if change > 1.5:
                trend = "上升"
                analysis = f"砸盘系数从{first_val:.1f}升至{last_val:.1f}，抛压加剧"
            elif change < -1.5:
                trend = "下降"
                analysis = f"砸盘系数从{first_val:.1f}降至{last_val:.1f}，抛压减轻"
            elif max_deviation > 2.0:
                trend = "震荡"
                analysis = f"砸盘系数在{min(val_list):.1f}~{max(val_list):.1f}区间震荡"
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
            row = self.db.get_smash_coefficient(date)
            if not row:
                coef, max_boards = self.calculate_daily(date)
                if coef is None:
                    return {
                        "signal": "未知",
                        "value": None,
                        "advantage": "砸盘系数数据缺失",
                        "disadvantage": "",
                        "trade_advice": "数据不足，建议观望"
                    }
            else:
                row = dict(row)
                coef = row.get("smash_coefficient", 5.0)

            if coef < self.THRESHOLD_LOW_PRESSURE:
                signal = "抛压轻"
                advantage = f"砸盘系数{coef}，市场抛压较轻，晋级率高"
                disadvantage = ""
                trade_advice = "抛压轻，适合主动进攻，可适当提高仓位"
            elif coef > self.THRESHOLD_HIGH_PRESSURE:
                signal = "抛压重"
                advantage = ""
                disadvantage = f"砸盘系数{coef}，市场抛压较重，晋级率低"
                trade_advice = "市场抛压重，严格控制仓位，避免追高，只做低吸，止损放宽2%"
            else:
                signal = "正常"
                advantage = f"砸盘系数{coef}，市场抛压适中"
                disadvantage = ""
                trade_advice = "市场正常运作，按常规策略操作"

            return {
                "signal": signal,
                "value": coef,
                "advantage": advantage,
                "disadvantage": disadvantage,
                "trade_advice": trade_advice,
            }
        except Exception as e:
            logger.error(f"砸盘系数信号判断异常: {e}")
            return {
                "signal": "未知",
                "value": None,
                "advantage": "",
                "disadvantage": "",
                "trade_advice": "数据异常，建议观望"
            }

    def get_market_score_impact(self, date):
        """获取砸盘系数对个股推荐评分的影响"""
        try:
            row = self.db.get_smash_coefficient(date)
            if not row:
                return 0
            coef = dict(row).get("smash_coefficient", 5.0)
            if coef > self.THRESHOLD_HIGH_PRESSURE:
                return -5
            elif coef < self.THRESHOLD_LOW_PRESSURE:
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