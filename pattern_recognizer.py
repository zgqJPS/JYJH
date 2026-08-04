"""
pattern_recognizer.py - 模式识别模块
识别市场周期阶段、龙头特征、概念轮动模式、封板风格变化
砸盘系数作为周期判断的核心参数
统一使用 xgt_limit_up_detail 表的字段：limit_up_days, seal_ratio
"""
import logging
from collections import Counter, defaultdict

logger = logging.getLogger(__name__)


class PatternRecognizer:
    """市场模式识别器"""

    # 周期阶段定义
    CYCLE_PHASES = ["冰点期", "启动期", "发酵期", "高潮期", "退潮期", "反包期"]

    def __init__(self, db):
        self.db = db
        from smash_coefficient import SmashCoefficientCalculator
        self.smash_calc = SmashCoefficientCalculator(db)

    def recognize_all(self, date_str, analysis_result):
        """
        综合识别所有模式
        返回模式识别结果字典
        """
        if not analysis_result:
            return {}

        result = {
            "date": date_str,
            "cycle_phase": self.recognize_cycle_phase(date_str, analysis_result),
            "dragon_features": self.recognize_dragon(date_str, analysis_result),
            "concept_rotation": self.recognize_concept_rotation(date_str, analysis_result),
            "seal_style_change": self.recognize_seal_style(date_str, analysis_result),
            "market_structure": self.recognize_market_structure(date_str, analysis_result),
            "smash_pattern": self.recognize_smash_pattern(date_str, analysis_result),
        }

        self._save_patterns(result)
        return result

    def recognize_cycle_phase(self, date_str, analysis):
        """
        识别市场周期阶段（砸盘系数为核心参数）
        综合判断：砸盘系数周期 + 传统情绪周期，砸盘系数权重最高
        """
        basic = analysis.get("basic_stats", {})
        sentiment = analysis.get("sentiment_score", 0)
        count = basic.get("total_count", 0)
        max_b = basic.get("max_boards", 0)

        smash_data = analysis.get("smash_analysis", {})
        smash_value = smash_data.get("smash_coefficient")
        smash_cycle = smash_data.get("cycle_phase_by_smash", "")

        if smash_cycle and smash_value is not None:
            if smash_cycle == "高潮期":
                return "高潮期"
            elif smash_cycle == "主升期":
                return "发酵期"
            elif smash_cycle == "补涨期":
                return "启动期"
            elif smash_cycle == "轮动/低迷期":
                if sentiment < 25:
                    return "冰点期"
                elif sentiment < 40:
                    return "退潮期"
                else:
                    return "发酵期"

        prev_analysis = self._get_previous_analysis(date_str)
        if prev_analysis:
            prev_sentiment = prev_analysis.get("sentiment_score", 50)
            prev_count = prev_analysis.get("basic_stats", {}).get("total_count", 50)
            if prev_sentiment < 40 and sentiment > 55 and count > prev_count * 1.3:
                return "反包期"
            if prev_sentiment > 65 and sentiment < prev_sentiment - 15:
                return "退潮期"

        if count < 20 and sentiment < 25:
            return "冰点期"
        elif count < 40 and sentiment < 45:
            return "启动期"
        elif count >= 40 and count < 70 and sentiment >= 45 and sentiment < 65:
            return "发酵期"
        elif count >= 70 and sentiment >= 65 and max_b >= 5:
            return "高潮期"
        elif count >= 60 and sentiment >= 55:
            return "发酵期"
        elif sentiment < 35:
            return "退潮期"
        else:
            if prev_analysis:
                prev_sentiment = prev_analysis.get("sentiment_score", 50)
                if sentiment > prev_sentiment + 10:
                    return "启动期" if sentiment < 50 else "发酵期"
                elif sentiment < prev_sentiment - 10:
                    return "退潮期"
            return "发酵期"

    def recognize_dragon(self, date_str, analysis):
        """
        识别龙头特征
        龙头标准：最高连板 + 概念热度 + 封板质量
        使用 limit_up_days 替代 continuous_boards
        """
        stocks = self.db.get_limit_up_data(date_str)
        if not stocks:
            return {"dragons": [], "analysis": "无数据"}

        stocks = [dict(s) for s in stocks]
        
        # 按连板数排序 - 使用 limit_up_days
        sorted_stocks = sorted(stocks, key=lambda x: (x.get("limit_up_days", 1) or 1), reverse=True)
        
        max_board = sorted_stocks[0].get("limit_up_days", 1) or 1 if sorted_stocks else 1
        
        # 预加载概念数据
        xgb_detail = self.db.get_xgb_detail(date_str)
        concept_map = {}
        for x in xgb_detail:
            x = dict(x)
            concept_map[x.get("code", "")] = x.get("concept", "")

        dragons = []
        for s in sorted_stocks[:5]:
            boards = s.get("limit_up_days", 1) or 1
            if boards >= max(2, max_board - 1):
                concept = concept_map.get(s.get("code", ""), "")
                dragons.append({
                    "code": s.get("code", ""),
                    "name": s.get("name", ""),
                    "boards": boards,
                    "seal_ratio": s.get("seal_ratio", 0),
                    "concept": concept,
                    "is_top_dragon": boards == max_board,
                })

        dragon_change = self._check_dragon_change(date_str, dragons)

        return {
            "dragons": dragons,
            "max_board": max_board,
            "dragon_change": dragon_change,
            "analysis": self._summarize_dragon(dragons, max_board),
        }

    def _check_dragon_change(self, date_str, dragons):
        """检查龙头是否更替 - 使用 limit_up_days"""
        all_dates = self.db.get_all_dates()
        current_idx = all_dates.index(date_str) if date_str in all_dates else -1
        if current_idx <= 0:
            return {"changed": False}
        
        prev_date = all_dates[current_idx - 1]
        prev_stocks = self.db.get_limit_up_data(prev_date)
        if not prev_stocks:
            return {"changed": False}
        
        prev_stocks = [dict(s) for s in prev_stocks]
        prev_sorted = sorted(prev_stocks, key=lambda x: x.get("limit_up_days", 1) or 1, reverse=True)
        prev_top = prev_sorted[0] if prev_sorted else None
        
        current_top = dragons[0] if dragons else None
        
        if prev_top and current_top:
            if prev_top.get("code") != current_top.get("code"):
                return {
                    "changed": True,
                    "old_dragon": prev_top.get("name", ""),
                    "new_dragon": current_top.get("name", ""),
                }
        return {"changed": False}

    def _summarize_dragon(self, dragons, max_board):
        """总结龙头情况"""
        if not dragons:
            return "无明确龙头"
        top = dragons[0]
        if max_board >= 5:
            return f"绝对龙头: {top.get('name', '')}({max_board}板)，市场高度充分打开"
        elif max_board >= 3:
            return f"阶段龙头: {top.get('name', '')}({max_board}板)，高度适中"
        else:
            return f"龙头不突出，最高仅{max_board}板，市场缺乏标杆"

    def recognize_concept_rotation(self, date_str, analysis):
        """
        识别概念轮动模式
        对比近期概念变化趋势（支持选股宝概念数据）
        """
        all_dates = self.db.get_all_dates()
        current_idx = all_dates.index(date_str) if date_str in all_dates else -1
        
        if current_idx < 2:
            return {"rotation_pattern": "数据不足", "main_concepts": []}
        
        recent_concepts = []
        for i in range(max(0, current_idx - 2), current_idx + 1):
            d = all_dates[i]
            concepts = self.db.get_concept_statistics(d)
            if concepts:
                top = sorted([dict(c) for c in concepts], key=lambda x: x.get("count", 0), reverse=True)[:5]
                recent_concepts.append({"date": d, "top": top})
                continue
            xgb_concepts = self.db.get_xgb_concepts_by_date(d)
            if xgb_concepts:
                top = [{"concept": k, "count": v}
                       for k, v in sorted(xgb_concepts.items(), key=lambda x: x[1], reverse=True)][:5]
                recent_concepts.append({"date": d, "top": top})
                continue
            recent_concepts.append({"date": d, "top": []})
        
        if len(recent_concepts) < 2:
            return {"rotation_pattern": "数据不足", "main_concepts": []}
        
        today_top = set(c.get("concept", "") for c in recent_concepts[-1]["top"])
        prev_top = set(c.get("concept", "") for c in recent_concepts[-2]["top"]) if len(recent_concepts) >= 2 else set()
        
        new_concepts = today_top - prev_top
        persisted = today_top & prev_top
        disappeared = prev_top - today_top
        
        if len(new_concepts) >= 3:
            pattern = "概念发散"
        elif len(persisted) >= 3:
            pattern = "主线明确"
        elif len(new_concepts) == 0:
            pattern = "概念固化"
        else:
            pattern = "概念轮动"
        
        return {
            "rotation_pattern": pattern,
            "new_concepts": list(new_concepts),
            "persisted_concepts": list(persisted),
            "disappeared_concepts": list(disappeared),
            "main_concepts": [c.get("concept", "") for c in recent_concepts[-1]["top"][:3]],
            "analysis": (
                f"概念轮动模式: {pattern}；"
                f"新增{len(new_concepts)}个，持续{len(persisted)}个，消失{len(disappeared)}个"
            ),
        }

    def recognize_seal_style(self, date_str, analysis):
        """
        识别封板风格变化
        一字板占比、T字板占比、换手板占比的变化
        """
        stocks = self.db.get_limit_up_data(date_str)
        if not stocks:
            return {"style": "无数据"}
        
        stocks = [dict(s) for s in stocks]
        total = len(stocks)
        
        one_char = sum(1 for s in stocks if "一字" in (s.get("seal_style", "") or ""))
        t_board = sum(1 for s in stocks if "T" in (s.get("seal_style", "") or "").upper())
        exchange = sum(1 for s in stocks if "换手" in (s.get("seal_style", "") or ""))
        
        one_pct = one_char / total * 100 if total else 0
        t_pct = t_board / total * 100 if total else 0
        ex_pct = exchange / total * 100 if total else 0
        
        if one_pct > 40:
            style = "一字板主导"
        elif t_pct > 50:
            style = "T字板为主"
        elif ex_pct > 50:
            style = "换手板为主"
        else:
            style = "混合风格"
        
        return {
            "style": style,
            "one_char_pct": round(one_pct, 1),
            "t_board_pct": round(t_pct, 1),
            "exchange_pct": round(ex_pct, 1),
            "analysis": f"封板风格: {style}（一字{one_pct:.0f}%/T字{t_pct:.0f}%/换手{ex_pct:.0f}%）",
        }

    def recognize_market_structure(self, date_str, analysis):
        """
        识别市场结构特征
        连板梯队完整性、龙头与跟风的关系等
        """
        basic = analysis.get("basic_stats", {})
        tiers = analysis.get("board_tiers", {})
        
        has_1 = "首板" in tiers
        has_2 = "二板" in tiers and tiers["二板"].get("count", 0) > 0
        has_3 = "三板" in tiers and tiers["三板"].get("count", 0) > 0
        has_high = ("高标" in tiers and tiers["高标"].get("count", 0) > 0) or \
                   ("超高标" in tiers and tiers["超高标"].get("count", 0) > 0)
        
        tier_count = sum([has_1, has_2, has_3, has_high])
        
        if tier_count >= 4:
            structure = "梯队完整"
        elif tier_count >= 3:
            structure = "梯队较完整"
        elif tier_count >= 2:
            structure = "梯队断层"
        else:
            structure = "梯队断裂"
        
        return {
            "structure": structure,
            "tier_completeness": tier_count,
            "has_first_board": has_1,
            "has_mid_board": has_2 or has_3,
            "has_high_board": has_high,
            "analysis": f"市场结构: {structure}，梯队覆盖{tier_count}/4层",
        }

    def recognize_smash_pattern(self, date_str, analysis):
        """
        识别砸盘系数的周期性模式
        """
        try:
            smash_data = analysis.get("smash_analysis", {})
            trend_values = smash_data.get("trend_values", [])
            smash_value = smash_data.get("smash_coefficient")
            trend = smash_data.get("trend", "未知")

            if not trend_values or smash_value is None:
                return {
                    "pattern": "数据不足",
                    "smash_value": smash_value,
                    "analysis": "砸盘系数历史数据不足，无法识别模式",
                    "risk_level": "未知",
                }

            values = [v.get("value", 0) for v in trend_values]
            current = values[-1] if values else smash_value

            if len(values) >= 3:
                rising_count = sum(1 for i in range(1, len(values)) if values[i] > values[i-1])
                falling_count = sum(1 for i in range(1, len(values)) if values[i] < values[i-1])
                sudden_spike = any(values[i] - values[i-1] > 3 for i in range(1, len(values)))

                if sudden_spike:
                    pattern = "突然飙升"
                    analysis_text = "砸盘系数突然飙升，龙头可能断板，注意风险控制"
                    risk_level = "高"
                elif rising_count >= len(values) - 2:
                    pattern = "连续上升"
                    analysis_text = "砸盘系数连续上升，抛压加剧，市场可能进入退潮期"
                    risk_level = "高"
                elif falling_count >= len(values) - 2:
                    pattern = "连续下降"
                    analysis_text = "砸盘系数连续下降，抛压减轻，市场可能回暖"
                    risk_level = "低"
                elif current > 7:
                    pattern = "高压持续"
                    analysis_text = f"砸盘系数{current:.1f}持续高位，市场抛压沉重"
                    risk_level = "高"
                elif current < 4:
                    pattern = "低压运行"
                    analysis_text = f"砸盘系数{current:.1f}持续低位，市场做多氛围好"
                    risk_level = "低"
                else:
                    pattern = "正常波动"
                    analysis_text = f"砸盘系数{current:.1f}处于正常区间"
                    risk_level = "中"
            else:
                pattern = "观察中"
                analysis_text = "数据积累中，需更多交易日判断模式"
                risk_level = "中"

            return {
                "pattern": pattern,
                "smash_value": smash_value,
                "trend": trend,
                "analysis": analysis_text,
                "risk_level": risk_level,
                "recent_values": values,
            }

        except Exception as e:
            logger.error(f"砸盘模式识别异常: {e}")
            return {
                "pattern": "异常",
                "smash_value": None,
                "analysis": f"识别异常: {e}",
                "risk_level": "未知",
            }

    def _get_previous_analysis(self, date_str):
        """获取前一日的分析结果"""
        all_dates = self.db.get_all_dates()
        current_idx = all_dates.index(date_str) if date_str in all_dates else -1
        if current_idx <= 0:
            return None
        
        prev_date = all_dates[current_idx - 1]
        snapshots = self.db.get_daily_snapshots(start_date=prev_date, end_date=prev_date)
        if snapshots:
            s = dict(snapshots[0])
            return {
                "sentiment_score": s.get("sentiment_score", 50),
                "basic_stats": {
                    "total_count": s.get("limit_up_count", 0),
                    "max_boards": s.get("max_continuous_boards", 0),
                },
            }
        return None

    def _save_patterns(self, patterns):
        """将识别到的模式保存到知识库"""
        try:
            phase = patterns.get("cycle_phase", "")
            if phase:
                self.db.save_knowledge("cycle_phase", phase, 
                    metadata={"date": patterns.get("date", ""), "full_pattern": str(patterns.get("market_structure", ""))})
            
            dragon = patterns.get("dragon_features", {})
            if dragon.get("dragons"):
                top = dragon["dragons"][0]
                self.db.save_knowledge("dragon", 
                    f"{top.get('name', '')}({top.get('boards', 0)}板)",
                    metadata={"date": patterns.get("date", ""), "code": top.get("code", "")})
            
            concept_rot = patterns.get("concept_rotation", {})
            if concept_rot.get("rotation_pattern"):
                self.db.save_knowledge("concept_rotation", concept_rot["rotation_pattern"],
                    metadata={"date": patterns.get("date", ""), "main_concepts": concept_rot.get("main_concepts", [])})
        except Exception as e:
            logger.error(f"保存模式失败: {e}")