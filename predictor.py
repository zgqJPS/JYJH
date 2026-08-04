"""
predictor.py - 预测引擎 (v2.0 升级版)
基于当前市场状态、历史数据和知识库，生成多维度预测。
每个预测附带置信度。砸盘系数作为核心预测因子，权重最高。
统一使用 xgt_limit_up_detail 表的字段：limit_up_days, seal_ratio
市场周期参考 CycleModel 的4阶段作为额外证据
"""
import logging
import math
from collections import Counter, defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 情绪周期状态引擎
# ─────────────────────────────────────────────────────────────

class SentimentStateEngine:
    """
    基于多维观测的市场情绪周期状态识别引擎。
    将市场划分为5个隐藏状态：冰点、蓄力、发酵、高潮、崩塌
    利用砸盘系数趋势、炸板率、涨停数量等观测变量推断当前状态。
    同时参考 CycleModel 的结果作为额外证据。
    """

    STATES = ['ICEPOINT', 'STARTUP', 'MAIN_RISE', 'CLIMAX', 'EBB']
    STATE_CN = {
        'ICEPOINT': '冰点期',
        'STARTUP': '蓄力期',
        'MAIN_RISE': '发酵期',
        'CLIMAX': '高潮期',
        'EBB': '崩塌期',
    }

    TRANSITION_MATRIX = {
        'ICEPOINT':  {'ICEPOINT': 0.45, 'STARTUP': 0.35, 'MAIN_RISE': 0.10, 'CLIMAX': 0.05, 'EBB': 0.05},
        'STARTUP':   {'ICEPOINT': 0.15, 'STARTUP': 0.30, 'MAIN_RISE': 0.35, 'CLIMAX': 0.10, 'EBB': 0.10},
        'MAIN_RISE': {'ICEPOINT': 0.05, 'STARTUP': 0.10, 'MAIN_RISE': 0.25, 'CLIMAX': 0.35, 'EBB': 0.25},
        'CLIMAX':    {'ICEPOINT': 0.05, 'STARTUP': 0.10, 'MAIN_RISE': 0.15, 'CLIMAX': 0.20, 'EBB': 0.50},
        'EBB':       {'ICEPOINT': 0.35, 'STARTUP': 0.25, 'MAIN_RISE': 0.10, 'CLIMAX': 0.05, 'EBB': 0.25},
    }

    def __init__(self, db):
        self.db = db
        self._history_cache = {}
        # 获取数据库路径
        self.db_path = getattr(db, 'db_path', None)

    def get_recent_smash_data(self, date_str, days=30):
        """获取最近N天的砸盘系数数据"""
        cache_key = f"smash_{date_str}_{days}"
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]

        result = []
        try:
            all_dates = self.db.get_all_dates()
            if date_str not in all_dates:
                return []
            current_idx = all_dates.index(date_str)
            start_idx = max(0, current_idx - days + 1)
            recent_dates = all_dates[start_idx:current_idx + 1]

            for d in recent_dates:
                try:
                    rows = self.db.conn.execute(
                        "SELECT trade_date, smash_coefficient, open_rate "
                        "FROM smash_coefficients WHERE trade_date = ?", (d,)
                    ).fetchall()
                    if rows:
                        row = dict(rows[0])
                        result.append({
                            'date': d,
                            'smash': row.get('smash_coefficient'),
                            'open_rate': row.get('open_rate'),
                        })
                        continue
                except Exception:
                    pass

                try:
                    rows = self.db.conn.execute(
                        "SELECT date, explosion_rate, limit_up_count FROM xgt_daily_summary WHERE date = ?",
                        (d,)
                    ).fetchall()
                    if rows:
                        row = dict(rows[0])
                        er = row.get('explosion_rate') or 0
                        lu = row.get('limit_up_count') or 50
                        estimated_smash = 3.0 + er * 10 - min(lu / 20, 3.0)
                        result.append({'date': d, 'smash': estimated_smash, 'open_rate': er})
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"获取砸盘数据异常: {e}")

        self._history_cache[cache_key] = result
        return result

    def get_recent_daily_data(self, date_str, days=10):
        """获取最近N天的每日汇总数据"""
        cache_key = f"daily_{date_str}_{days}"
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]

        result = []
        try:
            all_dates = self.db.get_all_dates()
            if date_str not in all_dates:
                return []
            current_idx = all_dates.index(date_str)
            start_idx = max(0, current_idx - days + 1)
            recent_dates = all_dates[start_idx:current_idx + 1]

            for d in recent_dates:
                try:
                    rows = self.db.conn.execute(
                        "SELECT date, limit_up_count, explosion_rate, max_continuous_boards, "
                        "market_heat, yesterday_limit_up_avg_change "
                        "FROM xgt_daily_summary WHERE date = ?", (d,)
                    ).fetchall()
                    if rows:
                        result.append(dict(rows[0]))
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"获取每日数据异常: {e}")

        self._history_cache[cache_key] = result
        return result

    def infer_state(self, date_str):
        """
        推断市场情绪状态 - 结合 CycleModel 结果作为额外证据
        """
        # 先获取 CycleModel 的结果
        cycle_phase = ''
        try:
            from cycle_model import CycleModel
            cycle_model = CycleModel(self.db_path)
            phase_result = cycle_model.detect_phase(date_str)
            cycle_phase = phase_result.get('phase', '')
        except Exception as e:
            logger.warning(f"CycleModel 调用失败: {e}")

        # 原有逻辑
        smash_data = self.get_recent_smash_data(date_str, days=10)
        daily_data = self.get_recent_daily_data(date_str, days=10)

        if not daily_data and not smash_data:
            logger.warning("无历史数据，默认返回发酵期")
            return {
                'state': 'MAIN_RISE',
                'probabilities': {s: 0.2 for s in self.STATES},
                'confidence': 0.2,
                'evidence': ['数据不足，默认发酵期'],
            }

        evidence = []
        scores = {s: 0.0 for s in self.STATES}

        if smash_data:
            recent_smash = [s['smash'] for s in smash_data if s.get('smash') is not None]
            if recent_smash:
                current_smash = recent_smash[-1]
                smash_3ma = sum(recent_smash[-3:]) / min(len(recent_smash), 3)
                smash_trend = current_smash - smash_3ma

                if current_smash >= 7.0:
                    scores['EBB'] += 3.0
                    evidence.append(f"砸盘系数{current_smash:.1f}≥7.0→崩塌信号")
                elif current_smash >= 4.5:
                    scores['CLIMAX'] += 2.0
                    evidence.append(f"砸盘系数{current_smash:.1f}偏高→高潮信号")
                elif current_smash >= 3.0:
                    scores['MAIN_RISE'] += 2.0
                    evidence.append(f"砸盘系数{current_smash:.1f}中等→发酵信号")
                elif current_smash >= 1.5:
                    scores['STARTUP'] += 2.0
                    evidence.append(f"砸盘系数{current_smash:.1f}偏低→蓄力信号")
                else:
                    scores['ICEPOINT'] += 2.0
                    evidence.append(f"砸盘系数{current_smash:.1f}<1.5→冰点信号")

                if len(recent_smash) >= 3:
                    if smash_trend > 1.0:
                        scores['EBB'] += 1.5
                        scores['CLIMAX'] += 0.5
                        evidence.append(f"砸盘趋势上升(+{smash_trend:.1f})→恶化信号")
                    elif smash_trend < -1.0:
                        scores['ICEPOINT'] += 0.5
                        scores['STARTUP'] += 1.5
                        evidence.append(f"砸盘趋势下降({smash_trend:.1f})→改善信号")

                    if len(recent_smash) >= 4:
                        declining_days = sum(
                            1 for i in range(-3, 0)
                            if recent_smash[i] < recent_smash[i - 1]
                        )
                        if declining_days >= 3:
                            scores['STARTUP'] += 1.0
                            scores['MAIN_RISE'] += 0.5
                            evidence.append("砸盘系数连续3天下降→企稳反弹")

        if daily_data:
            limit_counts = [d.get('limit_up_count', 0) for d in daily_data if d.get('limit_up_count')]
            if limit_counts:
                avg_count = sum(limit_counts) / len(limit_counts)
                current_count = limit_counts[-1]

                if current_count >= 80:
                    scores['CLIMAX'] += 2.0
                    evidence.append(f"涨停{current_count}只≥80→高潮信号")
                elif current_count >= 55:
                    scores['MAIN_RISE'] += 1.5
                    evidence.append(f"涨停{current_count}只≥55→发酵信号")
                elif current_count >= 35:
                    scores['STARTUP'] += 1.5
                    evidence.append(f"涨停{current_count}只→蓄力信号")
                else:
                    scores['ICEPOINT'] += 2.0
                    evidence.append(f"涨停{current_count}只<35→冰点信号")

        if daily_data:
            explosion_rates = [d.get('explosion_rate', 0) for d in daily_data if d.get('explosion_rate') is not None]
            if explosion_rates:
                avg_er = sum(explosion_rates) / len(explosion_rates)
                current_er = explosion_rates[-1]

                if current_er > 0.50:
                    scores['EBB'] += 2.5
                    scores['ICEPOINT'] += 0.5
                    evidence.append(f"炸板率{current_er:.0%}>50%→崩塌/冰点信号")
                elif current_er > 0.35:
                    scores['EBB'] += 1.5
                    evidence.append(f"炸板率{current_er:.0%}>35%→退潮信号")
                elif current_er < 0.15:
                    scores['CLIMAX'] += 1.5
                    evidence.append(f"炸板率{current_er:.0%}<15%→高潮信号(低分歧)")
                elif current_er < 0.25:
                    scores['MAIN_RISE'] += 1.0
                    evidence.append(f"炸板率{current_er:.0%}适中→发酵信号")

        if daily_data:
            max_boards_list = [d.get('max_continuous_boards', 0) for d in daily_data if d.get('max_continuous_boards')]
            if max_boards_list:
                current_max = max_boards_list[-1]
                if current_max >= 7:
                    scores['CLIMAX'] += 1.5
                    evidence.append(f"最高{current_max}板→高潮信号(高度拓展)")
                elif current_max >= 5:
                    scores['MAIN_RISE'] += 1.0
                    evidence.append(f"最高{current_max}板→发酵信号")
                elif current_max <= 2:
                    scores['ICEPOINT'] += 1.5
                    evidence.append(f"最高{current_max}板→冰点信号(高度压缩)")

        # ============ CycleModel 结果作为额外证据（仅使用新4阶段） ============
        if cycle_phase:
            phase_map = {
                '冰点酝酿期': 'ICEPOINT',
                '蓄力爬升期': 'STARTUP',
                '爆发高潮期': 'CLIMAX',
                '崩塌退潮期': 'EBB',
            }
            mapped = phase_map.get(cycle_phase, '')
            if mapped:
                scores[mapped] += 2.0
                evidence.append(f"CycleModel 判断为 {cycle_phase} → 映射到 {mapped}")

        # 最终打分选择
        total_score = sum(scores.values())
        if total_score > 0:
            probabilities = {s: round(v / total_score, 3) for s, v in scores.items()}
        else:
            probabilities = {s: 0.2 for s in self.STATES}

        best_state = max(probabilities, key=probabilities.get)
        confidence = round(probabilities[best_state], 2)

        logger.info(f"[{date_str}] 状态引擎推断: {self.STATE_CN.get(best_state, best_state)} "
                    f"(置信度{confidence:.0%}), 证据: {'; '.join(evidence[:5])}")

        return {
            'state': best_state,
            'probabilities': probabilities,
            'confidence': confidence,
            'evidence': evidence,
        }


# ─────────────────────────────────────────────────────────────
# 预测引擎主类
# ─────────────────────────────────────────────────────────────

class Predictor:
    def __init__(self, db, knowledge_base=None):
        self.db = db
        self.kb = knowledge_base
        self.weights = self._load_weights()
        from smash_coefficient import SmashCoefficientCalculator
        self.smash_calc = SmashCoefficientCalculator(db)
        self.state_engine = SentimentStateEngine(db)

    def _load_weights(self):
        weights = {}
        rows = self.db.get_all_weights()
        for r in rows:
            r = dict(r)
            weights[r["factor_name"]] = r["weight"]
        return weights

    def predict_next_day(self, date_str, analysis_result, pattern_result):
        if not analysis_result:
            logger.warning("无分析结果，无法生成预测")
            return {}

        predictions = {}

        state_info = self.state_engine.infer_state(date_str)
        current_state = state_info['state']
        state_probs = state_info['probabilities']
        logger.info(f"[{date_str}] 情绪周期状态: {current_state} (置信度{state_info['confidence']:.0%})")

        history = self._get_recent_history(date_str, days=10)
        long_history = self.state_engine.get_recent_daily_data(date_str, days=30)

        predictions["limit_up_count"] = self._predict_limit_up_count(
            date_str, analysis_result, history, state_info, long_history)

        predictions["max_continuous_boards"] = self._predict_max_boards(
            date_str, analysis_result, history, state_info)

        predictions["main_concept"] = self._predict_main_concept(
            date_str, analysis_result, pattern_result, history, state_info, long_history)

        predictions["sentiment_direction"] = self._predict_sentiment(
            date_str, analysis_result, pattern_result, history, state_info, long_history)

        predictions["smash_prediction"] = self._predict_by_smash(
            date_str, analysis_result, pattern_result, history)

        try:
            predictions["operation_advice"] = self._generate_advice(
                date_str, analysis_result, pattern_result, predictions, state_info, long_history)
        except Exception as e:
            logger.error(f"生成操作建议失败: {e}", exc_info=True)
            predictions["operation_advice"] = {
                "advice": "观望",
                "confidence": 0.3,
                "detail": "建议暂时观望",
                "reason": "操作建议生成异常"
            }

        self._save_predictions(date_str, predictions)
        return predictions

    def _get_recent_history(self, date_str, days=10):
        """
        获取最近N天的历史数据
        使用 limit_up_days 替代 continuous_boards
        """
        all_dates = self.db.get_all_dates()
        if date_str not in all_dates:
            return []

        current_idx = all_dates.index(date_str)
        start_idx = max(0, current_idx - days + 1)
        recent_dates = all_dates[start_idx:current_idx + 1]

        history = []
        for d in recent_dates:
            snapshot = self.db.get_daily_snapshots(start_date=d, end_date=d)
            if snapshot:
                history.append(dict(snapshot[0]))
            else:
                stocks = self.db.get_limit_up_data(d)
                if stocks:
                    stocks = [dict(s) for s in stocks]
                    boards = [s.get("limit_up_days", 1) or 1 for s in stocks]
                    history.append({
                        "date": d,
                        "limit_up_count": len(stocks),
                        "max_continuous_boards": max(boards),
                        "sentiment_score": 50,
                    })
        return history

    # ===== 预测方法 =====

    def _predict_limit_up_count(self, date_str, analysis, history, state_info, long_history):
        current_state = state_info['state']
        state_cn = SentimentStateEngine.STATE_CN.get(current_state, current_state)

        if not history:
            return {"predicted": 50, "confidence": 0.3, "range": (30, 70),
                    "reason": "数据不足，使用默认值"}

        counts = [h.get("limit_up_count", 50) for h in history]
        today_count = analysis.get("basic_stats", {}).get("total_count", counts[-1])

        recent_5 = counts[-5:] if len(counts) >= 5 else counts
        avg_5 = sum(recent_5) / len(recent_5)

        if len(counts) >= 3:
            recent_trend = counts[-1] - counts[-3]
            momentum = today_count + recent_trend * 0.5
        else:
            momentum = today_count

        state_modifier = {
            'ICEPOINT': 0.80,
            'STARTUP': 0.90,
            'MAIN_RISE': 1.05,
            'CLIMAX': 1.20,
            'EBB': 0.75,
        }
        modifier = state_modifier.get(current_state, 1.0)
        cycle_predicted = today_count * modifier

        smash_data = self.state_engine.get_recent_smash_data(date_str, days=10)
        smash_adj = 1.0
        if smash_data:
            recent_smash = [s['smash'] for s in smash_data if s.get('smash') is not None]
            if len(recent_smash) >= 3:
                smash_trend = recent_smash[-1] - sum(recent_smash[-3:]) / 3
                if smash_trend > 1.0:
                    smash_adj = 0.90
                elif smash_trend < -1.0:
                    smash_adj = 1.10

        w_breadth = self.weights.get("breadth_factor", 0.5)
        w_momentum = self.weights.get("momentum_factor", 0.5)
        w_cycle = self.weights.get("cycle_factor", 0.5)

        total_w = w_breadth + w_momentum + w_cycle
        predicted = (
            avg_5 * w_breadth +
            momentum * w_momentum +
            cycle_predicted * w_cycle
        ) / total_w
        predicted *= smash_adj
        predicted = max(10, min(150, round(predicted)))

        state_inertia = {
            'ICEPOINT': 0.69, 'STARTUP': 0.30,
            'MAIN_RISE': 0.35, 'CLIMAX': 0.71, 'EBB': 0.35,
        }
        inertia = state_inertia.get(current_state, 0.5)

        if inertia > 0.60:
            std = max(5, today_count * 0.20)
        else:
            variance = sum((c - avg_5) ** 2 for c in recent_5) / len(recent_5) if recent_5 else 100
            std = max(8, variance ** 0.5)

        pred_range = (max(5, round(predicted - std)), round(predicted + std))
        stability = max(0.2, 1 - std / avg_5) if avg_5 > 0 else 0.3
        confidence = round(min(0.8, stability * 0.7 + 0.1 + inertia * 0.1), 2)

        reason = (
            f"基于近{len(recent_5)}日均值{avg_5:.0f}，"
            f"当前处于{state_cn}(修正系数{modifier:.0%})，"
            f"砸盘修正{smash_adj:.0%}"
        )

        logger.info(f"[涨停数量预测] {reason} → 预测{predicted}只")

        return {
            "predicted": predicted,
            "confidence": confidence,
            "range": pred_range,
            "reason": reason,
        }

    def _predict_max_boards(self, date_str, analysis, history, state_info):
        current_state = state_info['state']
        state_cn = SentimentStateEngine.STATE_CN.get(current_state, current_state)

        if not history:
            return {"predicted": 3, "confidence": 0.3, "reason": "数据不足"}

        today_max = analysis.get("basic_stats", {}).get("max_boards", 3)
        sentiment = analysis.get("sentiment_score", 50)

        max_boards_hist = [h.get("max_continuous_boards", 3) for h in history]
        avg_max = sum(max_boards_hist) / len(max_boards_hist) if max_boards_hist else 3

        if current_state == 'CLIMAX':
            predicted = min(today_max + 1, 15)
        elif current_state == 'MAIN_RISE':
            predicted = today_max if today_max >= avg_max else round(avg_max)
        elif current_state == 'STARTUP':
            predicted = max(2, min(today_max, round(avg_max)))
        elif current_state == 'ICEPOINT':
            predicted = max(2, min(today_max - 1, round(avg_max)))
        elif current_state == 'EBB':
            predicted = max(2, today_max - 2)
        else:
            if sentiment > 65:
                predicted = min(today_max + 1, 15)
            elif sentiment > 50:
                predicted = today_max
            elif sentiment > 35:
                predicted = max(2, today_max - 1)
            else:
                predicted = max(2, min(today_max, round(avg_max)))

        dragon_w = self.weights.get("dragon_factor", 0.5)
        if dragon_w > 0.7 and today_max >= 5:
            predicted = min(today_max + 1, 15)

        confidence = round(min(0.7, 0.3 + dragon_w * 0.4), 2)

        return {
            "predicted": predicted,
            "confidence": confidence,
            "reason": f"当前最高{today_max}板，周期{state_cn}，历史均高{avg_max:.1f}板",
        }

    def _predict_main_concept(self, date_str, analysis, pattern, history, state_info, long_history):
        concept_heat = analysis.get("concept_heat", {})
        top_concepts = concept_heat.get("top_concepts", [])
        hot_concepts = concept_heat.get("hot_concepts", [])
        concept_source = concept_heat.get("source", "none")

        rotation = pattern.get("concept_rotation", {}) if pattern else {}
        rotation_pattern = rotation.get("rotation_pattern", "")
        persisted = rotation.get("persisted_concepts", [])

        if not top_concepts:
            try:
                xgb_concepts = self.db.get_xgb_concepts_by_date(date_str)
                if xgb_concepts:
                    top_concepts = [{"concept": k, "count": v}
                                    for k, v in sorted(xgb_concepts.items(),
                                                       key=lambda x: x[1], reverse=True)][:10]
                    hot_concepts = [c for c in top_concepts if c.get("count", 0) >= 5]
                    concept_source = "xgb_direct"
            except Exception:
                pass

        if not top_concepts:
            return {"predicted": "未知", "confidence": 0.2, "reason": "无概念数据（选股宝数据缺失）"}

        concept_history = self._get_concept_history(date_str, days=30)

        heat_w = self.weights.get("concept_heat_factor", 0.5)
        concept_scores = {}

        for c in top_concepts:
            name = c.get("concept", "")
            count = c.get("count", 0)
            score = count

            if name in persisted:
                score *= 1.3

            if rotation_pattern == "主线明确":
                score *= 1.2

            if name in concept_history:
                ch = concept_history[name]
                days_since_peak = ch.get('days_since_peak', 999)
                recent_trend = ch.get('recent_trend', 0)

                if days_since_peak <= 2:
                    score *= 1.15
                elif 3 <= days_since_peak <= 5:
                    score *= 1.10
                elif 6 <= days_since_peak <= 7:
                    score *= 1.20
                else:
                    score *= 0.80

                if ch.get('prev_count', 0) <= 1 and count >= 3:
                    score *= 1.25

            concept_scores[name] = score

        if long_history and concept_history:
            today_concepts = self._get_today_concepts(date_str)
            if today_concepts:
                total_count = sum(today_concepts.values())
                if total_count > 0:
                    top1_count = max(today_concepts.values())
                    concentration = top1_count / total_count
                    if concentration > 0.25:
                        logger.info(f"[概念预测] 集中度{concentration:.0%}，主线明确")

        sorted_concepts = sorted(concept_scores.items(), key=lambda x: x[1], reverse=True)
        predicted = sorted_concepts[0][0] if sorted_concepts else top_concepts[0].get("concept", "")
        top3 = [c[0] for c in sorted_concepts[:3]]

        confidence = round(min(0.75, heat_w + 0.1 * len(persisted)), 2)

        logger.info(f"[概念预测] 预测主线: {predicted}, TOP3: {top3}")

        return {
            "predicted": predicted,
            "confidence": confidence,
            "top_candidates": top3,
            "reason": f"概念热度+轮动周期(数据源:{concept_source}): {', '.join(f'{c[0]}({c[1]}分)' for c in sorted_concepts[:3])}",
        }

    def _get_concept_history(self, date_str, days=30):
        try:
            all_dates = self.db.get_all_dates()
            if date_str not in all_dates:
                return {}
            current_idx = all_dates.index(date_str)
            start_idx = max(0, current_idx - days + 1)
            recent_dates = all_dates[start_idx:current_idx + 1]

            daily_concepts = {}
            for d in recent_dates:
                try:
                    rows = self.db.conn.execute(
                        "SELECT concept, count FROM concept_statistics WHERE date = ? ORDER BY count DESC",
                        (d,)
                    ).fetchall()
                    if rows:
                        daily_concepts[d] = {r['concept']: r['count'] for r in rows}
                except Exception:
                    pass

            if not daily_concepts:
                return {}

            all_concept_names = set()
            for day_data in daily_concepts.values():
                all_concept_names.update(day_data.keys())

            result = {}
            sorted_dates = sorted(daily_concepts.keys())

            for concept in all_concept_names:
                counts_by_date = []
                for d in sorted_dates:
                    counts_by_date.append(daily_concepts[d].get(concept, 0))

                peak_idx = counts_by_date.index(max(counts_by_date)) if counts_by_date else 0
                days_since_peak = len(counts_by_date) - 1 - peak_idx

                recent_3 = sum(counts_by_date[-3:]) if len(counts_by_date) >= 3 else sum(counts_by_date)
                prev_3 = sum(counts_by_date[-6:-3]) if len(counts_by_date) >= 6 else sum(counts_by_date[:-3]) if len(counts_by_date) > 3 else recent_3

                result[concept] = {
                    'days_since_peak': days_since_peak,
                    'recent_trend': recent_3 - prev_3,
                    'prev_count': prev_3,
                    'total_count': sum(counts_by_date),
                }

            return result
        except Exception as e:
            logger.warning(f"概念历史获取异常: {e}")
            return {}

    def _get_today_concepts(self, date_str):
        try:
            rows = self.db.conn.execute(
                "SELECT concept, count FROM concept_statistics WHERE date = ?",
                (date_str,)
            ).fetchall()
            return {r['concept']: r['count'] for r in rows} if rows else {}
        except Exception:
            return {}

    def _predict_sentiment(self, date_str, analysis, pattern, history, state_info, long_history):
        current_state = state_info['state']
        state_cn = SentimentStateEngine.STATE_CN.get(current_state, current_state)
        state_probs = state_info['probabilities']

        signals = {}

        daily_data = long_history if long_history else self.state_engine.get_recent_daily_data(date_str, days=30)
        if daily_data:
            explosion_rates = [d.get('explosion_rate', 0) for d in daily_data
                               if d.get('explosion_rate') is not None]
            if len(explosion_rates) >= 5:
                er_3ma = sum(explosion_rates[-3:]) / 3
                er_5ma = sum(explosion_rates[-5:]) / 5
                er_mean = sum(explosion_rates) / len(explosion_rates)
                er_std = (sum((e - er_mean) ** 2 for e in explosion_rates) / len(explosion_rates)) ** 0.5

                if er_std > 0:
                    z_score = (explosion_rates[-1] - er_mean) / er_std
                else:
                    z_score = 0

                if z_score > 1.5:
                    signals['broken_rate'] = 0.7
                    logger.info(f"[情绪预测] 炸板率Z={z_score:.1f}>1.5，均值回归看多")
                elif z_score < -1.5:
                    signals['broken_rate'] = -0.5
                    logger.info(f"[情绪预测] 炸板率Z={z_score:.1f}<-1.5，一致性过高警惕见顶")
                else:
                    if er_3ma < er_5ma:
                        signals['broken_rate'] = 0.2
                    elif er_3ma > er_5ma:
                        signals['broken_rate'] = -0.2
                    else:
                        signals['broken_rate'] = 0.0
            elif len(explosion_rates) >= 3:
                er_3ma = sum(explosion_rates[-3:]) / 3
                if er_3ma > 0.40:
                    signals['broken_rate'] = 0.5
                elif er_3ma < 0.15:
                    signals['broken_rate'] = -0.3
                else:
                    signals['broken_rate'] = 0.0
        else:
            signals['broken_rate'] = 0.0

        smash_data = self.state_engine.get_recent_smash_data(date_str, days=10)
        if smash_data:
            recent_smash = [s['smash'] for s in smash_data if s.get('smash') is not None]
            if len(recent_smash) >= 3:
                smash_3ma = sum(recent_smash[-3:]) / 3
                smash_delta = recent_smash[-1] - smash_3ma

                if smash_delta < -1.0:
                    signals['smash_trend'] = 0.6
                elif smash_delta > 1.0:
                    signals['smash_trend'] = -0.6
                else:
                    signals['smash_trend'] = 0.0

                if len(recent_smash) >= 4:
                    declining = sum(1 for i in range(-3, 0)
                                    if recent_smash[i] < recent_smash[i - 1])
                    if declining >= 3:
                        signals['smash_trend'] = max(signals['smash_trend'], 0.5)
            else:
                signals['smash_trend'] = 0.0
        else:
            signals['smash_trend'] = 0.0

        if daily_data:
            premiums = [d.get('yesterday_limit_up_avg_change', 0) for d in daily_data
                        if d.get('yesterday_limit_up_avg_change') is not None]
            if len(premiums) >= 3:
                premium_trend = premiums[-1] - sum(premiums[-3:]) / 3
                signals['premium'] = max(-1.0, min(1.0, premium_trend / 2.0))
            else:
                signals['premium'] = 0.0
        else:
            signals['premium'] = 0.0

        if daily_data:
            max_boards_list = [d.get('max_continuous_boards', 0) for d in daily_data
                               if d.get('max_continuous_boards')]
            if len(max_boards_list) >= 3:
                if max_boards_list[-1] > max_boards_list[-3]:
                    signals['board_height'] = 0.4
                elif max_boards_list[-1] < max_boards_list[-3]:
                    signals['board_height'] = -0.3
                else:
                    signals['board_height'] = 0.0
            else:
                signals['board_height'] = 0.0
        else:
            signals['board_height'] = 0.0

        up_prob = state_probs.get('MAIN_RISE', 0) + state_probs.get('CLIMAX', 0)
        down_prob = state_probs.get('ICEPOINT', 0) + state_probs.get('EBB', 0)
        signals['state_direction'] = up_prob - down_prob

        composite = (
            0.25 * signals.get('broken_rate', 0) +
            0.20 * signals.get('smash_trend', 0) +
            0.15 * signals.get('premium', 0) +
            0.15 * signals.get('board_height', 0) +
            0.25 * signals.get('state_direction', 0)
        )

        if composite > 0.25:
            direction = "升温"
        elif composite < -0.25:
            direction = "降温"
        else:
            direction = "震荡"

        confidence = round(min(0.7, 0.3 + abs(composite) / 2), 2)

        signal_reasons = []
        if abs(signals.get('broken_rate', 0)) > 0.3:
            signal_reasons.append(f"炸板率信号{'看多' if signals['broken_rate'] > 0 else '看空'}")
        if abs(signals.get('smash_trend', 0)) > 0.3:
            signal_reasons.append(f"砸盘趋势{'改善' if signals['smash_trend'] > 0 else '恶化'}")
        if abs(signals.get('state_direction', 0)) > 0.2:
            signal_reasons.append(f"状态{'向上' if signals['state_direction'] > 0 else '向下'}转移")

        reason = (
            f"周期{state_cn}，综合信号{composite:+.2f}，"
            + ('；'.join(signal_reasons) if signal_reasons else '信号中性')
        )

        logger.info(f"[情绪预测] {direction}，{reason}")

        return {
            "predicted": direction,
            "confidence": confidence,
            "score": round(composite, 2),
            "reason": reason,
        }

    def _predict_by_smash(self, date_str, analysis, pattern, history):
        try:
            from config import SMASH_CONFIG

            smash_data = analysis.get("smash_analysis", {})
            smash_value = smash_data.get("smash_coefficient")
            trend = smash_data.get("trend", "未知")
            trend_values = smash_data.get("trend_values", [])

            if smash_value is None:
                return {
                    "predicted": "数据不足",
                    "confidence": 0.2,
                    "reason": "砸盘系数未计算，无法基于此预测",
                }

            low_threshold = SMASH_CONFIG.get("low_pressure_threshold", 4.0)
            high_threshold = SMASH_CONFIG.get("high_pressure_threshold", 7.0)
            prediction_weight = SMASH_CONFIG.get("prediction_weight", 0.35)

            if smash_value > high_threshold:
                base_direction = "降温"
                base_reason = f"砸盘系数{smash_value:.1f}偏高(>{high_threshold})，抛压沉重"
            elif smash_value < low_threshold:
                base_direction = "升温"
                base_reason = f"砸盘系数{smash_value:.1f}偏低(<{low_threshold})，做多氛围好"
            else:
                base_direction = "震荡"
                base_reason = f"砸盘系数{smash_value:.1f}处于正常区间"

            if trend == "上升":
                if base_direction != "降温":
                    base_direction = "降温"
                    base_reason += "；趋势上升，抛压加剧"
                else:
                    base_reason += "；且趋势持续上升"
            elif trend == "下降":
                if base_direction != "升温":
                    base_direction = "升温"
                    base_reason += "；趋势下降，抛压减轻"
                else:
                    base_reason += "；且趋势持续下降"

            confidence = round(min(0.8, prediction_weight + 0.3), 2)

            return {
                "predicted": base_direction,
                "confidence": confidence,
                "smash_value": smash_value,
                "trend": trend,
                "reason": base_reason,
            }

        except Exception as e:
            logger.error(f"砸盘系数预测异常: {e}")
            return {
                "predicted": "未知",
                "confidence": 0.2,
                "reason": f"砸盘系数预测异常: {e}",
            }

    def _generate_advice(self, date_str, analysis, pattern, predictions, state_info, long_history):
        current_state = state_info['state']
        state_cn = SentimentStateEngine.STATE_CN.get(current_state, current_state)

        sentiment = analysis.get("sentiment_score", 50)
        basic = analysis.get("basic_stats", {})
        seal_quality = analysis.get("seal_quality", {})
        quality_score = seal_quality.get("quality_score", 50)

        smash_data = analysis.get("smash_analysis", {})
        smash_value = smash_data.get("smash_coefficient")
        smash_signal = smash_data.get("signal", "未知")
        smash_trade_advice = smash_data.get("trade_advice", "")

        try:
            signal_strength = self._compute_signal_strength(
                date_str, analysis, predictions, state_info, long_history)
        except Exception as e:
            logger.warning(f"信号强度计算失败，使用默认值: {e}")
            signal_strength = 0.5

        if signal_strength >= 0.65:
            strength_bin = 'strong'
        elif signal_strength >= 0.45:
            strength_bin = 'medium'
        elif signal_strength >= 0.25:
            strength_bin = 'weak'
        else:
            strength_bin = 'none'

        ADVICE_MATRIX = {
            'ICEPOINT':  ['低吸试错', '观望',      '空仓观望',  '空仓观望'],
            'STARTUP':   ['积极进攻', '低吸试错',  '观望',      '观望'],
            'MAIN_RISE': ['积极进攻', '积极进攻',  '半仓持有',  '持有观望'],
            'CLIMAX':    ['逐步减仓', '持有观望',  '持有观望',  '防守'],
            'EBB':       ['空仓观望', '空仓观望',  '空仓观望',  '空仓观望'],
        }

        bin_index = {'strong': 0, 'medium': 1, 'weak': 2, 'none': 3}
        advice = ADVICE_MATRIX.get(current_state, ADVICE_MATRIX['MAIN_RISE'])[bin_index[strength_bin]]

        advice = self._safety_valve(advice, date_str, analysis, state_info, long_history)

        detail = self._build_advice_detail(advice, current_state, state_cn, signal_strength,
                                            smash_value, smash_trade_advice, predictions)

        confidence = round(min(0.75, 0.3 + signal_strength * 0.3 +
                               (0.1 if smash_value is not None else 0)), 2)

        reason_parts = [f"周期{state_cn}", f"信号强度{signal_strength:.0%}",
                        f"情绪分{sentiment:.0f}", f"封板质量{quality_score:.0f}分"]
        if smash_value is not None:
            reason_parts.append(f"砸盘系数{smash_value:.1f}({smash_signal})")
        reason = "，".join(reason_parts)

        logger.info(f"[操作建议] {advice}，{reason}")

        return {
            "advice": advice,
            "confidence": confidence,
            "detail": detail,
            "reason": reason,
        }

    def _compute_signal_strength(self, date_str, analysis, predictions, state_info, long_history):
        """
        计算综合信号强度（0-1标准化）
        """
        factors = {}

        try:
            today_concepts = self._get_today_concepts(date_str)
            if today_concepts:
                max_concept_count = max(today_concepts.values()) if today_concepts else 0
                factors['concept_explosion'] = min(1.0, max_concept_count / 10.0)
            else:
                factors['concept_explosion'] = 0.3
        except Exception:
            factors['concept_explosion'] = 0.3

        smash_data = self.state_engine.get_recent_smash_data(date_str, days=10)
        if smash_data:
            recent_smash = [s['smash'] for s in smash_data if s.get('smash') is not None]
            if len(recent_smash) >= 3:
                declining = sum(1 for i in range(-3, 0)
                                if recent_smash[i] < recent_smash[i - 1])
                factors['smash_declining'] = declining / 3.0
            else:
                factors['smash_declining'] = 0.5
        else:
            factors['smash_declining'] = 0.5

        if long_history:
            explosion_rates = [d.get('explosion_rate', 0) for d in long_history
                               if d.get('explosion_rate') is not None]
            if len(explosion_rates) >= 3:
                er_recent = sum(explosion_rates[-3:]) / 3
                factors['broken_rate_drop'] = max(0, 1.0 - er_recent / 0.40)
            else:
                factors['broken_rate_drop'] = 0.5
        else:
            factors['broken_rate_drop'] = 0.5

        sentiment_pred = predictions.get('sentiment_direction', {})
        if sentiment_pred.get('predicted') == '升温':
            factors['limit_up_trend'] = 0.8
        elif sentiment_pred.get('predicted') == '降温':
            factors['limit_up_trend'] = 0.2
        else:
            factors['limit_up_trend'] = 0.5

        basic = analysis.get("basic_stats", {})
        max_boards = basic.get("max_boards", 3)
        if max_boards >= 5:
            factors['consec_promotion'] = 0.8
        elif max_boards >= 3:
            factors['consec_promotion'] = 0.6
        else:
            factors['consec_promotion'] = 0.3

        if long_history:
            premiums = [d.get('yesterday_limit_up_avg_change', 0) for d in long_history
                        if d.get('yesterday_limit_up_avg_change') is not None]
            if len(premiums) >= 2:
                if premiums[-1] > 0:
                    factors['premium_recovering'] = min(1.0, 0.5 + premiums[-1] / 10)
                else:
                    factors['premium_recovering'] = max(0, 0.5 + premiums[-1] / 10)
            else:
                factors['premium_recovering'] = 0.5
        else:
            factors['premium_recovering'] = 0.5

        weights = {
            'concept_explosion': 0.25,
            'smash_declining': 0.20,
            'premium_recovering': 0.15,
            'limit_up_trend': 0.15,
            'broken_rate_drop': 0.15,
            'consec_promotion': 0.10,
        }

        total = sum(factors.get(k, 0.5) * v for k, v in weights.items())
        logger.info(f"[信号强度] 各因子: {', '.join(f'{k}={v:.2f}' for k, v in factors.items())} → 综合{total:.2f}")

        return total

    def _safety_valve(self, advice, date_str, analysis, state_info, long_history):
        if long_history:
            explosion_rates = [d.get('explosion_rate', 0) for d in long_history
                               if d.get('explosion_rate') is not None]
            current_er = explosion_rates[-1] if explosion_rates else 0
        else:
            current_er = 0

        current_state = state_info['state']

        if current_er > 0.50 and '积极' in advice:
            logger.warning(f"[安全阀] 炸板率{current_er:.0%}>50%，禁止积极进攻→降级为观望")
            return '观望'

        if current_state == 'EBB' and '积极' in advice:
            logger.warning(f"[安全阀] 崩塌期禁止积极进攻→降级为空仓观望")
            return '空仓观望'

        if current_state == 'ICEPOINT' and '积极' in advice:
            logger.warning(f"[安全阀] 冰点期限制→降级为低吸试错")
            return '低吸试错'

        return advice

    def _build_advice_detail(self, advice, state, state_cn, signal_strength,
                             smash_value, smash_trade_advice, predictions):
        detail_map = {
            '积极进攻': '市场处于进攻窗口，可适当提高仓位参与龙头和主线概念，关注3板以上确认标的',
            '低吸试错': '冰点期或有反转信号，轻仓试错首板或2板确认股，严格止损',
            '半仓持有': '市场中性偏强，持有为主，可半仓参与核心龙头',
            '持有观望': '市场方向不明，以持有为主，不追高不杀跌',
            '逐步减仓': '高潮末期信号，逐步降低仓位，锁定利润',
            '防守': '市场偏弱，降低仓位至2成以下，只做最强龙头',
            '观望': '信号不足，建议观望等待更明确的机会',
            '空仓观望': '市场极弱，建议空仓等待回暖信号',
        }
        detail = detail_map.get(advice, '建议观望')

        if smash_trade_advice:
            detail += f"；{smash_trade_advice}"

        sent_pred = predictions.get("sentiment_direction", {})
        if sent_pred.get("predicted") == "升温":
            detail += "；预测明日情绪升温，可适当提前布局"
        elif sent_pred.get("predicted") == "降温":
            detail += "；预测明日情绪降温，建议今日减仓"

        smash_pred = predictions.get("smash_prediction", {})
        if smash_pred.get("predicted") == "降温":
            detail += "；砸盘系数预测偏空，建议谨慎"
        elif smash_pred.get("predicted") == "升温":
            detail += "；砸盘系数预测偏多，可适度参与"

        return detail

    def _save_predictions(self, date_str, predictions):
        all_dates = self.db.get_all_dates()
        if date_str in all_dates:
            idx = all_dates.index(date_str)
            if idx + 1 < len(all_dates):
                target_date = all_dates[idx + 1]
            else:
                target_date = date_str
        else:
            target_date = date_str

        for pred_type, pred_data in predictions.items():
            if isinstance(pred_data, dict):
                content = pred_data.get("predicted", str(pred_data))
                confidence = pred_data.get("confidence", 0.5)
            else:
                content = str(pred_data)
                confidence = 0.5

            try:
                self.db.save_prediction(
                    date=target_date,
                    prediction_type=pred_type,
                    content=str(content),
                    confidence=confidence,
                )
            except Exception as e:
                logger.error(f"保存预测失败: {e}")

        logger.info(f"已保存 {date_str} 的预测（目标日: {target_date}）")