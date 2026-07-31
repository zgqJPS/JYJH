"""
stock_recommender.py - 个股推荐引擎
基于市场周期阶段 + 概念热度 + 龙头辨识度，推荐个股
"""

import sqlite3
import json
from datetime import datetime
from collections import defaultdict

DB_PATH = "/app/data/所有对话/主对话/stock_data_1784791326780_0_09ym.db"


class MarketData:
    """市场数据加载器"""
    
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
    
    def _ensure_conn(self):
        """确保连接可用"""
        try:
            self.conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row
    
    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
    
    def get_daily_stocks(self, date_str):
        """获取某日所有涨停股"""
        rows = self.conn.execute("""
            SELECT date, code, name, continuous_boards, seal_amount, seal_style,
                   turnover_rate, latest_price, change_percent, limit_up_time, market_cap
            FROM akshare_limit_up
            WHERE date = ?
            ORDER BY continuous_boards DESC
        """, (date_str,)).fetchall()
        return [dict(r) for r in rows]
    
    def get_smash_data(self, date_str):
        """获取某日砸盘数据"""
        row = self.conn.execute("""
            SELECT date, smash_coefficient, max_continuous_boards
            FROM smash_coefficient_results WHERE date = ?
        """, (date_str,)).fetchone()
        return dict(row) if row else None
    
    def get_concepts(self, date_str):
        """获取某日概念统计（按count降序）"""
        rows = self.conn.execute("""
            SELECT date, concept, count
            FROM concept_statistics
            WHERE date = ?
            ORDER BY count DESC
        """, (date_str,)).fetchall()
        return [dict(r) for r in rows]
    
    def get_trading_days(self):
        """获取所有交易日（有序）"""
        rows = self.conn.execute("""
            SELECT DISTINCT date FROM akshare_limit_up
            WHERE date >= '2026-01-21' AND date <= '2026-05-11'
            ORDER BY date
        """).fetchall()
        return [r[0] for r in rows]
    
    def get_stock_history(self, code, before_date=None, limit=10):
        """获取某只股票的历史涨停记录"""
        if before_date:
            rows = self.conn.execute("""
                SELECT date, continuous_boards, seal_amount, seal_style, turnover_rate
                FROM akshare_limit_up
                WHERE code = ? AND date <= ?
                ORDER BY date DESC LIMIT ?
            """, (code, before_date, limit)).fetchall()
        else:
            rows = self.conn.execute("""
                SELECT date, continuous_boards, seal_amount, seal_style, turnover_rate
                FROM akshare_limit_up
                WHERE code = ?
                ORDER BY date DESC LIMIT ?
            """, (code, limit)).fetchall()
        return [dict(r) for r in rows]
    
    def get_prev_dates(self, date_str, n=3):
        """获取某日之前的n个交易日"""
        rows = self.conn.execute("""
            SELECT DISTINCT date FROM akshare_limit_up
            WHERE date < ? AND date >= '2026-01-21'
            ORDER BY date DESC LIMIT ?
        """, (date_str, n)).fetchall()
        return sorted([r[0] for r in rows])
    
    def get_stock_concept_from_concepts(self, date_str):
        """
        尝试从 concept_statistics 反查某日概念覆盖的股票
        实际上我们需要从 akshare_limit_up 获取当天所有股票，
        然后用 concept_statistics 来判断市场热点
        """
        return self.get_concepts(date_str)


class CycleDetector:
    """市场情绪周期检测器"""
    
    PHASES = {
        '冰点酝酿': {
            'description': '市场低迷，酝酿反弹',
            'action': '保守策略',
            'stock_count': (3, 5),
        },
        '蓄力爬升': {
            'description': '市场逐步回暖，机会增多',
            'action': '积极策略',
            'stock_count': (5, 8),
        },
        '爆发高潮': {
            'description': '市场极度亢奋，风险加大',
            'action': '激进但警惕策略',
            'stock_count': (3, 5),
        },
        '崩塌退潮': {
            'description': '市场急速降温，规避风险',
            'action': '防守策略',
            'stock_count': (0, 2),
        },
    }
    
    def __init__(self, data: MarketData):
        self.data = data
    
    def detect_phase(self, date_str):
        """
        检测当前市场所处周期阶段
        返回: (phase_name, phase_detail, metrics)
        """
        smash = self.data.get_smash_data(date_str)
        if not smash:
            return 'unknown', {}, {}
        
        sc = smash['smash_coefficient']
        mb = smash['max_continuous_boards']
        
        stocks = self.data.get_daily_stocks(date_str)
        total_lu = len(stocks)
        
        # 获取前几日数据判断趋势
        prev_dates = self.data.get_prev_dates(date_str, 3)
        prev_sc_values = []
        prev_lu_values = []
        for pd in prev_dates:
            ps = self.data.get_smash_data(pd)
            if ps:
                prev_sc_values.append(ps['smash_coefficient'])
            pst = self.data.get_daily_stocks(pd)
            prev_lu_values.append(len(pst))
        
        metrics = {
            'smash_coefficient': sc,
            'max_boards': mb,
            'total_limit_up': total_lu,
            'prev_sc_values': prev_sc_values,
            'prev_lu_values': prev_lu_values,
        }
        
        # 趋势判断
        sc_trend = 0  # 砸盘趋势: -1下降, 0平稳, 1上升
        if prev_sc_values:
            sc_trend = sc - (sum(prev_sc_values) / len(prev_sc_values))
        
        lu_trend = 0
        if prev_lu_values:
            lu_trend = total_lu - (sum(prev_lu_values) / len(prev_lu_values))
        
        # 阶段判定
        # 崩塌退潮: mb骤降且sc骤降
        if prev_sc_values and prev_lu_values:
            if sc < prev_sc_values[-1] * 0.5 and total_lu < prev_lu_values[-1] * 0.7:
                return '崩塌退潮', {
                    'description': '市场急速降温，sc和涨停数骤降',
                    'sc_drop': round(prev_sc_values[-1] - sc, 2),
                    'lu_drop': prev_lu_values[-1] - total_lu,
                }, metrics
        
        # 爆发高潮: mb>=6, sc>4, lu>=70
        if mb >= 6 and sc > 4 and total_lu >= 70:
            return '爆发高潮', {
                'description': '市场极度亢奋，高标股多，涨停数多',
            }, metrics
        
        # 冰点酝酿: mb<=3, sc<2, lu在30~55
        if mb <= 3 and sc < 2 and 30 <= total_lu <= 55:
            return '冰点酝酿', {
                'description': '市场低迷，连板低、砸盘低、涨停数少',
            }, metrics
        
        # 蓄力爬升: mb=4~5, sc=2~4, lu=40~60
        if 4 <= mb <= 5 and 2 <= sc <= 4:
            return '蓄力爬升', {
                'description': '市场稳步爬升，连板逐步抬高',
            }, metrics
        
        # 补充判断 - 根据关键指标倾向性归类
        
        # sc很低 + mb低 = 冰点
        if sc < 2 and mb <= 3:
            return '冰点酝酿', {
                'description': '低砸盘+低连板，市场处于底部区域',
            }, metrics
        
        # sc很高 = 过热/崩塌
        if sc > 5 and sc_trend < -1:
            return '崩塌退潮', {
                'description': '高砸盘开始骤降，市场正在退潮',
                'sc_drop': round(-sc_trend, 2),
            }, metrics
        
        if sc > 4:
            return '爆发高潮', {
                'description': '砸盘系数较高，市场活跃度强',
            }, metrics
        
        # 默认归为蓄力爬升
        return '蓄力爬升', {
            'description': '市场处于中性偏积极状态',
        }, metrics


class StockScorer:
    """个股评分器"""
    
    def __init__(self, data: MarketData):
        self.data = data
    
    def score_stock(self, stock, date_str, cycle_phase, concepts, all_stocks):
        """
        对单只股票评分（满分100）
        
        stock: dict, 单只股票数据
        date_str: 日期
        cycle_phase: 当前周期阶段名
        concepts: 当日概念统计列表
        all_stocks: 当日所有涨停股列表
        """
        scores = {}
        
        boards = stock['continuous_boards'] or 1
        seal_amount = stock['seal_amount'] or 0
        seal_style = stock['seal_style'] or ''
        turnover = stock['turnover_rate'] or 0
        
        # ===== 1. 连板高度分 (20分) =====
        # 根据转移概率矩阵给分
        board_score_map = {
            1: 5,    # 首板，不确定性大
            2: 12,   # 2板，100%上升概率
            3: 16,   # 3板，82%上升
            4: 14,   # 4板，61%上升但接近5板生死线
            5: 10,   # 5板，生死线，67%下降
            6: 18,   # 6板，加速器，83%上升
            7: 8,    # 7板，60%下降风险
            8: 4,    # 8板，100%回落
        }
        
        # 周期适配：高潮期给高标加分，冰点期给低位加分
        if cycle_phase == '冰点酝酿':
            board_score_map = {1: 8, 2: 16, 3: 18, 4: 12, 5: 6, 6: 10, 7: 4, 8: 2}
        elif cycle_phase == '蓄力爬升':
            board_score_map = {1: 6, 2: 12, 3: 18, 4: 20, 5: 16, 6: 14, 7: 6, 8: 2}
        elif cycle_phase == '爆发高潮':
            board_score_map = {1: 4, 2: 8, 3: 12, 4: 14, 5: 16, 6: 20, 7: 12, 8: 4}
        elif cycle_phase == '崩塌退潮':
            board_score_map = {1: 10, 2: 16, 3: 14, 4: 8, 5: 4, 6: 6, 7: 2, 8: 1}
        
        scores['board'] = board_score_map.get(boards, 5)
        
        # ===== 2. 概念热度分 (25分) =====
        # 该股所属概念的数量和排名
        # 从concept_statistics反查：某概念count高且排名靠前，该股如果在此概念中则有加分
        concept_score = 0
        stock_concepts = []
        
        if concepts:
            # 取top10概念
            top_concepts = concepts[:10]
            max_count = top_concepts[0]['count'] if top_concepts else 1
            
            # 尝试通过name匹配concept（概念统计中count表示该概念下涨停股数量）
            # 由于没有直接的stock-concept映射，我们用概念count作为市场热度参考
            # 给所有高连板股按市场概念热度加分
            top_count = sum(c['count'] for c in top_concepts[:5])
            total_count = sum(c['count'] for c in concepts)
            
            # 基于板块聚集度: 该股连板越高，在概念越热时越有价值
            if boards >= 3:
                # 高连板股：top5概念热度占比越高越好
                ratio = top_count / total_count if total_count > 0 else 0
                concept_score = min(25, int(ratio * 40))
            else:
                concept_score = min(15, int((top_count / total_count * 25) if total_count > 0 else 0))
        
        # 为最高连板的股票加概念分（龙头辨识度）
        max_boards_today = max((s['continuous_boards'] or 0 for s in all_stocks), default=0)
        if boards == max_boards_today and max_boards_today >= 3:
            concept_score = min(25, concept_score + 8)
        
        scores['concept'] = concept_score
        
        # ===== 3. 封板质量分 (20分) =====
        seal_score = 0
        
        # 封板样式加分
        style_scores = {'一字板': 10, 'T字板': 6, '换手板': 3}
        seal_score += style_scores.get(seal_style, 2)
        
        # 封单额加分（越大越好，但要结合市值）
        if seal_amount > 5:
            seal_score += 6
        elif seal_amount > 2:
            seal_score += 4
        elif seal_amount > 0.5:
            seal_score += 2
        else:
            seal_score += 0
        
        # 换手率加分（太低可能是一字板无参与机会，适中最好）
        if 3 <= turnover <= 10:
            seal_score += 4
        elif 1 <= turnover < 3 or 10 < turnover <= 15:
            seal_score += 2
        else:
            seal_score += 1
        
        scores['seal'] = min(20, seal_score)
        
        # ===== 4. 辨识度分 (20分) =====
        identity_score = 0
        
        # 历史涨停天数（从stock_daily数据获取）
        stock_history = self.data.get_stock_history(stock['code'], date_str, 10)
        limit_up_days = len(stock_history)
        
        # 历史涨停天数越多辨识度越高
        if limit_up_days >= 6:
            identity_score += 10
        elif limit_up_days >= 4:
            identity_score += 7
        elif limit_up_days >= 2:
            identity_score += 4
        else:
            identity_score += 2
        
        # 最大连板
        max_hist_boards = max((h['continuous_boards'] or 0 for h in stock_history), default=0)
        if max_hist_boards >= 5:
            identity_score += 6
        elif max_hist_boards >= 3:
            identity_score += 4
        elif max_hist_boards >= 2:
            identity_score += 2
        
        # 是否为当日市场最高板
        if boards == max_boards_today and max_boards_today >= 3:
            identity_score += 4
        
        scores['identity'] = min(20, identity_score)
        
        # ===== 5. 周期适配分 (15分) =====
        cycle_score = 0
        
        if cycle_phase == '冰点酝酿':
            # 保守策略：低位连板（2~3板）优先
            if 2 <= boards <= 3:
                cycle_score = 15
            elif boards == 1:
                cycle_score = 10
            elif boards == 4:
                cycle_score = 5
            else:
                cycle_score = 2
        
        elif cycle_phase == '蓄力爬升':
            # 积极策略：正在晋级的高标股（3→4→5板）
            if 3 <= boards <= 5:
                cycle_score = 15
            elif boards == 2:
                cycle_score = 10
            elif boards == 6:
                cycle_score = 8
            else:
                cycle_score = 3
        
        elif cycle_phase == '爆发高潮':
            # 激进但警惕：最强龙头 + 补涨股
            if boards == max_boards_today and max_boards_today >= 5:
                cycle_score = 13
            elif boards <= 2:
                cycle_score = 10  # 补涨
            elif boards == 3:
                cycle_score = 8
            else:
                cycle_score = 3
        
        elif cycle_phase == '崩塌退潮':
            # 防守策略：不追高
            if boards <= 2:
                cycle_score = 10
            elif boards == 3:
                cycle_score = 5
            else:
                cycle_score = 1
        
        scores['cycle'] = cycle_score
        
        # 总分
        total = sum(scores.values())
        
        return {
            'total': total,
            'breakdown': scores,
        }


class StockRecommender:
    """个股推荐引擎"""
    
    def __init__(self, db_path=DB_PATH):
        self.data = MarketData(db_path)
        self.cycle_detector = CycleDetector(self.data)
        self.scorer = StockScorer(self.data)
    
    def recommend(self, date_str):
        """
        核心方法：输入日期，输出推荐列表
        
        返回格式:
        {
            "date": "2026-05-11",
            "cycle_phase": "蓄力爬升期",
            "recommendations": [...],
            "market_context": {...},
        }
        """
        # 1. 检测当前周期阶段
        phase_name, phase_detail, metrics = self.cycle_detector.detect_phase(date_str)
        
        # 2. 获取当日数据
        stocks = self.data.get_daily_stocks(date_str)
        concepts = self.data.get_concepts(date_str)
        smash = self.data.get_smash_data(date_str)
        
        if not stocks:
            return {
                'date': date_str,
                'cycle_phase': phase_name,
                'recommendations': [],
                'market_context': {'error': f'{date_str} 无涨停数据'},
            }
        
        # 3. 根据周期阶段筛选候选
        candidates = self._filter_candidates(stocks, phase_name)
        
        # 4. 对候选股评分
        scored = []
        for stock in candidates:
            score_result = self.scorer.score_stock(
                stock, date_str, phase_name, concepts, stocks
            )
            scored.append({
                'stock': stock,
                'score': score_result,
            })
        
        # 按总分排序
        scored.sort(key=lambda x: x['score']['total'], reverse=True)
        
        # 5. 根据周期阶段确定推荐数量
        phase_config = CycleDetector.PHASES.get(phase_name, {})
        min_count, max_count = phase_config.get('stock_count', (3, 5))
        
        # 6. 生成推荐列表
        recommendations = []
        for item in scored[:max_count]:
            stock = item['stock']
            score = item['score']
            
            # 确定风险等级和操作建议
            risk, action = self._determine_risk_action(
                stock, phase_name, score['total']
            )
            
            # 生成推荐理由
            reason = self._generate_reason(stock, phase_name, concepts, score)
            
            # 获取股票所属概念
            stock_concepts = self._infer_stock_concepts(stock, concepts)
            
            recommendations.append({
                'code': stock['code'],
                'name': stock['name'],
                'score': score['total'],
                'boards': stock['continuous_boards'] or 1,
                'concepts': stock_concepts,
                'seal_amount': stock['seal_amount'] or 0,
                'seal_style': stock['seal_style'] or '未知',
                'turnover_rate': round(stock['turnover_rate'] or 0, 2),
                'reason': reason,
                'risk_level': risk,
                'action': action,
                'score_breakdown': score['breakdown'],
            })
        
        # 7. 构建市场上下文
        market_context = {
            'date': date_str,
            'cycle_phase': phase_name,
            'phase_description': phase_detail.get('description', ''),
            'smash_coefficient': smash['smash_coefficient'] if smash else None,
            'max_boards': smash['max_continuous_boards'] if smash else None,
            'total_limit_up': len(stocks),
            'top_concepts': [c['concept'] + f"({c['count']})" for c in concepts[:5]],
            'phase_advice': phase_config.get('action', ''),
        }
        
        result = {
            'date': date_str,
            'cycle_phase': phase_name,
            'recommendations': recommendations,
            'market_context': market_context,
        }
        
        # 不关闭连接，由外部管理
        return result
    
    def _filter_candidates(self, stocks, phase_name):
        """根据周期阶段筛选候选股"""
        if phase_name == '冰点酝酿':
            # 保守：低位连板（1~3板），封单强的
            return [s for s in stocks if (s['continuous_boards'] or 0) <= 3]
        
        elif phase_name == '蓄力爬升':
            # 积极：2~5板，关注正在晋级的
            return [s for s in stocks if 2 <= (s['continuous_boards'] or 0) <= 5]
        
        elif phase_name == '爆发高潮':
            # 激进：最高板 + 低位补涨（1~2板）
            max_b = max((s['continuous_boards'] or 0 for s in stocks), default=0)
            return [s for s in stocks 
                    if (s['continuous_boards'] or 0) == max_b 
                    or (s['continuous_boards'] or 0) <= 2]
        
        elif phase_name == '崩塌退潮':
            # 防守：仅低位（1~3板）
            return [s for s in stocks if (s['continuous_boards'] or 0) <= 3]
        
        return stocks
    
    def _determine_risk_action(self, stock, phase_name, score):
        """确定风险等级和操作建议"""
        boards = stock['continuous_boards'] or 1
        
        if phase_name == '冰点酝酿':
            if boards <= 2 and score >= 60:
                return '低', '可追'
            elif boards <= 3:
                return '中', '观望'
            else:
                return '高', '回避'
        
        elif phase_name == '蓄力爬升':
            if 3 <= boards <= 5 and score >= 65:
                return '中', '可追'
            elif boards <= 4:
                return '中', '观望'
            else:
                return '高', '回避'
        
        elif phase_name == '爆发高潮':
            max_b = boards  # 已筛选过
            if score >= 70 and boards >= 5:
                return '高', '可追'  # 龙头但高风险
            elif boards <= 2:
                return '低', '可追'  # 补涨
            else:
                return '高', '回避'
        
        elif phase_name == '崩塌退潮':
            if boards <= 2 and score >= 55:
                return '中', '观望'  # 底部观察
            else:
                return '高', '回避'
        
        return '中', '观望'
    
    def _generate_reason(self, stock, phase_name, concepts, score):
        """生成推荐理由"""
        boards = stock['continuous_boards'] or 1
        seal_style = stock['seal_style'] or ''
        seal_amount = stock['seal_amount'] or 0
        
        parts = []
        
        # 连板描述
        if boards >= 5:
            parts.append(f"当前{boards}板高位龙头")
        elif boards >= 3:
            parts.append(f"{boards}连板，正处于晋级关键期")
        elif boards == 2:
            parts.append("2连板，有望继续晋级")
        else:
            parts.append("首板涨停")
        
        # 封板质量
        if seal_style == '一字板':
            parts.append("一字板封板极强")
        elif seal_style == 'T字板' and seal_amount > 2:
            parts.append(f"T字板封单{seal_amount}亿，封板较稳")
        
        # 概念热度
        if concepts:
            top3 = [c['concept'] for c in concepts[:3]]
            parts.append(f"市场热点集中在{'、'.join(top3)}")
        
        # 周期适配
        phase_advice = {
            '冰点酝酿': '冰点期低位股性价比高，适合潜伏',
            '蓄力爬升': '蓄力期高标股晋级概率大，适合积极参与',
            '爆发高潮': '高潮期龙头溢价高但风险也大，注意仓位',
            '崩塌退潮': '退潮期以防守为主，仅底部观察',
        }
        if phase_name in phase_advice:
            parts.append(phase_advice[phase_name])
        
        return '，'.join(parts) + '。'
    
    def _infer_stock_concepts(self, stock, concepts):
        """
        推断股票所属概念
        由于没有直接的stock-concept映射表在有效日期范围内，
        我们返回市场top概念作为参考
        """
        # 返回当日top3概念作为市场热点参考
        return [c['concept'] for c in concepts[:3]]


def format_recommendation(result):
    """格式化推荐结果"""
    lines = []
    ctx = result['market_context']
    
    lines.append("=" * 70)
    lines.append(f"📋 个股推荐报告 - {result['date']}")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f"📊 市场状态:")
    lines.append(f"  周期阶段: {ctx['cycle_phase']}")
    lines.append(f"  阶段描述: {ctx.get('phase_description', '')}")
    lines.append(f"  砸盘系数: {ctx['smash_coefficient']}")
    lines.append(f"  最高连板: {ctx['max_boards']}")
    lines.append(f"  涨停数量: {ctx['total_limit_up']}")
    lines.append(f"  热点概念: {', '.join(ctx.get('top_concepts', []))}")
    lines.append(f"  策略建议: {ctx.get('phase_advice', '')}")
    lines.append("")
    
    if not result['recommendations']:
        lines.append("⚠️ 当前无推荐标的")
    else:
        lines.append(f"🎯 推荐标的 ({len(result['recommendations'])}只):")
        lines.append("─" * 60)
        
        for i, rec in enumerate(result['recommendations'], 1):
            risk_icon = {'低': '🟢', '中': '🟡', '高': '🔴'}.get(rec['risk_level'], '⚪')
            action_icon = {'可追': '✅', '观望': '👀', '回避': '⛔'}.get(rec['action'], '')
            
            lines.append(f"  {i}. {rec['name']}({rec['code']}) - {rec['boards']}板")
            lines.append(f"     综合评分: {rec['score']}分 {risk_icon}{rec['risk_level']} {action_icon}{rec['action']}")
            lines.append(f"     封板: {rec['seal_style']} | 封单: {rec['seal_amount']}亿 | 换手: {rec['turnover_rate']}%")
            lines.append(f"     评分明细: 连板{rec['score_breakdown']['board']} + 概念{rec['score_breakdown']['concept']} "
                         f"+ 封板{rec['score_breakdown']['seal']} + 辨识度{rec['score_breakdown']['identity']} "
                         f"+ 周期{rec['score_breakdown']['cycle']}")
            lines.append(f"     理由: {rec['reason']}")
            lines.append("")
    
    lines.append("=" * 70)
    lines.append("⚠️ 免责声明：以上推荐基于历史数据分析，不构成投资建议。股市有风险，投资需谨慎。")
    lines.append("=" * 70)
    
    return '\n'.join(lines)


if __name__ == '__main__':
    recommender = StockRecommender()
    
    # 对最新日期生成推荐
    result = recommender.recommend('2026-05-11')
    print(format_recommendation(result))
    
    # 也可以对历史日期推荐
    print("\n\n")
    result2 = recommender.recommend('2026-04-29')
    print(format_recommendation(result2))
    
    # 关闭连接
    recommender.data.close()
