"""
cycle_model.py - 市场周期模型
基于砸盘系数×连板高度×涨停数量，自动识别市场所处的周期阶段
4阶段：冰点酝酿→蓄力爬升→爆发高潮→崩塌退潮
平均周期5.8天
"""
import sqlite3
import logging
from datetime import datetime
from functools import lru_cache

logger = logging.getLogger(__name__)


class CycleModel:
    """市场周期模型"""

    PHASE_ICE = "冰点酝酿期"
    PHASE_RISE = "蓄力爬升期"
    PHASE_BOOM = "爆发高潮期"
    PHASE_CRASH = "崩塌退潮期"

    def __init__(self, db_path):
        self.db_path = db_path
        self._cache = {}

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @lru_cache(maxsize=128)
    def detect_phase(self, date_str):
        """自动识别当日所处的市场周期阶段"""
        conn = self._get_conn()
        try:
            # 获取当日数据
            sc_row = conn.execute(
                "SELECT smash_coefficient, max_continuous_days FROM smash_coefficients WHERE trade_date=?",
                (date_str,)
            ).fetchone()

            if not sc_row:
                return {"phase": "数据不足", "confidence": 0, "indicators": {}, "advice": "缺少砸盘系数数据"}

            sc = sc_row["smash_coefficient"]
            mb = sc_row["max_continuous_days"] or 0

            # 获取涨停数
            lu_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM xgt_limit_up_detail WHERE date=?",
                (date_str,)
            ).fetchone()
            lu_count = lu_row["cnt"] if lu_row else 0

            # 获取前日数据
            prev_row = conn.execute(
                "SELECT smash_coefficient, max_continuous_days FROM smash_coefficients "
                "WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1",
                (date_str,)
            ).fetchone()

            prev_sc = prev_row["smash_coefficient"] if prev_row else sc
            prev_mb = prev_row["max_continuous_days"] if prev_row else mb

            sc_change = sc - prev_sc
            mb_change = mb - prev_mb

            indicators = {
                "smash_coefficient": sc,
                "max_boards": mb,
                "limit_up_count": lu_count,
                "sc_change": round(sc_change, 2),
                "mb_change": mb_change,
                "prev_sc": prev_sc,
                "prev_mb": prev_mb,
            }

            # 周期判断
            scores = {self.PHASE_ICE: 0, self.PHASE_RISE: 0, self.PHASE_BOOM: 0, self.PHASE_CRASH: 0}

            # 冰点期
            if mb <= 3:
                scores[self.PHASE_ICE] += 3
            if sc < 2:
                scores[self.PHASE_ICE] += 3
            if 30 <= lu_count <= 55:
                scores[self.PHASE_ICE] += 2

            # 蓄力期
            if 4 <= mb <= 5:
                scores[self.PHASE_RISE] += 3
            if 2 <= sc <= 4:
                scores[self.PHASE_RISE] += 3
            if 40 <= lu_count <= 60:
                scores[self.PHASE_RISE] += 2
            if mb_change > 0 and sc_change > 0:
                scores[self.PHASE_RISE] += 2

            # 高潮期
            if mb >= 6:
                scores[self.PHASE_BOOM] += 3
            if sc > 4:
                scores[self.PHASE_BOOM] += 2
            if lu_count >= 70:
                scores[self.PHASE_BOOM] += 3

            # 崩塌期
            if mb_change < -2:
                scores[self.PHASE_CRASH] += 4
            if sc_change < -2:
                scores[self.PHASE_CRASH] += 3
            if prev_mb >= 6 and mb <= 4:
                scores[self.PHASE_CRASH] += 4
            if prev_sc >= 5 and sc < 3:
                scores[self.PHASE_CRASH] += 3

            # 选择最高分
            phase = max(scores, key=scores.get)
            total_score = sum(scores.values()) or 1
            confidence = round(scores[phase] / total_score, 2)

            advice_map = {
                self.PHASE_ICE: "市场冰点，能量暗中积聚。观察砸盘骤降信号，准备抄底低位龙头。",
                self.PHASE_RISE: "蓄力爬升中，关注能否突破5板生死线。若5→6时砸盘下降，是强烈看涨信号。",
                self.PHASE_BOOM: "高潮期，涨停数暴增但分歧加剧。关注见顶信号，准备撤退。",
                self.PHASE_CRASH: "崩塌退潮中，等待底部信号再入场。",
            }

            # 下一阶段概率
            next_prob = self._calc_next_phase_probability(phase, indicators)

            return {
                "phase": phase,
                "confidence": confidence,
                "scores": scores,
                "indicators": indicators,
                "advice": advice_map.get(phase, ""),
                "next_phase_probability": next_prob,
            }

        finally:
            conn.close()

    def _calc_next_phase_probability(self, current_phase, indicators):
        """计算下一阶段概率"""
        mb = indicators["max_boards"]
        sc = indicators["smash_coefficient"]
        lu = indicators["limit_up_count"]
        sc_change = indicators["sc_change"]

        prob = {self.PHASE_ICE: 0, self.PHASE_RISE: 0, self.PHASE_BOOM: 0, self.PHASE_CRASH: 0}

        if current_phase == self.PHASE_ICE:
            prob[self.PHASE_RISE] = 0.65
            prob[self.PHASE_ICE] = 0.25
            prob[self.PHASE_BOOM] = 0.05
            prob[self.PHASE_CRASH] = 0.05
        elif current_phase == self.PHASE_RISE:
            prob[self.PHASE_RISE] = 0.35
            prob[self.PHASE_BOOM] = 0.35
            prob[self.PHASE_ICE] = 0.15
            prob[self.PHASE_CRASH] = 0.15
            if mb >= 5 and sc <= 4:
                prob[self.PHASE_BOOM] += 0.15
                prob[self.PHASE_RISE] -= 0.15
        elif current_phase == self.PHASE_BOOM:
            prob[self.PHASE_BOOM] = 0.30
            prob[self.PHASE_CRASH] = 0.40
            prob[self.PHASE_RISE] = 0.20
            prob[self.PHASE_ICE] = 0.10
            if sc > 6 and mb >= 7:
                prob[self.PHASE_CRASH] = 0.75
                prob[self.PHASE_BOOM] = 0.10
                prob[self.PHASE_RISE] = 0.10
                prob[self.PHASE_ICE] = 0.05
        elif current_phase == self.PHASE_CRASH:
            prob[self.PHASE_CRASH] = 0.30
            prob[self.PHASE_ICE] = 0.50
            prob[self.PHASE_RISE] = 0.15
            prob[self.PHASE_BOOM] = 0.05

        return prob

    def predict_next_day_boards(self, date_str):
        """预测次日连板高度"""
        conn = self._get_conn()
        try:
            sc_row = conn.execute(
                "SELECT smash_coefficient, max_continuous_days FROM smash_coefficients WHERE trade_date=?",
                (date_str,)
            ).fetchone()

            if not sc_row:
                return {"predicted_boards": 4, "probability_up": 0.5, "probability_down": 0.5, "confidence": 0.2}

            mb = sc_row["max_continuous_days"] or 0
            sc = sc_row["smash_coefficient"]

            # 转移概率矩阵
            matrix = {
                2: {"up": 1.00, "down": 0.00, "avg_next": 3.0},
                3: {"up": 0.82, "down": 0.00, "avg_next": 3.8},
                4: {"up": 0.61, "down": 0.13, "avg_next": 4.4},
                5: {"up": 0.33, "down": 0.67, "avg_next": 4.4},
                6: {"up": 0.83, "down": 0.17, "avg_next": 6.5},
                7: {"up": 0.40, "down": 0.60, "avg_next": 5.8},
                8: {"up": 0.00, "down": 1.00, "avg_next": 4.0},
            }.get(mb, {"up": 0.5, "down": 0.3, "avg_next": 4.0})

            # 获取前日数据计算趋势
            prev_row = conn.execute(
                "SELECT smash_coefficient FROM smash_coefficients "
                "WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1",
                (date_str,)
            ).fetchone()

            sc_change = sc - (prev_row["smash_coefficient"] if prev_row else sc)

            adj = 0.15 if sc_change < -1.5 else (-0.15 if sc_change > 1.5 else 0)
            prob_up = max(0.0, min(1.0, matrix["up"] + adj))

            predicted = round(matrix["avg_next"])

            # 特殊信号覆盖
            if mb == 5 and sc_change < 0:
                predicted = 7
                prob_up = 1.0
            elif mb >= 7 and sc > 6:
                predicted = max(3, mb - 3)
                prob_up = 0.0
            elif mb >= 8:
                predicted = 4
                prob_up = 0.0

            return {
                "predicted_boards": predicted,
                "probability_up": round(prob_up, 2),
                "probability_down": round(1 - prob_up, 2),
                "confidence": round(max(prob_up, 1 - prob_up), 2),
            }

        finally:
            conn.close()