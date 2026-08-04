"""
reporter.py - 报告生成模块
生成每日分析报告（Markdown格式）
包含市场概况、规律总结、预测结果、历史预测回顾、模型自我修正记录
"""
import logging
import json
import os
from datetime import datetime

logger = logging.getLogger(__name__)


class Reporter:
    """报告生成器"""

    def __init__(self, db, knowledge_base=None):
        self.db = db
        self.kb = knowledge_base
        self.report_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        os.makedirs(self.report_dir, exist_ok=True)

    def generate_daily_report(self, date_str, analysis, patterns, predictions, 
                               verifications, corrections, knowledge_match=None):
        """
        生成完整的每日分析报告
        """
        report = []
        report.append(f"# 📊 市场分析报告 - {date_str}")
        report.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")

        # 1. 市场概况
        report.extend(self._section_overview(analysis))
        
        # 1.5 砸盘系数（核心主导指标）
        report.extend(self._section_smash_coefficient(analysis, patterns))
        
        # 2. 模式识别
        report.extend(self._section_patterns(patterns))
        
        # 3. 预测结果
        report.extend(self._section_predictions(predictions))
        
        # 4. 历史预测回顾
        report.extend(self._section_verification(verifications))
        
        # 5. 模型修正记录
        report.extend(self._section_corrections(corrections))
        
        # 6. 知识库匹配
        if knowledge_match:
            report.extend(self._section_knowledge(knowledge_match))
        
        # 7. 模型健康度
        report.extend(self._section_model_health())
        
        report_text = "\n".join(report)
        
        # 保存到文件
        filepath = os.path.join(self.report_dir, f"report_{date_str}.md")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report_text)
        logger.info(f"报告已保存: {filepath}")
        
        return report_text

    def _section_overview(self, analysis):
        """市场概况部分"""
        lines = ["## 一、市场概况", ""]
        
        if not analysis:
            lines.append("*无分析数据*")
            return lines
        
        basic = analysis.get("basic_stats", {})
        sentiment = analysis.get("sentiment_score", 0)
        
        # 基础数据
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 涨停总数 | {basic.get('total_count', 0)} 家 |")
        lines.append(f"| 最高连板 | {basic.get('max_boards', 0)} 板 |")
        lines.append(f"| 2板及以上 | {basic.get('boards_2plus', 0)} 家 |")
        lines.append(f"| 3板及以上 | {basic.get('boards_3plus', 0)} 家 |")
        lines.append(f"| 封单总额 | {basic.get('total_seal_amount', 0):.1f} 亿 |")
        lines.append(f"| 平均换手率 | {basic.get('avg_turnover', 0):.1f}% |")
        lines.append(f"| 情绪得分 | {sentiment:.1f} / 100 |")
        lines.append("")
        
        # 情绪评级
        if sentiment >= 70:
            emoji = "🔴"
            level = "过热"
        elif sentiment >= 55:
            emoji = "🟠"
            level = "偏暖"
        elif sentiment >= 40:
            emoji = "🟡"
            level = "中性"
        elif sentiment >= 25:
            emoji = "🔵"
            level = "偏冷"
        else:
            emoji = "⚪"
            level = "冰点"
        
        lines.append(f"**情绪状态:** {emoji} {level} ({sentiment:.0f}分)")
        lines.append("")
        
        # 连板梯队
        tiers = analysis.get("board_tiers", {})
        lines.append("### 连板梯队")
        for tier_name, tier_data in tiers.items():
            count = tier_data.get("count", 0) if isinstance(tier_data, dict) else 0
            if tier_name in ("高标", "超高标"):
                stocks = tier_data.get("stocks", []) if isinstance(tier_data, dict) else []
                stock_names = ", ".join(f"{s.get('name', '')}({s.get('boards', '')}板)" for s in stocks[:5])
                lines.append(f"- **{tier_name}**: {count}只 - {stock_names}")
            else:
                names = tier_data.get("names", []) if isinstance(tier_data, dict) else []
                lines.append(f"- **{tier_name}**: {count}只 - {', '.join(names[:8])}")
        lines.append("")
        
        # 封板质量
        seal = analysis.get("seal_quality", {})
        lines.append("### 封板质量")
        lines.append(f"- 强封: {seal.get('strong_seal', 0)} 只 ({seal.get('strong_ratio', 0)*100:.0f}%)")
        lines.append(f"- 一字板: {seal.get('one_char_count', 0)} 只")
        lines.append(f"- T字板: {seal.get('t_board_count', 0)} 只")
        lines.append(f"- 换手板: {seal.get('exchange_count', 0)} 只")
        lines.append(f"- 质量评分: {seal.get('quality_score', 0):.0f}/100")
        lines.append("")
        
        # 概念热度
        concept = analysis.get("concept_heat", {})
        top_concepts = concept.get("top_concepts", [])
        if top_concepts:
            lines.append("### 概念热度TOP5")
            for c in top_concepts[:5]:
                name = c.get("concept", "")
                count = c.get("count", 0)
                lines.append(f"- {name}: {count}只涨停")
            lines.append("")
        
        # 晋级分析
        cont = analysis.get("continuation_analysis", {})
        if cont.get("analysis"):
            lines.append(f"### 晋级分析")
            lines.append(f"{cont.get('analysis', '')}")
            lines.append(f"- 晋级率: {cont.get('continuation_rate', 0):.1f}%")
            lines.append("")
        
        # 对比分析
        comp = analysis.get("comparison", {})
        if comp.get("analysis"):
            lines.append(f"### 与前日对比")
            lines.append(f"{comp.get('analysis', '')}")
            lines.append("")
        
        return lines

    def _section_smash_coefficient(self, analysis, patterns):
        """砸盘系数板块（核心主导指标）"""
        lines = ["## 砸盘系数分析（核心指标）", ""]

        if not analysis:
            lines.append("*无砸盘系数数据*")
            return lines

        smash_data = analysis.get("smash_analysis", {})
        smash_value = smash_data.get("smash_coefficient")
        
        if smash_value is None:
            lines.append("*砸盘系数未计算（可能缺少前日数据）*")
            return lines

        signal = smash_data.get("signal", "未知")
        trend = smash_data.get("trend", "未知")
        trade_advice = smash_data.get("trade_advice", "")

        # 信号图标
        if signal == "抛压轻":
            emoji = "🟢"
        elif signal == "抛压重":
            emoji = "🔴"
        else:
            emoji = "🟡"

        lines.append(f"**当日砸盘系数: {smash_value:.2f}** {emoji} {signal}")
        lines.append("")

        # 详细信息
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 砸盘系数 | {smash_value:.2f} |")
        lines.append(f"| 市场信号 | {emoji} {signal} |")
        lines.append(f"| 近5日趋势 | {trend} |")
        
        cycle_phase = smash_data.get("cycle_phase_by_smash", "")
        if cycle_phase:
            lines.append(f"| 砸盘周期判断 | {cycle_phase} |")
        lines.append("")

        # 趋势分析
        trend_analysis = smash_data.get("trend_analysis", "")
        if trend_analysis:
            lines.append(f"**趋势分析:** {trend_analysis}")
            lines.append("")

        # 近期走势（适配简单数值列表）
        trend_values = smash_data.get("trend_values", [])
        if trend_values:
            lines.append("**近期走势:**")
            for v in trend_values:  # 直接遍历数值
                bar = "█" * int(v)
                lines.append(f"- {v:.2f} {bar}")
            lines.append("")

        # 优劣势
        advantage = smash_data.get("advantage", "")
        disadvantage = smash_data.get("disadvantage", "")
        if advantage:
            lines.append(f"**优势:** {advantage}")
        if disadvantage:
            lines.append(f"**劣势:** {disadvantage}")
        if trade_advice:
            lines.append(f"**交易建议:** {trade_advice}")
        lines.append("")

        # 砸盘模式识别
        if patterns:
            smash_pattern = patterns.get("smash_pattern", {})
            if smash_pattern and smash_pattern.get("pattern") != "数据不足":
                risk = smash_pattern.get("risk_level", "未知")
                risk_emoji = {"高": "⚠️", "中": "🟡", "低": "✅"}.get(risk, "❓")
                lines.append(f"**砸盘模式:** {smash_pattern.get('pattern', '')} {risk_emoji} 风险{risk}")
                lines.append(f"- {smash_pattern.get('analysis', '')}")
                lines.append("")

        return lines

    def _section_patterns(self, patterns):
        """模式识别部分"""
        lines = ["## 二、模式识别", ""]
        
        if not patterns:
            lines.append("*无模式识别结果*")
            return lines
        
        # 周期阶段
        phase = patterns.get("cycle_phase", "")
        if phase:
            phase_emoji = {
                "冰点期": "❄️", "启动期": "🌱", "发酵期": "🔥",
                "高潮期": "🎆", "退潮期": "📉", "反包期": "🔄"
            }
            lines.append(f"### 周期阶段: {phase_emoji.get(phase, '')} {phase}")
            lines.append("")
        
        # 龙头
        dragon = patterns.get("dragon_features", {})
        if dragon.get("dragons"):
            lines.append("### 龙头识别")
            for d in dragon["dragons"][:3]:
                marker = "👑" if d.get("is_top_dragon") else "  "
                lines.append(f"- {marker} {d.get('name', '')}({d.get('code', '')}): "
                           f"{d.get('boards', 0)}板, 封单{d.get('seal_amount', 0):.1f}亿, "
                           f"概念: {d.get('concept', '未知')}")
            if dragon.get("dragon_change", {}).get("changed"):
                dc = dragon["dragon_change"]
                lines.append(f"- ⚡ 龙头更替: {dc.get('old_dragon', '')} → {dc.get('new_dragon', '')}")
            lines.append("")
        
        # 概念轮动
        rotation = patterns.get("concept_rotation", {})
        if rotation.get("analysis"):
            lines.append(f"### 概念轮动: {rotation.get('rotation_pattern', '')}")
            lines.append(f"{rotation.get('analysis', '')}")
            if rotation.get("new_concepts"):
                lines.append(f"- 新增概念: {', '.join(rotation['new_concepts'][:5])}")
            if rotation.get("disappeared_concepts"):
                lines.append(f"- 消失概念: {', '.join(rotation['disappeared_concepts'][:5])}")
            lines.append("")
        
        # 封板风格
        seal_style = patterns.get("seal_style", {})
        if seal_style.get("analysis"):
            lines.append(f"### {seal_style.get('analysis', '')}")
            lines.append("")
        
        # 市场结构
        structure = patterns.get("market_structure", {})
        if structure.get("analysis"):
            lines.append(f"### {structure.get('analysis', '')}")
            lines.append("")
        
        return lines

    def _section_predictions(self, predictions):
        """预测结果部分"""
        lines = ["## 三、明日预测", ""]
        
        if not predictions:
            lines.append("*无预测结果*")
            return lines
        
        for pred_type, pred_data in predictions.items():
            if not isinstance(pred_data, dict):
                continue
            
            predicted = pred_data.get("predicted", "")
            confidence = pred_data.get("confidence", 0)
            reason = pred_data.get("reason", "")
            
            # 置信度显示
            if confidence >= 0.7:
                conf_emoji = "🟢"
            elif confidence >= 0.5:
                conf_emoji = "🟡"
            else:
                conf_emoji = "🔴"
            
            type_names = {
                "limit_up_count": "涨停数量",
                "max_continuous_boards": "最高连板",
                "main_concept": "主线概念",
                "sentiment_direction": "情绪方向",
                "operation_advice": "操作建议",
                "smash_prediction": "砸盘系数预测",
            }
            
            name = type_names.get(pred_type, pred_type)
            lines.append(f"### {name}")
            lines.append(f"- 预测: **{predicted}** {conf_emoji} (置信度: {confidence*100:.0f}%)")
            
            if pred_type == "limit_up_count":
                pred_range = pred_data.get("range", ())
                if pred_range:
                    lines.append(f"- 预测区间: {pred_range[0]} ~ {pred_range[1]} 家")
            
            if pred_type == "main_concept":
                candidates = pred_data.get("top_candidates", [])
                if candidates:
                    lines.append(f"- 候选概念: {', '.join(candidates[:3])}")
            
            if pred_type == "operation_advice":
                detail = pred_data.get("detail", "")
                if detail:
                    lines.append(f"- 详细说明: {detail}")
            
            if reason:
                lines.append(f"- 分析依据: {reason}")
            lines.append("")
        
        return lines

    def _section_verification(self, verifications):
        """历史预测回顾"""
        lines = ["## 四、预测验证回顾", ""]
        
        if not verifications:
            lines.append("*本期无预测验证*")
            return lines
        
        total_score = 0
        lines.append("| 预测类型 | 预测值 | 实际值 | 得分 | 评价 |")
        lines.append("|----------|--------|--------|------|------|")
        
        for v in verifications:
            score = v.get("score", 0)
            total_score += score
            if score >= 0.8:
                eval_emoji = "✅"
            elif score >= 0.5:
                eval_emoji = "⚠️"
            else:
                eval_emoji = "❌"
            
            type_names = {
                "limit_up_count": "涨停数量",
                "max_continuous_boards": "最高连板",
                "main_concept": "主线概念",
                "sentiment_direction": "情绪方向",
                "operation_advice": "操作建议",
                "smash_prediction": "砸盘系数预测",
            }
            
            name = type_names.get(v.get("type", ""), v.get("type", ""))
            lines.append(f"| {name} | {v.get('predicted', '')} | {v.get('actual', '')} | "
                        f"{score:.2f} | {eval_emoji} |")
        
        avg_score = total_score / len(verifications) if verifications else 0
        lines.append("")
        lines.append(f"**平均准确率: {avg_score:.2f}** "
                    f"({'✅优秀' if avg_score >= 0.7 else '⚠️一般' if avg_score >= 0.4 else '❌需改进'})")
        lines.append("")
        
        return lines

    def _section_corrections(self, corrections):
        """模型修正记录"""
        lines = ["## 五、模型自我修正", ""]
        
        if not corrections:
            lines.append("*本期无修正*")
            return lines
        
        lines.append("| 因素 | 旧权重 | 新权重 | 变化 | 原因 |")
        lines.append("|------|--------|--------|------|------|")
        
        for c in corrections:
            change = c.get("change", 0)
            arrow = "↑" if change > 0 else "↓"
            lines.append(f"| {c.get('factor', '')} | {c.get('old_weight', 0):.3f} | "
                        f"{c.get('new_weight', 0):.3f} | {arrow}{abs(change):.3f} | "
                        f"{c.get('reason', '')[:50]} |")
        
        lines.append("")
        return lines

    def _section_knowledge(self, knowledge_match):
        """知识库匹配部分"""
        lines = ["## 六、知识库匹配", ""]
        
        advice = knowledge_match.get("advice", "")
        if advice:
            lines.append(f"**历史经验参考:** {advice}")
            lines.append("")
        
        similar = knowledge_match.get("similar_historical", [])
        if similar:
            lines.append("### 相似历史情境")
            for s in similar[:3]:
                k = s.get("knowledge", {})
                lines.append(f"- {k.get('description', '')} (匹配度: {s.get('score', 0)*100:.0f}%)")
            lines.append("")
        
        return lines

    def _section_model_health(self):
        """模型健康度部分"""
        lines = ["## 七、模型健康度", ""]
        
        weights = self.db.get_all_weights()
        if not weights:
            lines.append("*模型尚未初始化*")
            return lines
        
        lines.append("| 因素 | 权重 | 可信度 | 状态 |")
        lines.append("|------|------|--------|------|")
        
        total_cred = 0
        for w in weights:
            w = dict(w)
            cred = w.get("credibility", 1.0) or 1.0
            total_cred += cred
            
            if cred >= 0.7:
                status = "🟢 健康"
            elif cred >= 0.4:
                status = "🟡 警告"
            else:
                status = "🔴 危险"
            
            lines.append(f"| {w['factor_name']} | {w['weight']:.3f} | {cred:.2f} | {status} |")
        
        avg_cred = total_cred / len(weights) if weights else 0
        lines.append("")
        lines.append(f"**模型整体可信度: {avg_cred:.2f}**")
        lines.append("")
        
        return lines

    def generate_backtest_report(self, results, date_range=""):
        """生成回测汇总报告"""
        report = []
        report.append(f"# 📈 回测报告")
        report.append(f"> 回测区间: {date_range}")
        report.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")
        
        if not results:
            report.append("*无回测结果*")
            return "\n".join(report)
        
        # 按类型汇总
        type_scores = {}
        for r in results:
            for v in r.get("verifications", []):
                t = v.get("type", "unknown")
                if t not in type_scores:
                    type_scores[t] = []
                type_scores[t].append(v.get("score", 0))
        
        report.append("## 各维度预测准确率")
        report.append("")
        report.append("| 预测类型 | 样本数 | 平均准确率 | 优秀率(≥0.6) |")
        report.append("|----------|--------|------------|--------------|")
        
        total_all = 0
        score_all = 0
        for t, scores in type_scores.items():
            avg = sum(scores) / len(scores) if scores else 0
            good = sum(1 for s in scores if s >= 0.6)
            good_rate = good / len(scores) if scores else 0
            total_all += len(scores)
            score_all += sum(scores)
            
            type_names = {
                "limit_up_count": "涨停数量",
                "max_continuous_boards": "最高连板",
                "main_concept": "主线概念",
                "sentiment_direction": "情绪方向",
                "operation_advice": "操作建议",
                "smash_prediction": "砸盘系数预测",
            }
            report.append(f"| {type_names.get(t, t)} | {len(scores)} | "
                        f"{avg:.3f} | {good_rate*100:.1f}% |")
        
        overall_avg = score_all / total_all if total_all else 0
        report.append("")
        report.append(f"**整体平均准确率: {overall_avg:.3f} (共{total_all}次预测)**")
        report.append("")
        
        # 权重变化记录
        report.append("## 权重变化轨迹")
        report.append("")
        corrections_count = len(results)
        report.append(f"共执行 {corrections_count} 次自我修正")
        report.append("")
        
        return "\n".join(report)

    def generate_status_report(self):
        """生成系统状态报告"""
        report = []
        report.append("# 📊 系统状态报告")
        report.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")
        
        # 数据概览
        all_dates = self.db.get_all_dates()
        report.append("## 数据概览")
        report.append(f"- 数据日期范围: {all_dates[0] if all_dates else 'N/A'} ~ {all_dates[-1] if all_dates else 'N/A'}")
        report.append(f"- 交易日数量: {len(all_dates)}")
        report.append("")
        
        # 预测统计
        pred_rows = self.db.fetch_all(
            "SELECT COUNT(*) as total, SUM(CASE WHEN verified=1 THEN 1 ELSE 0 END) as verified FROM prediction_records")
        if pred_rows:
            pred = dict(pred_rows[0])
            report.append("## 预测统计")
            report.append(f"- 总预测数: {pred.get('total', 0)}")
            report.append(f"- 已验证: {pred.get('verified', 0)}")
            report.append("")
        
        # 知识库统计
        if self.kb:
            stats = self.kb.get_stats()
            report.append("## 知识库统计")
            report.append(f"- 总知识条目: {stats.get('total', 0)}")
            for pt, info in stats.get("by_type", {}).items():
                report.append(f"- {pt}: {info['count']}条 (平均成功率{info['avg_success']*100:.0f}%)")
            report.append("")
        
        # 模型健康度
        weights = self.db.get_all_weights()
        if weights:
            report.append("## 模型权重状态")
            for w in weights:
                w = dict(w)
                report.append(f"- {w['factor_name']}: 权重={w['weight']:.3f}, "
                            f"可信度={w.get('credibility', 1.0):.2f}")
            report.append("")
        
        return "\n".join(report)