"""
self_corrector.py - 自我修正引擎（核心模块）
根据预测验证结果，自动调整模型权重和策略
使用指数加权移动平均(EWMA)，设置权重上下限防止极端化
"""
import logging
import json
from datetime import datetime

logger = logging.getLogger(__name__)


class SelfCorrector:
    """自我修正引擎"""

    # 因素与预测类型的映射
    FACTOR_MAP = {
        "limit_up_count": ["breadth_factor", "momentum_factor", "cycle_factor", "smash_factor"],
        "max_continuous_boards": ["dragon_factor", "momentum_factor", "cycle_factor", "smash_factor"],
        "main_concept": ["concept_heat_factor", "continuation_factor"],
        "sentiment_direction": ["cycle_factor", "continuation_factor", "momentum_factor", "smash_factor"],
        "operation_advice": ["cycle_factor", "seal_quality_factor", "smash_factor"],
        "smash_prediction": ["smash_factor"],  # 砸盘系数专项预测
    }

    def __init__(self, db, knowledge_base=None):
        self.db = db
        self.kb = knowledge_base

    def correct(self, verification_results, date_str):
        """
        核心修正方法
        verification_results: 预测验证结果列表
        date_str: 当前日期
        返回: 修正记录列表
        """
        if not verification_results:
            logger.info("无验证结果，跳过修正")
            return []

        corrections = []
        
        # 1. 计算各因素本次的"贡献度"
        factor_scores = self._calculate_factor_scores(verification_results)
        
        # 2. 根据因素得分调整权重
        for factor_name, score_info in factor_scores.items():
            correction = self._adjust_weight(factor_name, score_info, date_str)
            if correction:
                corrections.append(correction)
        
        # 3. 检查是否有模式需要标记为低可信度
        self._check_low_credibility(date_str)
        
        # 4. 更新知识库中的知识权重
        self._update_knowledge_weights(verification_results, date_str)
        
        # 5. 记录整体修正摘要
        if corrections:
            self._log_correction_summary(date_str, corrections, factor_scores)
        
        return corrections

    def _calculate_factor_scores(self, verification_results):
        """
        计算各因素在本次验证中的得分
        将预测类型的准确率映射到其关联的因素上
        """
        factor_scores = {}
        
        for result in verification_results:
            pred_type = result.get("type", "")
            accuracy = result.get("score", 0)
            
            # 获取该预测类型关联的因素
            related_factors = self.FACTOR_MAP.get(pred_type, [])
            
            for factor in related_factors:
                if factor not in factor_scores:
                    factor_scores[factor] = {"scores": [], "types": []}
                factor_scores[factor]["scores"].append(accuracy)
                factor_scores[factor]["types"].append(pred_type)
        
        # 计算每个因素的综合得分
        result = {}
        for factor, info in factor_scores.items():
            scores = info["scores"]
            avg_score = sum(scores) / len(scores) if scores else 0.5
            
            # 加权：考虑得分的稳定性（方差越小越稳定）
            if len(scores) > 1:
                variance = sum((s - avg_score)**2 for s in scores) / len(scores)
                stability = max(0.5, 1 - variance)
            else:
                stability = 0.7
            
            result[factor] = {
                "avg_score": avg_score,
                "stability": stability,
                "weighted_score": avg_score * stability,
                "sample_count": len(scores),
                "related_types": info["types"],
            }
        
        return result

    def _adjust_weight(self, factor_name, score_info, date_str):
        """
        调整单个因素的权重
        使用 EWMA (指数加权移动平均)
        """
        from config import CORRECTION_CONFIG
        
        current = self.db.get_weight(factor_name)
        if not current:
            # 初始化权重
            new_weight = CORRECTION_CONFIG["default_weight"]
            self.db.update_weight(factor_name, new_weight, reason="初始化", date=date_str)
            return None
        
        current = dict(current)
        old_weight = current["weight"]
        alpha = CORRECTION_CONFIG["ewma_alpha"]
        step = CORRECTION_CONFIG["adjustment_step"]
        w_min = CORRECTION_CONFIG["weight_min"]
        w_max = CORRECTION_CONFIG["weight_max"]
        
        # 目标权重：基于本次表现
        # 得分>0.6则提高权重，<0.4则降低权重
        target_direction = score_info["weighted_score"]
        
        if target_direction >= 0.6:
            # 表现好：向0.7-0.9方向移动
            target_weight = old_weight + step * (target_direction - 0.5) * 2
        elif target_direction <= 0.4:
            # 表现差：降低权重
            target_weight = old_weight - step * (0.5 - target_direction) * 2
        else:
            # 表现一般：微调
            target_weight = old_weight + step * (target_direction - 0.5) * 0.5
        
        # EWMA平滑
        new_weight = alpha * target_weight + (1 - alpha) * old_weight
        
        # 限制范围
        new_weight = max(w_min, min(w_max, new_weight))
        
        # 确保变化量不小于最小步长（避免无效更新）
        if abs(new_weight - old_weight) < 0.001:
            return None
        
        # 更新权重
        reason = self._generate_reason(factor_name, old_weight, new_weight, score_info)
        self.db.update_weight(factor_name, round(new_weight, 4), reason=reason, date=date_str)
        
        # 更新连续误判计数
        consecutive = current.get("consecutive_misses", 0) or 0
        if target_direction < 0.4:
            consecutive += 1
        else:
            consecutive = 0
        
        try:
            self.db.execute(
                "UPDATE model_weights SET consecutive_misses = ? WHERE factor_name = ?",
                (consecutive, factor_name))
        except:
            pass
        
        # 更新可信度
        credibility = current.get("credibility", 1.0) or 1.0
        if consecutive >= 3:
            credibility = max(0.3, credibility - 0.1)
        elif target_direction > 0.6:
            credibility = min(1.0, credibility + 0.05)
        
        try:
            self.db.execute(
                "UPDATE model_weights SET credibility = ? WHERE factor_name = ?",
                (round(credibility, 3), factor_name))
        except:
            pass
        
        return {
            "factor": factor_name,
            "old_weight": round(old_weight, 4),
            "new_weight": round(new_weight, 4),
            "change": round(new_weight - old_weight, 4),
            "reason": reason,
            "score": score_info["weighted_score"],
            "consecutive_misses": consecutive,
            "credibility": round(credibility, 3),
        }

    def _generate_reason(self, factor_name, old_w, new_w, score_info):
        """生成修正原因说明"""
        direction = "↑提升" if new_w > old_w else "↓降低"
        score = score_info["weighted_score"]
        return (
            f"因素{factor_name}权重{direction}: "
            f"本次得分{score:.2f}(稳定度{score_info['stability']:.2f}), "
            f"涉及预测类型: {', '.join(set(score_info['related_types']))}"
        )

    def _check_low_credibility(self, date_str):
        """检查并标记低可信度因素"""
        from config import CORRECTION_CONFIG
        threshold = CORRECTION_CONFIG["low_confidence_threshold"]
        
        weights = self.db.get_all_weights()
        for w in weights:
            w = dict(w)
            if (w.get("consecutive_misses", 0) or 0) >= threshold:
                logger.warning(
                    f"⚠️ 因素 {w['factor_name']} 连续误判{w['consecutive_misses']}次，"
                    f"可信度降至{w.get('credibility', 1.0):.2f}")
                # 保存知识
                self.db.save_knowledge(
                    "low_credibility",
                    f"{w['factor_name']}连续误判{w['consecutive_misses']}次",
                    metadata={"date": date_str, "factor": w["factor_name"]})

    def _update_knowledge_weights(self, verification_results, date_str):
        """根据验证结果更新知识库中的知识权重"""
        from config import CORRECTION_CONFIG
        
        decay_factor = CORRECTION_CONFIG["knowledge_decay_factor"]
        
        for result in verification_results:
            pred_type = result.get("type", "")
            score = result.get("score", 0)
            
            # 根据预测类型找到相关知识
            pattern_type_map = {
                "limit_up_count": "market_pattern",
                "main_concept": "concept_rotation",
                "sentiment_direction": "cycle_phase",
                "max_continuous_boards": "dragon",
            }
            
            pattern_type = pattern_type_map.get(pred_type)
            if not pattern_type:
                continue
            
            # 更新知识的成功率
            knowledge_list = self.db.get_knowledge(pattern_type)
            for k in knowledge_list:
                k = dict(k)
                old_rate = k.get("success_rate", 0.5) or 0.5
                # EWMA更新成功率
                new_rate = 0.3 * score + 0.7 * old_rate
                self.db.update_knowledge_score(k["id"], round(new_rate, 3), date_str)
        
        # 知识衰减：降低长期未验证的知识权重
        all_knowledge = self.db.get_knowledge()
        for k in all_knowledge:
            k = dict(k)
            last_verified = k.get("last_verified")
            if last_verified:
                try:
                    last_date = datetime.strptime(last_verified, "%Y-%m-%d")
                    days_since = (datetime.now() - last_date).days
                    if days_since > CORRECTION_CONFIG["knowledge_decay_days"]:
                        old_rate = k.get("success_rate", 0.5) or 0.5
                        new_rate = old_rate * (decay_factor ** (days_since // 7))
                        new_rate = max(0.1, new_rate)
                        self.db.update_knowledge_score(k["id"], round(new_rate, 3), date_str)
                except:
                    pass

    def _log_correction_summary(self, date_str, corrections, factor_scores):
        """记录修正摘要"""
        avg_change = sum(abs(c["change"]) for c in corrections) / len(corrections)
        max_change = max(abs(c["change"]) for c in corrections)
        
        summary = (
            f"日期{date_str}: 共{len(corrections)}项修正, "
            f"平均调整幅度{avg_change:.4f}, 最大调整{max_change:.4f}"
        )
        logger.info(f"📊 {summary}")
        
        # 保存到修正日志
        for c in corrections:
            try:
                self.db.execute(
                    """INSERT INTO correction_log 
                       (date, trigger, factor_name, old_weight, new_weight, reason)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (date_str, "daily_verification", c["factor"],
                     c["old_weight"], c["new_weight"], c["reason"]))
            except Exception as e:
                logger.error(f"保存修正日志失败: {e}")

    def adapt_smash_thresholds(self, date_str):
        """
        自适应调整砸盘系数阈值
        根据历史预测准确率，动态调整砸盘系数在预测中的权重
        以及调整阈值（low_pressure/high_pressure）
        """
        try:
            from config import SMASH_CONFIG

            # 获取砸盘系数因子的历史表现
            smash_weight_row = self.db.get_weight("smash_factor")
            if not smash_weight_row:
                return None

            smash_weight_row = dict(smash_weight_row)
            smash_credibility = smash_weight_row.get("credibility", 1.0) or 1.0

            # 根据可信度动态调整砸盘系数的预测权重
            base_weight = SMASH_CONFIG.get("prediction_weight", 0.35)
            if smash_credibility >= 0.8:
                adjusted_weight = min(0.5, base_weight + 0.1)  # 可信度高→提升权重
            elif smash_credibility <= 0.4:
                adjusted_weight = max(0.15, base_weight - 0.1)  # 可信度低→降低权重
            else:
                adjusted_weight = base_weight

            # 根据准确率微调阈值
            low_threshold = SMASH_CONFIG.get("low_pressure_threshold", 4.0)
            high_threshold = SMASH_CONFIG.get("high_pressure_threshold", 7.0)

            # 如果连续误判，适当放宽阈值范围
            consecutive_misses = smash_weight_row.get("consecutive_misses", 0) or 0
            if consecutive_misses >= 2:
                low_threshold = max(3.0, low_threshold - 0.5)  # 降低低抛压阈值
                high_threshold = min(8.0, high_threshold + 0.5)  # 升高高抛压阈值

            result = {
                "adjusted_prediction_weight": round(adjusted_weight, 3),
                "adjusted_low_threshold": round(low_threshold, 1),
                "adjusted_high_threshold": round(high_threshold, 1),
                "smash_credibility": round(smash_credibility, 3),
                "consecutive_misses": consecutive_misses,
            }

            # 保存自适应知识
            self.db.save_knowledge(
                "smash_adaptation",
                f"砸盘系数自适应调整: 权重={adjusted_weight:.3f}, "
                f"低阈值={low_threshold:.1f}, 高阈值={high_threshold:.1f}",
                metadata={"date": date_str, **result}
            )

            logger.info(f"砸盘系数自适应调整: {result}")
            return result

        except Exception as e:
            logger.error(f"砸盘系数自适应调整异常: {e}")
            return None

    def get_model_health(self):
        """
        获取模型健康度报告
        """
        weights = self.db.get_all_weights()
        if not weights:
            return {"status": "未初始化", "factors": []}
        
        factors = []
        total_credibility = 0
        low_cred_count = 0
        
        for w in weights:
            w = dict(w)
            credibility = w.get("credibility", 1.0) or 1.0
            total_credibility += credibility
            if credibility < 0.5:
                low_cred_count += 1
            
            factors.append({
                "name": w["factor_name"],
                "weight": w["weight"],
                "credibility": credibility,
                "consecutive_misses": w.get("consecutive_misses", 0),
                "status": "健康" if credibility > 0.7 else ("警告" if credibility > 0.4 else "危险"),
            })
        
        avg_credibility = total_credibility / len(factors) if factors else 0
        
        # 健康度评分
        if avg_credibility >= 0.8:
            status = "优秀"
        elif avg_credibility >= 0.6:
            status = "良好"
        elif avg_credibility >= 0.4:
            status = "一般"
        else:
            status = "需要改进"
        
        return {
            "status": status,
            "avg_credibility": round(avg_credibility, 3),
            "low_credibility_count": low_cred_count,
            "total_factors": len(factors),
            "factors": factors,
        }

    def export_weights_json(self, filepath):
        """导出权重为JSON文件"""
        weights = self.db.get_all_weights()
        data = {}
        for w in weights:
            w = dict(w)
            data[w["factor_name"]] = {
                "weight": w["weight"],
                "credibility": w.get("credibility", 1.0),
                "consecutive_misses": w.get("consecutive_misses", 0),
                "updated_at": w.get("updated_at", ""),
            }
        
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"权重已导出到 {filepath}")
        return data
