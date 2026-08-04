"""
market_analyzer.py - 市场分析引擎
每日涨停统计、连板梯队、封板质量、概念热度、情绪指标计算
砸盘系数统一从 smash_coefficients 表读取（与 smart_recommender 同源）
统一使用 xgt_limit_up_detail 表的字段：limit_up_days, seal_ratio
市场周期统一使用 cycle_model.py
"""
import logging
from collections import defaultdict, Counter

logger = logging.getLogger(__name__)


class MarketAnalyzer:
    """市场分析引擎"""

    def __init__(self, db):
        self.db = db
        from cycle_model import CycleModel
        self.cycle_model = CycleModel(db.db_path if hasattr(db, 'db_path') else None)

    def analyze_date(self, date_str):
        """
        完整分析某一天的市场数据
        返回包含所有分析维度的字典
        """
        stocks = self.db.get_limit_up_data(date_str)
        if not stocks:
            logger.warning(f"{date_str} 无涨停数据")
            return None

        stocks = [dict(s) for s in stocks]

        result = {
            "date": date_str,
            "basic_stats": self._basic_stats(stocks),
            "board_tiers": self._board_tiers(stocks),
            "seal_quality": self._seal_quality(stocks),
            "concept_heat": self._concept_heat(date_str, stocks),
            "sentiment_score": self._calc_sentiment(stocks, date_str),
            "continuation_analysis": self._continuation_analysis(date_str, stocks),
            "comparison": self._compare_with_previous(date_str, stocks),
            "smash_analysis": self._smash_analysis(date_str),
        }
        return result

    def _smash_analysis(self, date_str):
        """砸盘系数分析（统一从 smash_coefficients 表读取）"""
        try:
            conn = self.db.conn
            cursor = conn.execute(
                "SELECT smash_coefficient FROM smash_coefficients WHERE trade_date = ?",
                (date_str,)
            )
            row = cursor.fetchone()
            smash_value = row[0] if row else None

            cursor = conn.execute(
                "SELECT trade_date, smash_coefficient FROM smash_coefficients WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 3",
                (date_str,)
            )
            rows = cursor.fetchall()
            trend_values = [{"date": r[0], "value": r[1]} for r in rows]

            if len(trend_values) >= 2:
                curr = trend_values[0]["value"]
                prev = trend_values[-1]["value"]
                if curr - prev > 1.0:
                    trend = "上升"
                elif prev - curr > 1.0:
                    trend = "下降"
                else:
                    trend = "平稳"
            else:
                trend = "未知"

            if smash_value is None:
                signal = "未知"
                advantage = "砸盘系数数据缺失"
                disadvantage = ""
                trade_advice = ""
            elif smash_value > 6.0:
                signal = "高风险（砸盘严重）"
                advantage = "市场抛压极大，需谨慎"
                disadvantage = "砸盘系数过高，大盘风险显著"
                trade_advice = "建议空仓或极轻仓"
            elif smash_value > 4.0:
                signal = "中等风险"
                advantage = "市场有一定抛压，控制仓位"
                disadvantage = "砸盘系数偏高，注意回撤"
                trade_advice = "建议5成以下仓位"
            elif smash_value > 2.0:
                signal = "低风险"
                advantage = "市场抛压轻，适合进攻"
                disadvantage = ""
                trade_advice = "可积极操作"
            else:
                signal = "极低风险"
                advantage = "市场情绪温和，机会较好"
                disadvantage = ""
                trade_advice = "可重仓参与"

            # 统一使用 CycleModel 获取周期阶段
            try:
                phase_result = self.cycle_model.detect_phase(date_str)
                cycle_phase = phase_result.get("phase", "")
            except Exception as e:
                logger.warning(f"CycleModel 调用失败: {e}")
                cycle_phase = ""

            return {
                "smash_coefficient": smash_value,
                "signal": signal,
                "advantage": advantage,
                "disadvantage": disadvantage,
                "trade_advice": trade_advice,
                "trend": trend,
                "trend_analysis": f"近三日系数: {[v['value'] for v in trend_values]}" if trend_values else "",
                "trend_values": trend_values,
                "cycle_phase_by_smash": cycle_phase,
            }
        except Exception as e:
            logger.error(f"砸盘系数分析异常: {e}")
            return {
                "smash_coefficient": None,
                "signal": "未知",
                "advantage": "砸盘系数查询异常",
                "disadvantage": "",
                "trade_advice": "",
                "trend": "未知",
                "trend_analysis": "",
                "trend_values": [],
                "cycle_phase_by_smash": "",
            }

    def _basic_stats(self, stocks):
        """基础统计 - 使用 limit_up_days 替代 continuous_boards"""
        boards = [s.get("limit_up_days", 1) or 1 for s in stocks]
        seal_ratios = [s.get("seal_ratio", 0) or 0 for s in stocks]
        turnover_rates = [s.get("turnover_rate", 0) or 0 for s in stocks]

        board_dist = Counter(boards)
        boards_2plus = sum(1 for b in boards if b >= 2)
        boards_3plus = sum(1 for b in boards if b >= 3)
        boards_5plus = sum(1 for b in boards if b >= 5)

        return {
            "total_count": len(stocks),
            "max_boards": max(boards) if boards else 0,
            "avg_boards": sum(boards) / len(boards) if boards else 0,
            "boards_2plus": boards_2plus,
            "boards_3plus": boards_3plus,
            "boards_5plus": boards_5plus,
            "board_distribution": dict(sorted(board_dist.items())),
            "total_seal_amount": sum(seal_ratios),
            "avg_seal_amount": sum(seal_ratios) / len(seal_ratios) if seal_ratios else 0,
            "avg_turnover": sum(turnover_rates) / len(turnover_rates) if turnover_rates else 0,
        }

    def _board_tiers(self, stocks):
        """连板梯队分析 - 使用 limit_up_days"""
        tiers = defaultdict(list)
        for s in stocks:
            boards = s.get("limit_up_days", 1) or 1
            if boards == 1:
                tiers["首板"].append(s.get("name", s.get("code", "")))
            elif boards == 2:
                tiers["二板"].append(s.get("name", s.get("code", "")))
            elif boards == 3:
                tiers["三板"].append(s.get("name", s.get("code", "")))
            elif 4 <= boards <= 6:
                tiers["高标"].append({
                    "name": s.get("name", ""),
                    "code": s.get("code", ""),
                    "boards": boards,
                    "seal_ratio": s.get("seal_ratio", 0),
                })
            elif boards >= 7:
                tiers["超高标"].append({
                    "name": s.get("name", ""),
                    "code": s.get("code", ""),
                    "boards": boards,
                    "seal_ratio": s.get("seal_ratio", 0),
                })
        
        result = {}
        for tier, items in tiers.items():
            if tier in ("首板", "二板", "三板"):
                result[tier] = {"count": len(items), "names": items[:10]}
            else:
                result[tier] = {"count": len(items), "stocks": items}
        return result

    def _seal_quality(self, stocks):
        """封板质量分析 - 使用 seal_ratio 替代 seal_amount"""
        strong = []
        medium = []
        weak = []
        one_char = 0
        t_board = 0
        exchange = 0

        for s in stocks:
            seal = s.get("seal_ratio", 0) or 0
            turnover = s.get("turnover_rate", 0) or 0
            style = (s.get("seal_style", "") or "").strip()
            
            if "一字" in style:
                one_char += 1
            elif "T" in style.upper():
                t_board += 1
            elif "换手" in style:
                exchange += 1

            if seal >= 0.05 and turnover <= 5.0:
                strong.append(s)
            elif seal >= 0.02:
                medium.append(s)
            else:
                weak.append(s)

        total = len(stocks)
        return {
            "strong_seal": len(strong),
            "medium_seal": len(medium),
            "weak_seal": len(weak),
            "strong_ratio": len(strong) / total if total else 0,
            "one_char_count": one_char,
            "t_board_count": t_board,
            "exchange_count": exchange,
            "quality_score": self._calc_quality_score(strong, medium, weak, total),
        }

    def _calc_quality_score(self, strong, medium, weak, total):
        if total == 0:
            return 0
        score = (len(strong) * 1.0 + len(medium) * 0.6 + len(weak) * 0.2) / total * 100
        return round(score, 1)

    def _concept_heat(self, date_str, stocks):
        concept_stats = self.db.get_concept_statistics(date_str)
        if concept_stats:
            concept_data = sorted([dict(c) for c in concept_stats],
                                  key=lambda x: x.get("count", 0), reverse=True)
            if concept_data:
                return {
                    "top_concepts": concept_data[:10],
                    "total_concepts": len(concept_data),
                    "hot_concepts": [c for c in concept_data if c.get("count", 0) >= 5],
                    "emerging_concepts": [c for c in concept_data if 3 <= c.get("count", 0) < 5],
                    "source": "concept_statistics",
                }

        xgb_concepts = self.db.get_xgb_concepts_by_date(date_str)
        if xgb_concepts:
            concept_data = [{"concept": k, "count": v}
                            for k, v in sorted(xgb_concepts.items(), key=lambda x: x[1], reverse=True)]
            return {
                "top_concepts": concept_data[:10],
                "total_concepts": len(concept_data),
                "hot_concepts": [c for c in concept_data if c.get("count", 0) >= 5],
                "emerging_concepts": [c for c in concept_data if 3 <= c.get("count", 0) < 5],
                "source": "xgb_detail",
            }

        return {"top_concepts": [], "total_concepts": 0,
                "hot_concepts": [], "emerging_concepts": [],
                "source": "none"}

    def _calc_sentiment(self, stocks, date_str):
        if not stocks:
            return 0
        boards = [s.get("limit_up_days", 1) or 1 for s in stocks]
        seal_ratios = [s.get("seal_ratio", 0) or 0 for s in stocks]
        
        count = len(stocks)
        count_score = min(count / 100 * 30, 30)
        max_b = max(boards)
        board_score = min(max_b / 10 * 20, 20)
        total_seal = sum(seal_ratios)
        seal_score = min(total_seal * 200 * 20, 20)
        high_count = sum(1 for b in boards if b >= 3)
        high_score = min(high_count / 10 * 15, 15)
        unique_boards = len(set(boards))
        breadth_score = min(unique_boards / 6 * 15, 15)
        total = count_score + board_score + seal_score + high_score + breadth_score
        return round(total, 1)

    def _continuation_analysis(self, date_str, stocks):
        all_dates = self.db.get_all_dates()
        current_idx = all_dates.index(date_str) if date_str in all_dates else -1
        if current_idx <= 0:
            return {"continuation_rate": 0, "continuation_stocks": [], "analysis": "无前日数据"}
        prev_date = all_dates[current_idx - 1]
        prev_stocks = self.db.get_limit_up_data(prev_date)
        if not prev_stocks:
            return {"continuation_rate": 0, "continuation_stocks": [], "analysis": "前日无数据"}
        prev_codes = set(dict(s).get("code", "") for s in prev_stocks)
        curr_codes = set(s.get("code", "") for s in stocks)
        continued = prev_codes & curr_codes
        continuation_rate = len(continued) / len(prev_codes) if prev_codes else 0
        cont_stocks = []
        for s in stocks:
            code = s.get("code", "")
            boards = s.get("limit_up_days", 1) or 1
            if code in continued and boards >= 2:
                cont_stocks.append({
                    "code": code,
                    "name": s.get("name", ""),
                    "boards": boards,
                })
        return {
            "continuation_rate": round(continuation_rate * 100, 1),
            "continuation_count": len(continued),
            "continuation_stocks": cont_stocks,
            "prev_date": prev_date,
            "prev_count": len(prev_codes),
            "analysis": f"前日{len(prev_codes)}只涨停中，{len(continued)}只继续涨停，晋级率{round(continuation_rate*100,1)}%",
        }

    def _compare_with_previous(self, date_str, stocks):
        all_dates = self.db.get_all_dates()
        current_idx = all_dates.index(date_str) if date_str in all_dates else -1
        if current_idx <= 0:
            return {"analysis": "无前日数据对比"}
        prev_date = all_dates[current_idx - 1]
        prev_stocks = self.db.get_limit_up_data(prev_date)
        prev_stocks = [dict(s) for s in prev_stocks]
        if not prev_stocks:
            return {"analysis": "前日无数据"}
        curr_count = len(stocks)
        prev_count = len(prev_stocks)
        curr_max = max(s.get("limit_up_days", 1) or 1 for s in stocks)
        prev_max = max(s.get("limit_up_days", 1) or 1 for s in prev_stocks)
        curr_seal = sum(s.get("seal_ratio", 0) or 0 for s in stocks)
        prev_seal = sum(s.get("seal_ratio", 0) or 0 for s in prev_stocks)
        return {
            "count_change": curr_count - prev_count,
            "count_change_pct": round((curr_count - prev_count) / prev_count * 100, 1) if prev_count else 0,
            "max_board_change": curr_max - prev_max,
            "seal_change": round(curr_seal - prev_seal, 2),
            "prev_date": prev_date,
            "analysis": (
                f"涨停数{curr_count}家(前日{prev_count}家，{'增' if curr_count >= prev_count else '减'}"
                f"{abs(curr_count - prev_count)}家)；"
                f"最高板{curr_max}板(前日{prev_max}板)；"
                f"封单总额{curr_seal:.1f}(前日{prev_seal:.1f})"
            ),
        }

    def generate_snapshot(self, analysis_result):
        if not analysis_result:
            return None
        basic = analysis_result.get("basic_stats", {})
        concept = analysis_result.get("concept_heat", {})
        top_concepts = concept.get("top_concepts", [])
        smash_info = analysis_result.get("smash_analysis", {})
        return {
            "date": analysis_result["date"],
            "limit_up_count": basic.get("total_count", 0),
            "max_continuous_boards": basic.get("max_boards", 0),
            "avg_seal_amount": basic.get("avg_seal_amount", 0),
            "avg_turnover_rate": basic.get("avg_turnover", 0),
            "main_concept": top_concepts[0].get("concept", "") if top_concepts else "",
            "main_concept_count": top_concepts[0].get("count", 0) if top_concepts else 0,
            "sentiment_score": analysis_result.get("sentiment_score", 0),
            "cycle_phase": smash_info.get("cycle_phase_by_smash", ""),
            "board_distribution": basic.get("board_distribution", {}),
        }