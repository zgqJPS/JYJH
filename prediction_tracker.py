"""
prediction_tracker.py - 预测追踪与验证模块
将前一天的预测与实际数据对比，计算各维度预测准确率
"""
import logging
import json
from datetime import datetime

logger = logging.getLogger(__name__)


class PredictionTracker:
    """预测追踪器"""

    def __init__(self, db):
        self.db = db

    def verify_predictions_for_date(self, target_date, actual_analysis):
        """
        验证某日的所有预测
        target_date: 被预测的日期（实际发生日）
        actual_analysis: 该日的实际分析结果
        返回: 验证结果列表
        """
        if not actual_analysis:
            logger.warning(f"{target_date} 无实际分析数据，跳过验证")
            return []

        # 获取针对该日的未验证预测
        predictions = self.db.fetch_all(
            "SELECT * FROM prediction_records WHERE date = ? AND verified = 0",
            (target_date,))
        
        if not predictions:
            logger.info(f"{target_date} 无待验证预测")
            return []

        results = []
        for pred in predictions:
            pred = dict(pred)
            pred_type = pred["prediction_type"]
            predicted_content = pred["content"]
            
            # 根据预测类型计算准确率
            accuracy = self._calculate_accuracy(
                pred_type, predicted_content, actual_analysis, target_date)
            
            # 更新数据库
            self.db.verify_prediction(
                pred["id"],
                actual_result=json.dumps(self._extract_actual(pred_type, actual_analysis), ensure_ascii=False),
                accuracy_score=accuracy["score"],
            )
            
            result = {
                "prediction_id": pred["id"],
                "type": pred_type,
                "predicted": predicted_content,
                "actual": accuracy.get("actual", ""),
                "score": accuracy["score"],
                "detail": accuracy.get("detail", ""),
            }
            results.append(result)
            logger.info(f"验证 [{pred_type}]: 预测={predicted_content}, 实际={accuracy.get('actual','')}, 得分={accuracy['score']:.2f}")

        # 保存到回测记录表（如果存在）
        self._save_backtest_records(target_date, results)
        
        return results

    def _calculate_accuracy(self, pred_type, predicted, actual_analysis, date):
        """
        计算单个预测的准确率
        返回: {"score": 0-1, "actual": ..., "detail": ...}
        """
        try:
            if pred_type == "limit_up_count":
                return self._verify_limit_up_count(predicted, actual_analysis)
            elif pred_type == "max_continuous_boards":
                return self._verify_max_boards(predicted, actual_analysis)
            elif pred_type == "main_concept":
                return self._verify_main_concept(predicted, actual_analysis, date)
            elif pred_type == "sentiment_direction":
                return self._verify_sentiment(predicted, actual_analysis, date)
            elif pred_type == "operation_advice":
                return self._verify_advice(predicted, actual_analysis)
            elif pred_type == "smash_prediction":
                return self._verify_smash_prediction(predicted, actual_analysis, date)
            else:
                return {"score": 0.0, "actual": "未知类型", "detail": f"未实现的验证类型: {pred_type}"}
        except Exception as e:
            logger.error(f"验证计算失败: {e}")
            return {"score": 0.0, "actual": "错误", "detail": str(e)}

    def _verify_limit_up_count(self, predicted, analysis):
        """验证涨停数量预测"""
        actual_count = analysis.get("basic_stats", {}).get("total_count", 0)
        try:
            pred_num = float(predicted)
        except (ValueError, TypeError):
            pred_num = 50
        
        # 完全准确：误差<=5%；基本准确：误差<=15%；偏差大：>15%
        if actual_count == 0:
            return {"score": 0.0, "actual": 0, "detail": "实际为0"}
        
        error_pct = abs(pred_num - actual_count) / actual_count
        
        if error_pct <= 0.05:
            score = 1.0
        elif error_pct <= 0.10:
            score = 0.8
        elif error_pct <= 0.15:
            score = 0.6
        elif error_pct <= 0.25:
            score = 0.4
        elif error_pct <= 0.40:
            score = 0.2
        else:
            score = 0.0
        
        return {
            "score": score,
            "actual": actual_count,
            "detail": f"预测{pred_num:.0f}，实际{actual_count}，误差{error_pct*100:.1f}%",
        }

    def _verify_max_boards(self, predicted, analysis):
        """验证最高连板预测"""
        actual_max = analysis.get("basic_stats", {}).get("max_boards", 0)
        try:
            pred_num = float(predicted)
        except (ValueError, TypeError):
            pred_num = 3
        
        diff = abs(pred_num - actual_max)
        if diff == 0:
            score = 1.0
        elif diff == 1:
            score = 0.7
        elif diff == 2:
            score = 0.4
        else:
            score = 0.1
        
        return {
            "score": score,
            "actual": actual_max,
            "detail": f"预测{pred_num:.0f}板，实际{actual_max}板，差{diff:.0f}板",
        }

    def _verify_main_concept(self, predicted, analysis, date):
        """验证主线概念预测"""
        # 获取实际的top概念
        concepts = analysis.get("concept_heat", {})
        top_concepts = concepts.get("top_concepts", [])
        
        if not top_concepts:
            # 从数据库获取
            concept_data = self.db.get_concept_data(date)
            if concept_data:
                top_concepts = sorted([dict(c) for c in concept_data], 
                                     key=lambda x: x.get("count", 0), reverse=True)[:5]
                top_concepts = [{"concept": c.get("concept", ""), "count": c.get("count", 0)} for c in top_concepts]
        
        actual_top = top_concepts[0].get("concept", "") if top_concepts else "未知"
        actual_top3 = [c.get("concept", "") for c in top_concepts[:3]]
        
        # 完全命中
        if predicted == actual_top:
            score = 1.0
        elif predicted in actual_top3:
            score = 0.7  # 在top3中
        elif predicted in [c.get("concept", "") for c in top_concepts[:5]]:
            score = 0.4
        else:
            score = 0.0
        
        return {
            "score": score,
            "actual": actual_top,
            "detail": f"预测主线={predicted}，实际top={actual_top}，top3={actual_top3}",
        }

    def _verify_sentiment(self, predicted, analysis, date):
        """验证情绪方向预测"""
        actual_sentiment = analysis.get("sentiment_score", 50)
        
        # 获取前一日情绪
        all_dates = self.db.get_all_dates()
        if date in all_dates:
            idx = all_dates.index(date)
            if idx > 0:
                prev_date = all_dates[idx - 1]
                prev_stocks = self.db.get_limit_up_data(prev_date)
                if prev_stocks:
                    prev_count = len(prev_stocks)
                    curr_count = analysis.get("basic_stats", {}).get("total_count", 50)
                    if curr_count > prev_count * 1.15:
                        actual_direction = "升温"
                    elif curr_count < prev_count * 0.85:
                        actual_direction = "降温"
                    else:
                        actual_direction = "震荡"
                else:
                    actual_direction = "震荡"
            else:
                actual_direction = "震荡"
        else:
            actual_direction = "震荡"
        
        if predicted == actual_direction:
            score = 1.0
        elif (predicted in ("升温", "降温") and actual_direction == "震荡") or              (predicted == "震荡" and actual_direction in ("升温", "降温")):
            score = 0.4  # 方向偏差但幅度不大
        else:
            score = 0.0  # 完全反向
        
        return {
            "score": score,
            "actual": actual_direction,
            "detail": f"预测{predicted}，实际{actual_direction}（情绪分{actual_sentiment:.0f}）",
        }

    def _verify_advice(self, predicted, analysis):
        """验证操作建议 - 基于结果反推"""
        sentiment = analysis.get("sentiment_score", 50)
        
        # 根据实际情绪推断当时的"正确建议"
        if sentiment >= 70:
            correct_advice = "保守"
        elif sentiment >= 55:
            correct_advice = "积极"
        elif sentiment >= 40:
            correct_advice = "中性偏保守"
        elif sentiment >= 25:
            correct_advice = "保守"
        else:
            correct_advice = "观望"
        
        # 建议的验证比较宽松
        aggressive_set = {"积极", "中性偏保守"}
        conservative_set = {"保守", "观望"}
        
        if predicted == correct_advice:
            score = 1.0
        elif (predicted in aggressive_set and correct_advice in aggressive_set) or              (predicted in conservative_set and correct_advice in conservative_set):
            score = 0.6
        else:
            score = 0.2
        
        return {
            "score": score,
            "actual": correct_advice,
            "detail": f"建议{predicted}，合理建议={correct_advice}",
        }

    def _verify_smash_prediction(self, predicted, analysis, date):
        """
        验证砸盘系数预测
        比较预测的方向（升温/降温/震荡）与实际砸盘系数变化
        """
        try:
            # 获取当日砸盘系数
            smash_row = self.db.get_smash_coefficient(date)
            if not smash_row:
                return {"score": 0.0, "actual": "无数据", "detail": "当日砸盘系数未计算"}
            
            smash_row = dict(smash_row)
            current_smash = smash_row.get("smash_coefficient", 5.0)
            
            # 获取前日砸盘系数
            all_dates = self.db.get_all_dates()
            if date not in all_dates:
                return {"score": 0.0, "actual": "日期无效", "detail": ""}
            
            idx = all_dates.index(date)
            if idx <= 0:
                return {"score": 0.0, "actual": "无前日数据", "detail": ""}
            
            prev_date = all_dates[idx - 1]
            prev_row = self.db.get_smash_coefficient(prev_date)
            if not prev_row:
                return {"score": 0.0, "actual": "无前日砸盘数据", "detail": ""}
            
            prev_row = dict(prev_row)
            prev_smash = prev_row.get("smash_coefficient", 5.0)
            
            # 判断实际方向
            change = current_smash - prev_smash
            if change > 1.5:
                actual_direction = "升温"  # 抛压加剧 = 市场升温（过热）
            elif change < -1.5:
                actual_direction = "降温"  # 抛压减轻 = 市场降温
            else:
                actual_direction = "震荡"
            
            # 比较预测与实际
            if predicted == actual_direction:
                score = 1.0
            elif (predicted in ("升温", "降温") and actual_direction == "震荡") or \
                 (predicted == "震荡" and actual_direction in ("升温", "降温")):
                score = 0.4
            else:
                score = 0.0
            
            return {
                "score": score,
                "actual": actual_direction,
                "detail": f"预测{predicted}，实际{actual_direction}（砸盘系数{prev_smash:.1f}→{current_smash:.1f}，变化{change:+.1f}）",
            }
        except Exception as e:
            return {"score": 0.0, "actual": "异常", "detail": str(e)}

    def _extract_actual(self, pred_type, analysis):
        """提取实际值用于存储"""
        if pred_type == "limit_up_count":
            return {"count": analysis.get("basic_stats", {}).get("total_count", 0)}
        elif pred_type == "max_continuous_boards":
            return {"max_boards": analysis.get("basic_stats", {}).get("max_boards", 0)}
        elif pred_type == "sentiment_direction":
            return {"sentiment_score": analysis.get("sentiment_score", 0)}
        else:
            return {}

    def _save_backtest_records(self, target_date, results):
        """保存回测记录"""
        for r in results:
            try:
                self.db.execute(
                    """INSERT INTO backtest_records 
                       (backtest_date, target_date, prediction_type, predicted_value, 
                        actual_value, accuracy_score)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (datetime.now().strftime("%Y-%m-%d"), target_date,
                     r["type"], r.get("predicted", ""), str(r.get("actual", "")),
                     r.get("score", 0)))
            except Exception as e:
                logger.debug(f"保存回测记录失败: {e}")

    def get_accuracy_summary(self, prediction_type=None, days=30):
        """
        获取准确率汇总
        返回各预测类型的平均准确率
        """
        sql = """SELECT prediction_type, 
                 COUNT(*) as total, 
                 AVG(accuracy_score) as avg_score,
                 SUM(CASE WHEN accuracy_score >= 0.6 THEN 1 ELSE 0 END) as good_count
                 FROM prediction_records 
                 WHERE verified = 1"""
        params = []
        if prediction_type:
            sql += " AND prediction_type = ?"
            params.append(prediction_type)
        sql += " GROUP BY prediction_type"
        
        rows = self.db.fetch_all(sql, params)
        summary = {}
        for r in rows:
            r = dict(r)
            summary[r["prediction_type"]] = {
                "total": r["total"],
                "avg_score": round(r["avg_score"] or 0, 3),
                "good_rate": round(r["good_count"] / r["total"], 3) if r["total"] > 0 else 0,
            }
        return summary

    def get_recent_accuracy(self, days=10):
        """获取近期整体准确率"""
        sql = """SELECT AVG(accuracy_score) as avg_score, COUNT(*) as total
                 FROM prediction_records 
                 WHERE verified = 1"""
        row = self.db.fetch_one(sql)
        if row:
            row = dict(row)
            return {
                "avg_score": round(row.get("avg_score") or 0, 3),
                "total": row.get("total", 0),
            }
        return {"avg_score": 0, "total": 0}
