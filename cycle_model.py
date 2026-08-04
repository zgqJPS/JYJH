"""
cycle_model.py - 市场周期模型
基于砸盘系数×连板高度×涨停数量，自动识别市场所处的周期阶段
4阶段：冰点酝酿→蓄力爬升→爆发高潮→崩塌退潮
平均周期5.8天
统一使用 xgt_limit_up_detail 和 smash_coefficients 表
"""
import sqlite3
import logging
from config import DB_PATH

logger = logging.getLogger(__name__)


class CycleModel:
    """市场周期模型"""
    
    # 周期阶段定义
    PHASE_ICE = "冰点酝酿期"      # mb≤3, sc<2, lu=30~55
    PHASE_RISE = "蓄力爬升期"      # mb=4~5, sc=2~4, lu=40~60
    PHASE_BOOM = "爆发高潮期"      # mb≥6, sc>4, lu=70~110
    PHASE_CRASH = "崩塌退潮期"     # mb骤降, sc骤降
    
    # 转移概率矩阵（核心规律，来自67天数据统计）
    TRANSITION_MATRIX = {
        2: {"up": 1.00, "flat": 0.00, "down": 0.00, "avg_next": 3.0},
        3: {"up": 0.82, "flat": 0.18, "down": 0.00, "avg_next": 3.8},
        4: {"up": 0.61, "flat": 0.26, "down": 0.13, "avg_next": 4.4},
        5: {"up": 0.33, "flat": 0.00, "down": 0.67, "avg_next": 4.4},  # 生死线
        6: {"up": 0.83, "flat": 0.00, "down": 0.17, "avg_next": 6.5},  # 加速器
        7: {"up": 0.40, "flat": 0.00, "down": 0.60, "avg_next": 5.8},
        8: {"up": 0.00, "flat": 0.00, "down": 1.00, "avg_next": 4.0},  # 天花板
    }
    
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
    
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def detect_phase(self, date_str):
        """
        自动识别当日所处的市场周期阶段
        返回: {
            "phase": str,  # 阶段名称
            "confidence": float,  # 置信度
            "indicators": {...},  # 判断依据
            "advice": str,  # 阶段建议
            "next_phase_probability": {...},  # 下一阶段概率
        }
        """
        conn = self._get_conn()
        try:
            # 获取当日数据（从 smash_coefficients 读取）
            sc_row = conn.execute(
                "SELECT smash_coefficient, max_continuous_days as max_continuous_boards FROM smash_coefficients WHERE trade_date=? AND smash_coefficient>0",
                (date_str,)
            ).fetchone()
            
            lu_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM xgt_limit_up_detail WHERE date=?",
                (date_str,)
            ).fetchone()["cnt"]
            
            if not sc_row:
                return {"phase": "数据不足", "confidence": 0, "indicators": {}, "advice": "缺少砸盘系数数据", "next_phase_probability": {}}
            
            sc = sc_row["smash_coefficient"]
            mb = sc_row["max_continuous_boards"]
            
            # 获取前日数据（用于判断变化方向）
            prev_row = conn.execute(
                """SELECT s.smash_coefficient, s.max_continuous_days as max_continuous_boards 
                   FROM smash_coefficients s 
                   WHERE s.trade_date < ? AND s.smash_coefficient > 0
                   ORDER BY s.trade_date DESC LIMIT 1""",
                (date_str,)
            ).fetchone()
            
            prev_sc = prev_row["smash_coefficient"] if prev_row else sc
            prev_mb = prev_row["max_continuous_boards"] if prev_row else mb
            
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
            
            # 周期阶段判断（综合评分）
            scores = {
                self.PHASE_ICE: 0,
                self.PHASE_RISE: 0,
                self.PHASE_BOOM: 0,
                self.PHASE_CRASH: 0,
            }
            
            # 冰点酝酿期特征：mb≤3, sc<2, lu=30~55
            if mb <= 3:
                scores[self.PHASE_ICE] += 3
            if sc < 2:
                scores[self.PHASE_ICE] += 3
            if 30 <= lu_count <= 55:
                scores[self.PHASE_ICE] += 2
            if sc < 1.5:
                scores[self.PHASE_ICE] += 2  # 极低分歧
            
            # 蓄力爬升期特征：mb=4~5, sc=2~4, lu=40~60
            if 4 <= mb <= 5:
                scores[self.PHASE_RISE] += 3
            if 2 <= sc <= 4:
                scores[self.PHASE_RISE] += 3
            if 40 <= lu_count <= 60:
                scores[self.PHASE_RISE] += 2
            if mb_change > 0 and sc_change > 0:
                scores[self.PHASE_RISE] += 2  # 同步上升
            
            # 爆发高潮期特征：mb≥6, sc>4, lu≥70
            if mb >= 6:
                scores[self.PHASE_BOOM] += 3
            if sc > 4:
                scores[self.PHASE_BOOM] += 2
            if lu_count >= 70:
                scores[self.PHASE_BOOM] += 3
            if sc > 6:
                scores[self.PHASE_BOOM] += 2  # 极高分歧
            
            # 崩塌退潮期特征：mb从高位骤降, sc骤降
            if mb_change < -2:
                scores[self.PHASE_CRASH] += 4
            if sc_change < -2:
                scores[self.PHASE_CRASH] += 3
            if prev_mb >= 6 and mb <= 4:
                scores[self.PHASE_CRASH] += 4  # 从高板骤降
            if prev_sc >= 5 and sc < 3:
                scores[self.PHASE_CRASH] += 3  # 砸盘骤降
            
            # 选择得分最高的阶段
            phase = max(scores, key=scores.get)
            total_score = sum(scores.values()) or 1
            confidence = round(scores[phase] / total_score, 2)
            
            # 阶段建议
            advice_map = {
                self.PHASE_ICE: "市场冰点，能量暗中积聚。观察是否出现砸盘骤降信号，准备抄底低位龙头。",
                self.PHASE_RISE: "蓄力爬升中，关注能否突破5板生死线。若5→6时砸盘下降，是强烈看涨信号。",
                self.PHASE_BOOM: "高潮期，涨停数暴增但分歧加剧。关注是否出现见顶信号(7板+砸盘>6)，准备撤退。",
                self.PHASE_CRASH: "崩塌退潮中，等待底部信号(连续2天砸盘<3+连板≤3)再入场。",
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
        """基于当前阶段和指标，计算下一阶段概率"""
        mb = indicators["max_boards"]
        sc = indicators["smash_coefficient"]
        lu = indicators["limit_up_count"]
        sc_change = indicators["sc_change"]
        
        prob = {
            self.PHASE_ICE: 0,
            self.PHASE_RISE: 0,
            self.PHASE_BOOM: 0,
            self.PHASE_CRASH: 0,
        }
        
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
        """
        基于转移概率矩阵预测次日连板高度
        返回: {
            "predicted_boards": int,
            "probability_up": float,
            "probability_down": float,
            "confidence": float,
            "reason": str,
        }
        """
        conn = self._get_conn()
        try:
            sc_row = conn.execute(
                "SELECT smash_coefficient, max_continuous_days as max_continuous_boards FROM smash_coefficients WHERE trade_date=? AND smash_coefficient>0",
                (date_str,)
            ).fetchone()
            
            if not sc_row:
                return {"predicted_boards": 4, "probability_up": 0.5, "probability_down": 0.5, "confidence": 0.2, "reason": "数据不足"}
            
            mb = sc_row["max_continuous_boards"]
            sc = sc_row["smash_coefficient"]
            
            # 基础预测：来自转移概率矩阵
            matrix = self.TRANSITION_MATRIX.get(mb, {"up": 0.5, "flat": 0.2, "down": 0.3, "avg_next": 4.0})
            
            # 砸盘系数修正
            prev_row = conn.execute(
                """SELECT smash_coefficient FROM smash_coefficients 
                   WHERE trade_date < ? AND smash_coefficient > 0 ORDER BY trade_date DESC LIMIT 1""",
                (date_str,)
            ).fetchone()
            
            sc_change = sc - (prev_row["smash_coefficient"] if prev_row else sc)
            
            if sc_change < -1.5:
                adj = 0.15
            elif sc_change > 1.5:
                adj = -0.15
            else:
                adj = 0
            
            prob_up = min(1.0, max(0.0, matrix["up"] + adj))
            prob_down = min(1.0, max(0.0, matrix["down"] - adj))
            
            predicted = round(matrix["avg_next"])
            
            reason_parts = [f"当前{mb}板，转移概率: ↑{matrix['up']*100:.0f}% ↓{matrix['down']*100:.0f}%"]
            
            if mb == 5 and sc_change < 0:
                predicted = 7
                prob_up = 1.0
                reason_parts.append("⚡5→6突破+砸盘下降=最强看涨信号(历史3/3)")
            elif mb >= 7 and sc > 6:
                predicted = max(3, mb - 3)
                prob_down = 1.0
                reason_parts.append("⚠️7板+砸盘>6=见顶崩塌信号(历史100%)")
            elif mb >= 8:
                predicted = 4
                prob_down = 1.0
                reason_parts.append("⚠️8板天花板，100%回落")
            
            return {
                "predicted_boards": predicted,
                "probability_up": round(prob_up, 2),
                "probability_down": round(prob_down, 2),
                "confidence": round(max(prob_up, prob_down), 2),
                "reason": "；".join(reason_parts),
            }
        finally:
            conn.close()
    
    def detect_signals(self, date_str):
        """
        检测当日是否触发5个高价值预测信号
        返回: [{"signal_id": 1~5, "triggered": bool, "details": str, "strength": 1~3}]
        """
        conn = self._get_conn()
        try:
            signals = []
            
            today = conn.execute(
                """SELECT s.smash_coefficient as sc, s.max_continuous_days as mb,
                          (SELECT COUNT(*) FROM xgt_limit_up_detail WHERE date=s.trade_date) as lu
                   FROM smash_coefficients s WHERE s.trade_date=? AND s.smash_coefficient>0""",
                (date_str,)
            ).fetchone()
            
            if not today:
                return [{"signal_id": i, "triggered": False, "details": "数据不足", "strength": 0, "name": f"信号{i}"} for i in range(1, 6)]
            
            sc = today["sc"]
            mb = today["mb"]
            lu = today["lu"]
            
            prev = conn.execute(
                """SELECT s.smash_coefficient as sc, s.max_continuous_days as mb,
                          (SELECT COUNT(*) FROM xgt_limit_up_detail WHERE date=s.trade_date) as lu
                   FROM smash_coefficients s 
                   WHERE s.trade_date < ? AND s.smash_coefficient > 0
                   ORDER BY s.trade_date DESC LIMIT 1""",
                (date_str,)
            ).fetchone()
            
            if not prev:
                return [{"signal_id": i, "triggered": False, "details": "无前日数据", "strength": 0, "name": f"信号{i}"} for i in range(1, 6)]
            
            prev_sc = prev["sc"]
            prev_mb = prev["mb"]
            sc_change = sc - prev_sc
            mb_change = mb - prev_mb
            
            prev2 = conn.execute(
                """SELECT s.smash_coefficient as sc, s.max_continuous_days as mb
                   FROM smash_coefficients s 
                   WHERE s.trade_date < ? AND s.smash_coefficient > 0
                   ORDER BY s.trade_date DESC LIMIT 1 OFFSET 1""",
                (date_str,)
            ).fetchone()
            
            # 信号1: 5→6突破+砸盘下降
            s1_triggered = (prev_mb == 5 and mb == 6 and sc_change < 0)
            signals.append({
                "signal_id": 1,
                "name": "5→6突破+砸盘下降",
                "triggered": s1_triggered,
                "strength": 3 if s1_triggered else 0,
                "details": f"连板{prev_mb}→{mb}，砸盘{prev_sc:.2f}→{sc:.2f}({sc_change:+.2f})" if not s1_triggered else
                           f"⚡触发！连板5→6突破，砸盘下降{abs(sc_change):.2f}，次日涨停数预期77.8(历史3/3验证)",
            })
            
            # 信号2: 砸盘骤降>3+连板≤3
            s2_triggered = (sc_change < -3 and mb <= 3)
            signals.append({
                "signal_id": 2,
                "name": "砸盘骤降>3+连板≤3",
                "triggered": s2_triggered,
                "strength": 3 if s2_triggered else 0,
                "details": f"砸盘变化{sc_change:+.2f}，连板{mb}" if not s2_triggered else
                           f"⚡触发！砸盘骤降{abs(sc_change):.2f}点，连板仅{mb}板，见底反弹信号(历史83%)",
            })
            
            # 信号3: 连续2天砸盘<3+连板≤3
            s3_triggered = False
            if prev2:
                s3_triggered = (prev_sc < 3 and sc < 3 and mb <= 3 and prev["mb"] <= 3)
            signals.append({
                "signal_id": 3,
                "name": "连续2天砸盘<3+连板≤3",
                "triggered": s3_triggered,
                "strength": 2 if s3_triggered else 0,
                "details": f"今日砸盘{sc:.2f}，前日{prev_sc:.2f}，连板{mb}" if not s3_triggered else
                           f"⚡触发！连续低分歧+低位连板，底部确认信号(历史75%)",
            })
            
            # 信号4: 7板+砸盘>6
            s4_triggered = (mb >= 7 and sc > 6)
            signals.append({
                "signal_id": 4,
                "name": "7板+砸盘>6",
                "triggered": s4_triggered,
                "strength": 2 if s4_triggered else 0,
                "details": f"连板{mb}，砸盘{sc:.2f}" if not s4_triggered else
                           f"⚠️触发！连板{mb}+砸盘{sc:.2f}，见顶崩塌信号(历史100%)",
            })
            
            # 信号5: 4板+涨停数<35+砸盘<3
            s5_triggered = (mb == 4 and lu < 35 and sc < 3)
            signals.append({
                "signal_id": 5,
                "name": "4板+涨停<35+砸盘<3",
                "triggered": s5_triggered,
                "strength": 1 if s5_triggered else 0,
                "details": f"连板{mb}，涨停{lu}只，砸盘{sc:.2f}" if not s5_triggered else
                           f"⚠️触发！假突破预警：连板仅到4，涨停{lu}只(<35)，砸盘{sc:.2f}(<3)，量能不足",
            })
            
            return signals
        finally:
            conn.close()
    
    def get_historical_cycles(self, start_date=None, end_date=None):
        """回溯历史周期，标注每个交易日属于哪个周期阶段"""
        conn = self._get_conn()
        try:
            query = """
                SELECT s.trade_date as date, s.smash_coefficient as sc, 
                       s.max_continuous_days as mb,
                       (SELECT COUNT(*) FROM xgt_limit_up_detail WHERE date=s.trade_date) as lu
                FROM smash_coefficients s
                WHERE s.smash_coefficient > 0
            """
            params = []
            if start_date:
                query += " AND s.trade_date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND s.trade_date <= ?"
                params.append(end_date)
            query += " ORDER BY s.trade_date"
            
            rows = conn.execute(query, params).fetchall()
            
            cycles = []
            for i, row in enumerate(rows):
                row = dict(row)
                if i > 0:
                    prev = dict(rows[i-1])
                    sc_change = row["sc"] - prev["sc"]
                    mb_change = row["mb"] - prev["mb"]
                else:
                    sc_change = 0
                    mb_change = 0
                
                mb, sc, lu = row["mb"], row["sc"], row["lu"]
                if mb <= 3 and sc < 2:
                    phase = self.PHASE_ICE
                elif mb >= 6 and sc > 4:
                    phase = self.PHASE_BOOM
                elif mb_change < -2 or sc_change < -3:
                    phase = self.PHASE_CRASH
                elif 4 <= mb <= 5:
                    phase = self.PHASE_RISE
                elif mb <= 3 and sc >= 2:
                    phase = self.PHASE_ICE
                else:
                    phase = self.PHASE_RISE
                
                cycles.append({
                    "date": row["date"],
                    "phase": phase,
                    "smash_coefficient": row["sc"],
                    "max_boards": row["mb"],
                    "limit_up_count": row["lu"],
                    "sc_change": round(sc_change, 2),
                    "mb_change": mb_change,
                })
            
            return cycles
        finally:
            conn.close()