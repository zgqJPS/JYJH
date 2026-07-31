"""
predictor_v2.py - 新版预测引擎
核心改动：
1. 用转移概率矩阵替代简单均值预测连板
2. 用条件预测替代简单均值预测涨停数
3. 砸盘系数从"方向指标"重定义为"分歧度/波动率指标"
4. 集成5个高价值信号作为预测规则
5. 基准值修正：日均涨停57.7（非20）
"""
import logging
import sqlite3
from collections import defaultdict

logger = logging.getLogger(__name__)


class PredictorV2:
    """新版预测引擎"""
    
    # 日均涨停基准值（67天数据统计）
    DAILY_AVG_LIMIT_UP = 57.7
    
    # 信号权重（来自67天深度分析）
    SIGNAL_WEIGHTS = {
        1: {"weight": 3, "description": "5→6突破+砸盘下降", "base_count": 77.8},
        2: {"weight": 3, "description": "砸盘骤降>3+连板≤3", "count_adjustment": 30},
        3: {"weight": 2, "description": "连续2天砸盘<3+连板≤3", "count_adjustment": 15},
        4: {"weight": 2, "description": "7板+砸盘>6", "count_adjustment": -25},
        5: {"weight": 1, "description": "4板+涨停<35+砸盘<3", "count_adjustment": -10},
    }
    
    # 转移概率矩阵（来自cycle_model）
    TRANSITION_MATRIX = {
        2: {"up": 1.00, "flat": 0.00, "down": 0.00, "avg_next": 3.0},
        3: {"up": 0.82, "flat": 0.18, "down": 0.00, "avg_next": 3.8},
        4: {"up": 0.61, "flat": 0.26, "down": 0.13, "avg_next": 4.4},
        5: {"up": 0.33, "flat": 0.00, "down": 0.67, "avg_next": 4.4},
        6: {"up": 0.83, "flat": 0.00, "down": 0.17, "avg_next": 6.5},
        7: {"up": 0.40, "flat": 0.00, "down": 0.60, "avg_next": 5.8},
        8: {"up": 0.00, "flat": 0.00, "down": 1.00, "avg_next": 4.0},
    }
    
    def __init__(self, db, knowledge_base=None, db_path=None):
        """
        初始化预测引擎
        db: 数据库连接对象（兼容原有接口）
        knowledge_base: 知识库对象
        db_path: sqlite数据库路径（用于直接操作sqlite3）
        """
        self.db = db
        self.kb = knowledge_base
        self.db_path = db_path or getattr(db, 'db_path', None)
        
        # 初始化周期模型
        from cycle_model import CycleModel
        self.cycle_model = CycleModel(self.db_path)
        
        # 初始化砸盘系数计算器
        from smash_coefficient_v2 import SmashCoefficientCalculatorV2
        self.smash_calc = SmashCoefficientCalculatorV2(db, db_path=self.db_path)
        
        # 权重（兼容原有接口）
        self.weights = self._load_weights()
    
    def _get_conn(self):
        """获取sqlite连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def _load_weights(self):
        """加载模型权重"""
        weights = {}
        try:
            rows = self.db.get_all_weights()
            for r in rows:
                r = dict(r)
                weights[r["factor_name"]] = r["weight"]
        except Exception as e:
            logger.warning(f"加载权重失败: {e}")
        return weights
    
    def predict_next_day(self, date_str, analysis_result, pattern_result):
        """
        基于当日分析和模式识别结果，预测次日市场
        返回预测字典（接口签名与原predictor完全一致）
        """
        if not analysis_result:
            logger.warning("无分析结果，无法生成预测")
            return {}
        
        predictions = {}
        
        # 检测信号
        signals = self.cycle_model.detect_signals(date_str)
        triggered_signals = [s for s in signals if s.get("triggered")]
        
        # 获取周期阶段
        phase_result = self.cycle_model.detect_phase(date_str)
        
        # 获取历史数据
        history = self._get_recent_history(date_str, days=10)
        
        # 1. 涨停数量预测（基于条件预测）
        predictions["limit_up_count"] = self._predict_limit_up_count(
            date_str, analysis_result, history, phase_result, triggered_signals)
        
        # 2. 连板高度预测（基于转移概率矩阵）
        predictions["max_continuous_boards"] = self._predict_max_boards(
            date_str, analysis_result, history, phase_result, triggered_signals)
        
        # 3. 主线概念预测
        predictions["main_concept"] = self._predict_main_concept(
            date_str, analysis_result, pattern_result, history)
        
        # 4. 情绪方向预测
        predictions["sentiment_direction"] = self._predict_sentiment(
            date_str, analysis_result, pattern_result, history, phase_result, triggered_signals)
        
        # 5. 砸盘系数分歧度预测（重定义）
        predictions["smash_prediction"] = self._predict_by_smash(
            date_str, analysis_result, pattern_result, history, phase_result)
        
        # 6. 操作建议（整合周期+信号+分歧度）
        predictions["operation_advice"] = self._generate_advice(
            date_str, analysis_result, pattern_result, predictions, phase_result, triggered_signals)
        
        # 保存预测
        self._save_predictions(date_str, predictions)
        
        return predictions
    
    def _get_recent_history(self, date_str, days=10):
        """获取最近N天的市场数据"""
        conn = self._get_conn()
        try:
            query = """
                SELECT s.date, s.smash_coefficient as sc, s.max_continuous_boards as mb,
                       (SELECT COUNT(*) FROM akshare_limit_up WHERE date=s.date) as lu
                FROM smash_coefficient_results s
                WHERE s.date <= ? AND s.smash_coefficient > 0
                ORDER BY s.date DESC LIMIT ?
            """
            rows = conn.execute(query, (date_str, days)).fetchall()
            history = []
            for row in reversed(rows):
                row = dict(row)
                history.append({
                    "date": row["date"],
                    "limit_up_count": row["lu"],
                    "max_continuous_boards": row["mb"],
                    "smash_coefficient": row["sc"],
                })
            return history
        finally:
            conn.close()
    
    def _predict_limit_up_count(self, date_str, analysis, history, phase_result, triggered_signals):
        """
        预测明日涨停数量
        方法：基础值=前日涨停数 → 周期阶段调整 → 信号修正 → 砸盘修正
        """
        # 基础值 = 前日涨停数
        today_count = analysis.get("basic_stats", {}).get("total_count", self.DAILY_AVG_LIMIT_UP)
        
        # 从历史中获取前日涨停数
        if history:
            base_count = history[-1]["limit_up_count"]
        else:
            base_count = today_count
        
        predicted = base_count
        
        # 周期阶段调整
        phase = phase_result.get("phase", "")
        phase_adj_pct = 0.0
        
        if phase == "冰点酝酿期":
            phase_adj_pct = 0.10  # 冰点期+10%
        elif phase == "蓄力爬升期":
            phase_adj_pct = 0.05  # 蓄力期+5%
        elif phase == "爆发高潮期":
            # 高潮期看砸盘方向：砸盘上升→下调15%（即将崩塌）；砸盘下降→继续维持
            sc_change = phase_result.get("indicators", {}).get("sc_change", 0)
            if sc_change > 1.5:
                phase_adj_pct = -0.15  # 高潮+分歧上升→风险
            else:
                phase_adj_pct = 0.05  # 高潮+分歧下降→继续
        elif phase == "崩塌退潮期":
            phase_adj_pct = -0.20  # 崩塌期-20%
        
        predicted = predicted * (1 + phase_adj_pct)
        
        # 信号修正（高价值信号直接覆盖）
        signal_reasons = []
        for sig in triggered_signals:
            sid = sig.get("signal_id")
            if sid == 1:
                # 信号1触发：次日涨停数预期77.8
                predicted = 77.8
                signal_reasons.append("⚡信号1: 涨停预期77.8")
                break  # 信号1最强，直接覆盖
            elif sid == 2:
                predicted += 30
                signal_reasons.append("信号2: +30")
            elif sid == 3:
                predicted += 15
                signal_reasons.append("信号3: +15")
            elif sid == 4:
                predicted -= 25
                signal_reasons.append("⚠️信号4: -25(见顶)")
            elif sid == 5:
                predicted -= 10
                signal_reasons.append("信号5: -10(假突破)")
        
        # 砸盘修正：砸盘下降→上调5~10%；砸盘上升→下调5~10%
        sc_change = phase_result.get("indicators", {}).get("sc_change", 0)
        smash_adj_pct = 0.0
        if sc_change < -1.5:
            smash_adj_pct = 0.10  # 分歧下降→上调
        elif sc_change > 1.5:
            smash_adj_pct = -0.10  # 分歧上升→下调
        elif sc_change < -0.5:
            smash_adj_pct = 0.05
        elif sc_change > 0.5:
            smash_adj_pct = -0.05
        
        predicted = predicted * (1 + smash_adj_pct)
        
        # 限制范围
        predicted = max(10, min(150, round(predicted)))
        
        # 置信度计算
        confidence = 0.5
        if history and len(history) >= 3:
            counts = [h["limit_up_count"] for h in history[-5:]]
            variance = sum((c - sum(counts)/len(counts))**2 for c in counts) / len(counts)
            stability = max(0.2, 1 - (variance ** 0.5) / (sum(counts)/len(counts))) if counts else 0.3
            confidence = round(min(0.85, stability * 0.6 + 0.2 + (0.1 if triggered_signals else 0)), 2)
        
        # 预测范围
        base_std = max(8, abs(predicted - base_count) * 1.5)
        pred_range = (max(5, round(predicted - base_std)), round(predicted + base_std))
        
        # 原因描述
        reason_parts = [f"基础值{base_count}"]
        if phase_adj_pct != 0:
            reason_parts.append(f"{phase}调整{phase_adj_pct*100:+.0f}%")
        if smash_adj_pct != 0:
            reason_parts.append(f"砸盘变化{sc_change:+.2f}调整{smash_adj_pct*100:+.0f}%")
        if signal_reasons:
            reason_parts.extend(signal_reasons)
        
        return {
            "predicted": predicted,
            "confidence": confidence,
            "range": pred_range,
            "reason": "，".join(reason_parts),
            "phase": phase,
            "signals_triggered": len(triggered_signals),
        }
    
    def _predict_max_boards(self, date_str, analysis, history, phase_result, triggered_signals):
        """
        预测明日最高连板
        使用转移概率矩阵 + 信号覆盖
        """
        boards_result = self.cycle_model.predict_next_day_boards(date_str)
        
        predicted = boards_result["predicted_boards"]
        prob_up = boards_result["probability_up"]
        prob_down = boards_result["probability_down"]
        confidence = boards_result["confidence"]
        reason = boards_result["reason"]
        
        # 如果有信号触发，补充说明
        for sig in triggered_signals:
            sid = sig.get("signal_id")
            if sid == 1:
                predicted = 7
                prob_up = 1.0
                confidence = 0.95
                reason += "；⚡信号1覆盖→预测7板"
            elif sid == 4:
                predicted = max(3, predicted - 2)
                prob_down = 1.0
                confidence = 0.90
                reason += "；⚠️信号4覆盖→回落风险"
        
        return {
            "predicted": predicted,
            "confidence": confidence,
            "probability_up": prob_up,
            "probability_down": prob_down,
            "reason": reason,
            "transition_matrix_used": True,
        }
    
    def _predict_main_concept(self, date_str, analysis, pattern, history):
        """
        预测明日主线概念（与原版本基本一致）
        """
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
                                    for k, v in sorted(xgb_concepts.items(), key=lambda x: x[1], reverse=True)][:10]
                    hot_concepts = [c for c in top_concepts if c.get("count", 0) >= 5]
                    concept_source = "xgb_direct"
            except Exception:
                pass
        
        if not top_concepts:
            return {"predicted": "未知", "confidence": 0.2, "reason": "无概念数据（选股宝数据缺失）"}
        
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
            
            concept_scores[name] = score
        
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
    
    def _predict_sentiment(self, date_str, analysis, pattern, history, phase_result, triggered_signals):
        """
        预测情绪方向：升温/降温/震荡
        整合周期阶段和信号
        """
        sentiment = analysis.get("sentiment_score", 50)
        continuation = analysis.get("continuation_analysis", {})
        cont_rate = continuation.get("continuation_rate", 0)
        
        phase = phase_result.get("phase", "")
        indicators = phase_result.get("indicators", {})
        sc_change = indicators.get("sc_change", 0)
        
        # 周期阶段主导
        cycle_score = 0
        if phase == "冰点酝酿期":
            cycle_score = 10  # 即将反弹
        elif phase == "蓄力爬升期":
            cycle_score = 15  # 升温中
        elif phase == "爆发高潮期":
            # 高潮期看分歧方向
            if sc_change > 1.5:
                cycle_score = -15  # 分歧上升→即将降温
            else:
                cycle_score = 5  # 分歧下降→继续升温
        elif phase == "崩塌退潮期":
            cycle_score = -20  # 降温中
        
        # 信号修正
        signal_score = 0
        for sig in triggered_signals:
            sid = sig.get("signal_id")
            if sid in (1, 2, 3):
                signal_score += 10  # 看涨信号→升温
            elif sid in (4, 5):
                signal_score -= 10  # 看跌信号→降温
        
        # 砸盘变化修正
        smash_score = 0
        if sc_change < -2:
            smash_score = 10  # 分歧骤降→利好
        elif sc_change > 2:
            smash_score = -10  # 分歧骤升→利空
        
        total = cycle_score + signal_score + smash_score
        
        if total > 10:
            direction = "升温"
        elif total < -10:
            direction = "降温"
        else:
            direction = "震荡"
        
        confidence = round(min(0.75, 0.3 + abs(total) / 50), 2)
        
        return {
            "predicted": direction,
            "confidence": confidence,
            "score": round(total, 1),
            "reason": f"周期阶段:{phase}({cycle_score:+d})，信号修正{signal_score:+d}，砸盘变化{sc_change:+.2f}({smash_score:+d})",
            "phase": phase,
        }
    
    def _predict_by_smash(self, date_str, analysis, pattern, history, phase_result):
        """
        砸盘系数预测（重定义为分歧度/波动率指标）
        
        新逻辑：
        - 低分歧+低连板=冰点酝酿，关注启动信号
        - 低分歧+高连板=高度一致，加速中
        - 高分歧+高连板=过热，即将崩塌
        - 高分歧+低连板=混乱，方向不明
        - 砸盘骤降=抛压释放=利好
        """
        try:
            smash_data = analysis.get("smash_analysis", {})
            smash_value = smash_data.get("smash_coefficient")
            
            if smash_value is None:
                return {
                    "predicted": "数据不足",
                    "confidence": 0.2,
                    "reason": "砸盘系数未计算，无法基于此预测",
                }
            
            # 获取连板高度
            mb = phase_result.get("indicators", {}).get("max_boards", 3)
            sc_change = phase_result.get("indicators", {}).get("sc_change", 0)
            
            # 分歧度判断（5档分类）
            if smash_value < 1.5:
                divergence = "极低分歧"
            elif smash_value < 3:
                divergence = "低分歧"
            elif smash_value < 5:
                divergence = "中等分歧"
            elif smash_value < 7:
                divergence = "高分歧"
            else:
                divergence = "极高分歧"
            
            # 组合判断
            if smash_value < 3 and mb <= 3:
                direction = "酝酿"
                interpretation = f"分歧度{smash_value:.1f}({divergence})+连板{mb}→冰点酝酿，关注启动信号"
            elif smash_value < 3 and mb >= 5:
                direction = "一致"
                interpretation = f"分歧度{smash_value:.1f}({divergence})+连板{mb}→高度一致，加速中"
            elif smash_value > 5 and mb >= 6:
                direction = "过热"
                interpretation = f"分歧度{smash_value:.1f}({divergence})+连板{mb}→过热，即将崩塌"
            elif smash_value > 5 and mb <= 3:
                direction = "混乱"
                interpretation = f"分歧度{smash_value:.1f}({divergence})+连板{mb}→混乱，方向不明"
            else:
                direction = "中性"
                interpretation = f"分歧度{smash_value:.1f}({divergence})，连板{mb}，正常波动"
            
            # 趋势修正
            if sc_change < -1.5:
                trend_advice = "分歧骤降→抛压释放，利好"
            elif sc_change > 1.5:
                trend_advice = "分歧骤升→抛压加剧，利空"
            else:
                trend_advice = "分歧稳定"
            
            confidence = 0.65
            
            return {
                "predicted": direction,
                "confidence": confidence,
                "smash_value": smash_value,
                "divergence_level": divergence,
                "trend": "上升" if sc_change > 0.5 else ("下降" if sc_change < -0.5 else "稳定"),
                "reason": f"{interpretation}；{trend_advice}",
            }
        
        except Exception as e:
            logger.error(f"砸盘系数分歧度预测异常: {e}")
            return {
                "predicted": "未知",
                "confidence": 0.2,
                "reason": f"砸盘系数预测异常: {e}",
            }
    
    def _generate_advice(self, date_str, analysis, pattern, predictions, phase_result, triggered_signals):
        """
        生成操作建议
        整合：周期阶段判断 + 信号触发情况 + 分歧度
        """
        phase = phase_result.get("phase", "")
        indicators = phase_result.get("indicators", {})
        sc = indicators.get("smash_coefficient", 5)
        mb = indicators.get("max_boards", 3)
        lu = indicators.get("limit_up_count", 50)
        sc_change = indicators.get("sc_change", 0)
        
        # 获取分析结果中的其他指标
        sentiment = analysis.get("sentiment_score", 50)
        seal_quality = analysis.get("seal_quality", {})
        quality_score = seal_quality.get("quality_score", 50)
        
        # 基础建议（基于周期阶段）
        if phase == "冰点酝酿期":
            advice = "潜伏"
            detail = "市场冰点酝酿中，观察启动信号。可极轻仓试错低位龙头，严格止损。"
            if triggered_signals:
                for sig in triggered_signals:
                    if sig.get("signal_id") in (2, 3):
                        detail = "市场底部信号触发！砸盘骤降释放完毕，可开始建仓低位龙头。"
                        advice = "建仓"
                        break
        elif phase == "蓄力爬升期":
            if mb >= 5 and sc_change < 0:
                advice = "加仓"
                detail = "蓄力爬升+5板+分歧下降，强烈看涨信号，可积极加仓主线龙头。"
            else:
                advice = "积极参与"
                detail = f"蓄力爬升中，连板{mb}，可逐步建仓主线方向。"
        elif phase == "爆发高潮期":
            if mb >= 7 and sc > 6:
                advice = "撤退"
                detail = "⚠️高潮过热！连板{mb}+分歧{sc:.1f}，见顶崩塌风险极高，立即减仓！"
                advice = "撤退"
            else:
                advice = "持仓观望"
                detail = f"高潮期进行中，连板{mb}，分歧{sc:.1f}。持有核心龙头，但准备随时撤退。"
        elif phase == "崩塌退潮期":
            if sc_change < -3 and mb <= 3:
                advice = "准备建仓"
                detail = "崩塌末期出现底部信号！分歧骤降，可开始关注低位龙头准备抄底。"
            else:
                advice = "空仓观望"
                detail = "崩塌退潮中，等待底部信号再入场。当前不宜操作。"
        else:
            # 默认基于情绪判断
            if sentiment >= 60:
                advice = "积极"
                detail = "市场情绪偏暖，可适当参与"
            elif sentiment >= 40:
                advice = "中性"
                detail = "市场情绪中性，控制仓位"
            else:
                advice = "保守"
                detail = "市场情绪偏弱，建议观望"
        
        # 信号触发补充
        if triggered_signals:
            triggered_names = [s.get("name", "") for s in triggered_signals if s.get("name")]
            detail += f"\n触发信号: {', '.join(triggered_names)}"
        
        # 砸盘预测补充
        smash_pred = predictions.get("smash_prediction", {})
        smash_direction = smash_pred.get("predicted", "")
        if smash_direction == "过热":
            detail += "；分歧度过高，注意风险"
        elif smash_direction == "一致":
            detail += "；高度一致，可加仓"
        elif smash_direction == "酝酿":
            detail += "；冰点酝酿中，等待信号"
        
        # 情绪方向补充
        sent_pred = predictions.get("sentiment_direction", {})
        if sent_pred.get("predicted") == "升温":
            detail += "；预测次日升温，可适当提前布局"
        elif sent_pred.get("predicted") == "降温":
            detail += "；预测次日降温，建议减仓"
        
        confidence = round(min(0.80, 0.4 + (0.15 if triggered_signals else 0) + (0.1 if phase_result.get("confidence", 0) > 0.5 else 0)), 2)
        
        return {
            "advice": advice,
            "confidence": confidence,
            "detail": detail,
            "reason": f"周期阶段:{phase}，分歧度:{sc:.1f}，连板:{mb}，信号:{len(triggered_signals)}个",
            "phase": phase,
        }
    
    def _save_predictions(self, date_str, predictions):
        """保存预测到数据库"""
        try:
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
        except Exception as e:
            logger.error(f"保存预测整体失败: {e}")
