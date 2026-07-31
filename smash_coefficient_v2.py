"""
smash_coefficient_v2.py - 新版砸盘系数模块
核心改动：
1. 重新定义砸盘系数为"分歧度/波动率指标"（而非"抛压指标"）
2. 新增5档分类：<1.5极低分歧, 1.5~3低分歧, 3~5中等分歧, 5~7高分歧, >7极高分歧
3. get_signal() 方法逻辑改为：
   - 不再简单说"抛压轻/重"
   - 而是说"分歧度低/中/高"+"结合连板判断含义"
   - 低分歧+低连板=冰点酝酿（即将启动）
   - 低分歧+高连板=高度一致（加速中）
   - 高分歧+高连板=过热（即将崩塌）
   - 高分歧+低连板=混乱（方向不明）
"""
import logging
import sqlite3
from collections import Counter

logger = logging.getLogger(__name__)


class SmashCoefficientCalculatorV2:
    """砸盘系数计算器（V2版-分歧度重定义）"""
    
    # 5档分歧度分类（来自67天深度分析）
    DIVERGENCE_LEVELS = {
        "极低分歧": (0, 1.5),
        "低分歧": (1.5, 3.0),
        "中等分歧": (3.0, 5.0),
        "高分歧": (5.0, 7.0),
        "极高分歧": (7.0, 999),
    }
    
    # 组合含义矩阵（分歧度 × 连板高度）
    COMBINATION_MEANING = {
        # (分歧度级别, 连板高度级别) → (含义, 建议)
        ("极低分歧", "低"): ("冰点酝酿", "市场极度平静，能量暗中积聚，等待启动信号"),
        ("极低分歧", "中"): ("温和推进", "市场缓慢推进，分歧极低，可关注主线龙头"),
        ("极低分歧", "高"): ("高度一致", "市场高度一致看多，加速阶段，注意过度一致性风险"),
        ("低分歧", "低"): ("冰点酝酿", "分歧不大但热度低，酝酿阶段，等待放量信号"),
        ("低分歧", "中"): ("温和推进", "分歧低+连板适中，温和上涨行情"),
        ("低分歧", "高"): ("高度一致", "分歧低+高连板，一致看多加速中"),
        ("中等分歧", "低"): ("震荡试探", "分歧中等但热度低，市场在试探方向"),
        ("中等分歧", "中"): ("正常波动", "分歧和连板都在正常范围，正常市场状态"),
        ("中等分歧", "高"): ("加速分化", "分歧上升+高连板，个股开始分化"),
        ("高分歧", "低"): ("混乱", "分歧高但连板低，方向不明，建议观望"),
        ("高分歧", "中"): ("退潮", "分歧偏高+连板中等，退潮阶段，谨慎操作"),
        ("高分歧", "高"): ("过热", "分歧高+高连板，过热即将崩塌，准备撤退！"),
        ("极高分歧", "低"): ("混乱", "分歧极高但无连板，市场混乱，严格观望"),
        ("极高分歧", "中"): ("退潮", "分歧极高，退潮确认，空仓等待"),
        ("极高分歧", "高"): ("过热崩塌", "极高分歧+高连板，崩塌前兆，立即撤退！"),
    }
    
    def __init__(self, db, db_path=None):
        """
        初始化
        db: 数据库连接对象（兼容原有接口）
        db_path: sqlite数据库路径
        """
        self.db = db
        self.db_path = db_path or getattr(db, 'db_path', None)
    
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def calculate(self, start_date, end_date):
        """
        计算指定日期范围内每个交易日的砸盘系数
        返回: {date: {smash_coefficient, max_continuous_boards}} 字典
        """
        try:
            all_dates = self.db.get_all_dates()
            target_dates = [d for d in all_dates if start_date <= d <= end_date]
            
            if not target_dates:
                logger.warning(f"日期范围 {start_date}~{end_date} 内无交易日数据")
                return {}
            
            results = {}
            for date in target_dates:
                date_idx = all_dates.index(date)
                if date_idx <= 0:
                    continue
                
                prev_date = all_dates[date_idx - 1]
                coef, max_boards = self._calc_single_date(date, prev_date)
                
                if coef is not None:
                    results[date] = {
                        "smash_coefficient": coef,
                        "max_continuous_boards": max_boards,
                    }
                    self.db.save_smash_coefficient(date, coef, max_boards)
            
            logger.info(f"砸盘系数V2计算完成: {len(results)} 个交易日")
            return results
        
        except Exception as e:
            logger.error(f"砸盘系数V2批量计算异常: {e}", exc_info=True)
            return {}
    
    def calculate_daily(self, date):
        """计算单日砸盘系数"""
        try:
            all_dates = self.db.get_all_dates()
            if date not in all_dates:
                logger.warning(f"{date} 不在交易日列表中")
                return None, None
            
            date_idx = all_dates.index(date)
            if date_idx <= 0:
                logger.warning(f"{date} 是第一个交易日，无前日数据可对比")
                return None, None
            
            prev_date = all_dates[date_idx - 1]
            coef, max_boards = self._calc_single_date(date, prev_date)
            
            if coef is not None:
                self.db.save_smash_coefficient(date, coef, max_boards)
                logger.info(f"{date} 砸盘系数V2: {coef}，最高连板: {max_boards}")
            
            return coef, max_boards
        
        except Exception as e:
            logger.error(f"单日砸盘系数V2计算异常: {e}", exc_info=True)
            return None, None
    
    def _calc_single_date(self, date, prev_date):
        """
        计算单日砸盘系数（核心算法，与原版本一致）
        晋升比率均值 × 10
        """
        try:
            today_stocks = self.db.get_limit_up_data(date)
            if not today_stocks:
                return None, None
            
            prev_stocks = self.db.get_limit_up_data(prev_date)
            if not prev_stocks:
                return None, None
            
            today_boards = [int(dict(s).get("continuous_boards", 1) or 1) for s in today_stocks]
            today_dist = Counter(today_boards)
            
            prev_boards = [int(dict(s).get("continuous_boards", 1) or 1) for s in prev_stocks]
            prev_dist = Counter(prev_boards)
            
            max_boards = max(today_boards) if today_boards else 0
            
            ratios = []
            for n in range(2, 11):
                today_n = today_dist.get(n, 0)
                prev_n_minus_1 = prev_dist.get(n - 1, 0)
                if prev_n_minus_1 > 0:
                    ratio = today_n / prev_n_minus_1
                    ratios.append(ratio)
            
            if not ratios:
                first_board = today_dist.get(1, 1)
                high_board = sum(today_dist.get(n, 0) for n in range(2, 11))
                if first_board > 0:
                    simplified_ratio = high_board / first_board
                    smash_coef = round(simplified_ratio * 10, 2)
                else:
                    smash_coef = 5.0
            else:
                mean_ratio = sum(ratios) / len(ratios)
                smash_coef = round(mean_ratio * 10, 2)
            
            smash_coef = max(0.0, min(20.0, smash_coef))
            return smash_coef, max_boards
        
        except Exception as e:
            logger.error(f"单日砸盘系数V2计算失败({date}): {e}")
            return None, None
    
    def get_divergence_level(self, smash_value):
        """
        获取分歧度级别
        返回: "极低分歧" / "低分歧" / "中等分歧" / "高分歧" / "极高分歧"
        """
        if smash_value < 1.5:
            return "极低分歧"
        elif smash_value < 3.0:
            return "低分歧"
        elif smash_value < 5.0:
            return "中等分歧"
        elif smash_value < 7.0:
            return "高分歧"
        else:
            return "极高分歧"
    
    def _get_board_level(self, max_boards):
        """获取连板高度级别"""
        if max_boards <= 3:
            return "低"
        elif max_boards <= 5:
            return "中"
        else:
            return "高"
    
    def get_trend(self, date, days=5):
        """
        获取砸盘系数趋势
        返回: {values, trend, change, analysis}
        """
        try:
            all_dates = self.db.get_all_dates()
            if date not in all_dates:
                return {"values": [], "trend": "未知", "change": 0, "analysis": "日期无效"}
            
            date_idx = all_dates.index(date)
            start_idx = max(0, date_idx - days + 1)
            recent_dates = all_dates[start_idx:date_idx + 1]
            
            values = []
            for d in recent_dates:
                row = self.db.get_smash_coefficient(d)
                if row:
                    values.append({"date": d, "value": dict(row).get("smash_coefficient", 0)})
                else:
                    coef, _ = self.calculate_daily(d)
                    if coef is not None:
                        values.append({"date": d, "value": coef})
            
            if len(values) < 2:
                return {
                    "values": values,
                    "trend": "数据不足",
                    "change": 0,
                    "analysis": "分歧度历史数据不足，无法判断趋势"
                }
            
            first_val = values[0]["value"]
            last_val = values[-1]["value"]
            change = last_val - first_val
            
            val_list = [v["value"] for v in values]
            avg_val = sum(val_list) / len(val_list) if val_list else 0
            max_deviation = max(abs(v - avg_val) for v in val_list) if val_list else 0
            
            if change > 1.5:
                trend = "上升"
                analysis = f"分歧度从{first_val:.1f}升至{last_val:.1f}，分歧加剧"
            elif change < -1.5:
                trend = "下降"
                analysis = f"分歧度从{first_val:.1f}降至{last_val:.1f}，分歧收敛"
            elif max_deviation > 2.0:
                trend = "震荡"
                analysis = f"分歧度在{min(val_list):.1f}~{max(val_list):.1f}区间震荡"
            else:
                trend = "平稳"
                analysis = f"分歧度稳定在{avg_val:.1f}附近"
            
            return {
                "values": values,
                "trend": trend,
                "change": round(change, 2),
                "analysis": analysis,
            }
        
        except Exception as e:
            logger.error(f"分歧度趋势分析异常: {e}")
            return {"values": [], "trend": "未知", "change": 0, "analysis": f"分析异常: {e}"}
    
    def get_signal(self, date):
        """
        基于砸盘系数给出市场信号（V2版-分歧度重定义）
        
        不再简单说"抛压轻/重"，而是：
        1. 判断分歧度级别（5档）
        2. 结合连板高度判断含义
        3. 给出组合建议
        
        返回: {
            "signal": 分歧度组合含义,
            "value": 砸盘系数值,
            "divergence_level": 分歧度级别,
            "combination": 组合描述,
            "advantage": 优势描述,
            "disadvantage": 劣势描述,
            "trade_advice": 交易建议
        }
        """
        try:
            row = self.db.get_smash_coefficient(date)
            if not row:
                coef, max_boards = self.calculate_daily(date)
                if coef is None:
                    return {
                        "signal": "未知",
                        "value": None,
                        "divergence_level": "未知",
                        "combination": "",
                        "advantage": "分歧度数据缺失",
                        "disadvantage": "",
                        "trade_advice": "数据不足，建议观望"
                    }
            else:
                row = dict(row)
                coef = row.get("smash_coefficient", 5.0)
                max_boards = row.get("max_continuous_boards", 3)
            
            # 分歧度级别
            divergence = self.get_divergence_level(coef)
            
            # 连板高度级别
            board_level = self._get_board_level(max_boards)
            
            # 组合含义查找
            combination_key = (divergence, board_level)
            meaning, meaning_detail = self.COMBINATION_MEANING.get(
                combination_key, ("未知", "市场状态复杂，建议观望"))
            
            # 生成信号
            signal = meaning
            advantage = ""
            disadvantage = ""
            
            # 根据组合生成优势和劣势
            if meaning == "冰点酝酿":
                advantage = f"分歧度{coef:.1f}({divergence})，连板{max_boards}，能量正在积聚"
                disadvantage = "尚未出现明确启动信号，需等待"
                trade_advice = "极轻仓试错低位龙头，严格止损，等待放量启动信号"
            elif meaning == "高度一致":
                advantage = f"分歧度{coef:.1f}({divergence})，连板{max_boards}，市场高度看多"
                disadvantage = "过度一致性往往意味着反转风险"
                trade_advice = "持有核心龙头，但注意设好止盈位，准备随时撤退"
            elif meaning == "过热" or meaning == "过热崩塌":
                advantage = ""
                disadvantage = f"分歧度{coef:.1f}({divergence})，连板{max_boards}，市场过热"
                trade_advice = "立即减仓！高连板+高分歧=崩塌前兆，不要追高"
            elif meaning == "混乱":
                advantage = ""
                disadvantage = f"分歧度{coef:.1f}({divergence})，连板{max_boards}，方向不明"
                trade_advice = "建议观望，不参与或极轻仓试探"
            elif meaning == "温和推进":
                advantage = f"分歧度{coef:.1f}({divergence})，市场节奏良好"
                disadvantage = ""
                trade_advice = "可适当参与主线方向，关注龙头持续性"
            elif meaning == "正常波动":
                advantage = f"分歧度{coef:.1f}处于正常范围，连板{max_boards}"
                disadvantage = ""
                trade_advice = "按常规策略操作，聚焦核心龙头"
            elif meaning == "加速分化":
                advantage = f"连板{max_boards}仍在拓展"
                disadvantage = f"分歧度{coef:.1f}偏高，个股开始分化"
                trade_advice = "只持有最强龙头，弱势股及时兑现"
            elif meaning == "退潮":
                advantage = ""
                disadvantage = f"分歧度{coef:.1f}偏高，退潮确认"
                trade_advice = "减仓或空仓，等待下一轮周期启动"
            elif meaning == "震荡试探":
                advantage = "分歧度适中"
                disadvantage = "热度不足，方向未定"
                trade_advice = "观望为主，可关注首板机会"
            else:
                advantage = ""
                disadvantage = ""
                trade_advice = "按常规策略操作"
            
            return {
                "signal": signal,
                "value": coef,
                "divergence_level": divergence,
                "max_boards": max_boards,
                "combination": f"{divergence}+{board_level}连板={meaning}",
                "advantage": advantage,
                "disadvantage": disadvantage,
                "trade_advice": trade_advice,
            }
        
        except Exception as e:
            logger.error(f"分歧度信号V2判断异常: {e}")
            return {
                "signal": "未知",
                "value": None,
                "divergence_level": "未知",
                "combination": "",
                "advantage": "",
                "disadvantage": "",
                "trade_advice": "数据异常，建议观望"
            }
    
    def get_market_score_impact(self, date):
        """
        获取分歧度对个股推荐评分的影响
        返回: score_adjustment（分值调整，范围[-5, +3]）
        """
        try:
            row = self.db.get_smash_coefficient(date)
            if not row:
                return 0
            
            coef = dict(row).get("smash_coefficient", 5.0)
            
            # 使用5档分类
            if coef >= 7.0:
                return -5  # 极高分歧，大幅扣分
            elif coef >= 5.0:
                return -3  # 高分歧，扣分
            elif coef >= 3.0:
                return -1  # 中等分歧，轻微扣分
            elif coef >= 1.5:
                return 1   # 低分歧，轻微加分
            else:
                return 3   # 极低分歧，加分
        
        except Exception as e:
            logger.error(f"分歧度评分影响计算异常: {e}")
            return 0
