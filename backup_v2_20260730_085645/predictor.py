"""
predictor.py - 预测引擎
基于当前市场状态、历史数据和知识库，生成多维度预测
每个预测附带置信度
砸盘系数作为核心预测因子，权重最高
"""
import logging
from collections import Counter, defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)


class Predictor:
    """预测引擎"""

    def __init__(self, db, knowledge_base=None):
        self.db = db
        self.kb = knowledge_base
        self.weights = self._load_weights()
        # 初始化砸盘系数计算器
        from smash_coefficient import SmashCoefficientCalculator
        self.smash_calc = SmashCoefficientCalculator(db)

    def _load_weights(self):
        """加载模型权重"""
        weights = {}
        rows = self.db.get_all_weights()
        for r in rows:
            r = dict(r)
            weights[r["factor_name"]] = r["weight"]
        return weights

    def predict_next_day(self, date_str, analysis_result, pattern_result):
        """
        基于当日分析和模式识别结果，预测次日市场
        返回预测字典
        """
        if not analysis_result:
            logger.warning("无分析结果，无法生成预测")
            return {}

        predictions = {}
        
        # 获取历史数据作为预测基础
        history = self._get_recent_history(date_str, days=10)
        
        # 1. 涨停数量预测
        predictions["limit_up_count"] = self._predict_limit_up_count(
            date_str, analysis_result, history)
        
        # 2. 连板高度预测
        predictions["max_continuous_boards"] = self._predict_max_boards(
            date_str, analysis_result, history)
        
        # 3. 主线概念预测
        predictions["main_concept"] = self._predict_main_concept(
            date_str, analysis_result, pattern_result, history)
        
        # 4. 情绪方向预测
        predictions["sentiment_direction"] = self._predict_sentiment(
            date_str, analysis_result, pattern_result, history)
        
        # 5. 砸盘系数专项预测（核心预测因子）
        predictions["smash_prediction"] = self._predict_by_smash(
            date_str, analysis_result, pattern_result, history)

        # 6. 操作建议（整合砸盘系数）
        predictions["operation_advice"] = self._generate_advice(
            date_str, analysis_result, pattern_result, predictions)
        
        # 保存预测
        self._save_predictions(date_str, predictions)
        
        return predictions

    def _get_recent_history(self, date_str, days=10):
        """获取最近N天的市场数据"""
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
                # 从原始数据计算
                stocks = self.db.get_limit_up_data(d)
                if stocks:
                    stocks = [dict(s) for s in stocks]
                    boards = [s.get("continuous_boards", 1) or 1 for s in stocks]
                    history.append({
                        "date": d,
                        "limit_up_count": len(stocks),
                        "max_continuous_boards": max(boards),
                        "sentiment_score": 50,  # 估算
                    })
        return history

    def _predict_limit_up_count(self, date_str, analysis, history):
        """
        预测明日涨停数量
        方法：加权平均 + 动量 + 周期调整
        """
        if not history:
            return {"predicted": 50, "confidence": 0.3, "range": (30, 70), "reason": "数据不足，使用默认值"}
        
        counts = [h.get("limit_up_count", 50) for h in history]
        today_count = analysis.get("basic_stats", {}).get("total_count", counts[-1])
        
        # 方法1: 近5日均值（权重: breadth_factor）
        recent_5 = counts[-5:] if len(counts) >= 5 else counts
        avg_5 = sum(recent_5) / len(recent_5)
        
        # 方法2: 动量趋势（权重: momentum_factor）
        if len(counts) >= 3:
            recent_trend = counts[-1] - counts[-3]
            momentum = today_count + recent_trend * 0.5
        else:
            momentum = today_count
        
        # 方法3: 周期调整（权重: cycle_factor）
        phase = analysis.get("sentiment_score", 50)
        if phase > 65:
            cycle_adj = -5  # 高潮后可能回落
        elif phase < 30:
            cycle_adj = 10  # 冰点后可能反弹
        else:
            cycle_adj = 0
        
        # 加权融合
        w_breadth = self.weights.get("breadth_factor", 0.5)
        w_momentum = self.weights.get("momentum_factor", 0.5)
        w_cycle = self.weights.get("cycle_factor", 0.5)
        
        total_w = w_breadth + w_momentum + w_cycle
        predicted = (
            avg_5 * w_breadth + 
            momentum * w_momentum + 
            (today_count + cycle_adj) * w_cycle
        ) / total_w
        
        predicted = max(10, min(150, round(predicted)))
        
        # 置信度
        variance = sum((c - avg_5)**2 for c in recent_5) / len(recent_5) if recent_5 else 100
        stability = max(0.2, 1 - (variance ** 0.5) / avg_5) if avg_5 > 0 else 0.3
        confidence = round(min(0.8, stability * 0.7 + 0.1), 2)
        
        # 预测范围
        std = variance ** 0.5 if variance > 0 else 15
        pred_range = (max(5, round(predicted - std)), round(predicted + std))
        
        return {
            "predicted": predicted,
            "confidence": confidence,
            "range": pred_range,
            "reason": f"基于近{len(recent_5)}日均值{avg_5:.0f}，动量趋势，周期调整",
        }

    def _predict_max_boards(self, date_str, analysis, history):
        """
        预测明日最高连板
        考虑：当前最高板、龙头状态、周期阶段
        """
        if not history:
            return {"predicted": 3, "confidence": 0.3, "reason": "数据不足"}
        
        today_max = analysis.get("basic_stats", {}).get("max_boards", 3)
        sentiment = analysis.get("sentiment_score", 50)
        
        # 历史最高板趋势
        max_boards_hist = [h.get("max_continuous_boards", 3) for h in history]
        avg_max = sum(max_boards_hist) / len(max_boards_hist) if max_boards_hist else 3
        
        # 连板高度预测逻辑
        if sentiment > 65:
            # 高潮期：高度可能继续拓展
            predicted = min(today_max + 1, 15)
        elif sentiment > 50:
            # 发酵期：高度维持
            predicted = today_max
        elif sentiment > 35:
            # 退潮期：高度可能压缩
            predicted = max(2, today_max - 1)
        else:
            # 冰点期：高度较低
            predicted = max(2, min(today_max, round(avg_max)))
        
        # 龙头权重调整
        dragon_w = self.weights.get("dragon_factor", 0.5)
        if dragon_w > 0.7 and today_max >= 5:
            predicted = min(today_max + 1, 15)  # 龙头效应强
        
        confidence = round(min(0.7, 0.3 + dragon_w * 0.4), 2)
        
        return {
            "predicted": predicted,
            "confidence": confidence,
            "reason": f"当前最高{today_max}板，情绪分{sentiment:.0f}，历史均高{avg_max:.1f}板",
        }

    def _predict_main_concept(self, date_str, analysis, pattern, history):
        """
        预测明日主线概念
        考虑：概念持续性、概念热度、轮动模式
        数据来源：优先使用选股宝概念数据
        """
        concept_heat = analysis.get("concept_heat", {})
        top_concepts = concept_heat.get("top_concepts", [])
        hot_concepts = concept_heat.get("hot_concepts", [])
        concept_source = concept_heat.get("source", "none")

        rotation = pattern.get("concept_rotation", {}) if pattern else {}
        rotation_pattern = rotation.get("rotation_pattern", "")
        persisted = rotation.get("persisted_concepts", [])

        if not top_concepts:
            # 最后尝试从xgb表直接聚合
            xgb_concepts = self.db.get_xgb_concepts_by_date(date_str)
            if xgb_concepts:
                top_concepts = [{"concept": k, "count": v}
                                for k, v in sorted(xgb_concepts.items(), key=lambda x: x[1], reverse=True)][:10]
                hot_concepts = [c for c in top_concepts if c.get("count", 0) >= 5]
                concept_source = "xgb_direct"

        if not top_concepts:
            return {"predicted": "未知", "confidence": 0.2, "reason": "无概念数据（选股宝数据缺失）"}
        
        # 概念热度权重
        heat_w = self.weights.get("concept_heat_factor", 0.5)
        
        # 候选主线概念评分
        concept_scores = {}
        for c in top_concepts:
            name = c.get("concept", "")
            count = c.get("count", 0)
            score = count
            
            # 持续存在的概念加分
            if name in persisted:
                score *= 1.3
            
            # 主线明确时，头部概念更可能持续
            if rotation_pattern == "主线明确":
                score *= 1.2
            
            concept_scores[name] = score
        
        # 排序
        sorted_concepts = sorted(concept_scores.items(), key=lambda x: x[1], reverse=True)
        
        predicted = sorted_concepts[0][0] if sorted_concepts else top_concepts[0].get("concept", "")
        top3 = [c[0] for c in sorted_concepts[:3]]
        
        confidence = round(min(0.75, heat_w + 0.1 * len(persisted)), 2)
        
        return {
            "predicted": predicted,
            "confidence": confidence,
            "top_candidates": top3,
            "reason": f"概念热度排序(数据源:{concept_source}): {', '.join(f'{c[0]}({c[1]}分)' for c in sorted_concepts[:3])}",
        }

    def _predict_sentiment(self, date_str, analysis, pattern, history):
        """
        预测情绪方向：升温/降温/震荡
        """
        sentiment = analysis.get("sentiment_score", 50)
        continuation = analysis.get("continuation_analysis", {})
        cont_rate = continuation.get("continuation_rate", 0)
        
        # 获取近期情绪趋势
        sentiments = [h.get("sentiment_score", 50) for h in history]
        
        if not sentiments:
            return {"predicted": "震荡", "confidence": 0.3, "reason": "数据不足"}
        
        # 趋势计算
        if len(sentiments) >= 3:
            trend = sentiments[-1] - sentiments[-3]
        else:
            trend = 0
        
        # 周期因子
        cycle_w = self.weights.get("cycle_factor", 0.5)
        continuation_w = self.weights.get("continuation_factor", 0.5)
        
        # 综合评分
        momentum_score = trend * 0.5  # 动量
        continuation_score = (cont_rate - 30) * 0.3  # 晋级率偏离
        cycle_score = 0
        
        phase = pattern.get("cycle_phase", "") if pattern else ""
        if phase == "启动期":
            cycle_score = 10
        elif phase == "发酵期":
            cycle_score = 5
        elif phase == "高潮期":
            cycle_score = -5  # 高潮后易退潮
        elif phase == "退潮期":
            cycle_score = -10
        elif phase == "冰点期":
            cycle_score = 10  # 冰点可能反弹
        elif phase == "反包期":
            cycle_score = 5
        
        total = momentum_score * cycle_w + continuation_score * continuation_w + cycle_score
        
        if total > 5:
            direction = "升温"
        elif total < -5:
            direction = "降温"
        else:
            direction = "震荡"
        
        confidence = round(min(0.7, 0.3 + abs(total) / 30), 2)
        
        return {
            "predicted": direction,
            "confidence": confidence,
            "score": round(total, 1),
            "reason": f"情绪分{sentiment:.0f}，趋势{trend:+.0f}，晋级率{cont_rate:.0f}%，周期{phase}",
        }

    def _predict_by_smash(self, date_str, analysis, pattern, history):
        """
        基于砸盘系数预测明日市场（核心预测因子）
        砸盘系数在预测中的权重最高
        """
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

            # 基于当前砸盘系数判断明日方向
            if smash_value > high_threshold:
                base_direction = "降温"
                base_reason = f"砸盘系数{smash_value:.1f}偏高(>{high_threshold})，抛压沉重"
            elif smash_value < low_threshold:
                base_direction = "升温"
                base_reason = f"砸盘系数{smash_value:.1f}偏低(<{low_threshold})，做多氛围好"
            else:
                base_direction = "震荡"
                base_reason = f"砸盘系数{smash_value:.1f}处于正常区间"

            # 趋势修正
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

            # 计算置信度（砸盘系数权重大）
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

    def _generate_advice(self, date_str, analysis, pattern, predictions):
        """
        生成操作建议（砸盘系数为核心影响因素）
        """
        from config import SMASH_CONFIG

        sentiment = analysis.get("sentiment_score", 50)
        basic = analysis.get("basic_stats", {})
        seal_quality = analysis.get("seal_quality", {})
        quality_score = seal_quality.get("quality_score", 50)

        # 获取砸盘系数（核心因素）
        smash_data = analysis.get("smash_analysis", {})
        smash_value = smash_data.get("smash_coefficient")
        smash_signal = smash_data.get("signal", "未知")
        smash_trade_advice = smash_data.get("trade_advice", "")

        high_threshold = SMASH_CONFIG.get("high_pressure_threshold", 7.0)
        low_threshold = SMASH_CONFIG.get("low_pressure_threshold", 4.0)
        stop_loss_ext = SMASH_CONFIG.get("stop_loss_extension", 2.0)

        # 砸盘系数主导的操作建议
        if smash_value is not None and smash_value > high_threshold:
            # 抛压重 → 保守
            advice = "保守"
            detail = "市场抛压重，严格控制仓位，避免追高，只做低吸"
            if stop_loss_ext:
                detail += f"，止损放宽{stop_loss_ext}%"
        elif smash_value is not None and smash_value < low_threshold:
            # 抛压轻 → 可以积极
            if sentiment >= 55 and quality_score >= 60:
                advice = "积极"
                detail = "抛压轻，适合主动进攻，可适当提高仓位参与龙头和主线"
            else:
                advice = "中性偏积极"
                detail = "抛压较轻，市场做多氛围好，可适当参与前排龙头"
        else:
            # 砸盘系数正常或缺失，按传统情绪判断
            if sentiment >= 70:
                advice = "保守"
                detail = "市场情绪过热，建议控制仓位，避免追高，等待回调低吸"
            elif sentiment >= 55:
                if quality_score >= 60:
                    advice = "积极"
                    detail = "市场情绪偏暖且封板质量较好，可适当参与龙头和主线"
                else:
                    advice = "中性"
                    detail = "市场尚可但封板质量一般，建议聚焦核心龙头，控制仓位"
            elif sentiment >= 40:
                advice = "中性偏保守"
                detail = "市场情绪中性，建议轻仓参与前排龙头，快进快出"
            elif sentiment >= 25:
                advice = "保守"
                detail = "市场偏弱，建议观望或极轻仓试错首板"
            else:
                advice = "观望"
                detail = "市场冰点，建议空仓等待回暖信号"

        # 追加砸盘系数的交易建议
        if smash_trade_advice:
            detail += f"；{smash_trade_advice}"

        # 结合预测调整
        sent_pred = predictions.get("sentiment_direction", {})
        if sent_pred.get("predicted") == "升温":
            detail += "；预测明日情绪升温，可适当提前布局"
        elif sent_pred.get("predicted") == "降温":
            detail += "；预测明日情绪降温，建议今日减仓"

        # 砸盘系数预测调整
        smash_pred = predictions.get("smash_prediction", {})
        if smash_pred.get("predicted") == "降温":
            detail += "；砸盘系数预测偏空，建议谨慎"
        elif smash_pred.get("predicted") == "升温":
            detail += "；砸盘系数预测偏多，可适度参与"

        confidence = round(min(0.75, sentiment / 100 + 0.2 + (0.1 if smash_value is not None else 0)), 2)

        # 生成reason
        reason_parts = [f"情绪分{sentiment:.0f}", f"封板质量{quality_score:.0f}分"]
        if smash_value is not None:
            reason_parts.append(f"砸盘系数{smash_value:.1f}({smash_signal})")
        reason = "，".join(reason_parts)

        return {
            "advice": advice,
            "confidence": confidence,
            "detail": detail,
            "reason": reason,
        }

    def _save_predictions(self, date_str, predictions):
        """保存预测到数据库"""
        # 预测的次日
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
