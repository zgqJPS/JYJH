#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
entry_certainty_analyzer.py — 进场确定性深度分析引擎

核心目标：在龙头识别基础上，对每只候选股进行多维度进场确定性推演，
解决"选到龙头但不知何时/何价/何条件进场"的问题。

六大维度（基于11975条带标签样本的实证胜率校准）：
1. 题材强度量化 — 板块涨停数、封单集中度、概念梯队完整性、题材新鲜度
2. 卡位分析 — 在板块内的排名、身位优势、是否日内龙/板块龙
3. 换手结构 — 换手率健康度、量价配合、筹码稳定性
4. 封板质量 — 封单比、封板时间、开板次数、回封能力、封单变化趋势
5. 竞价/盘口推演 — 竞价强度（首封时间代理）、开盘预期、量比信号
6. 次日确定性推演 — 基于历史标签的贝叶斯概率、多因子综合胜率、情景演绎

每个维度输出 0-100 分和详细诊断，最终合成进场确定性等级和具体操作指令。
"""

import sqlite3
import math
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict
from config import DB_PATH

# 导入真实连板计算器（兼容项目根目录直接运行与打包路径）
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
try:
    from board_calculator import BoardCalculator, VALID_DATA_START
    _HAS_BOARD_CALC = True
except ImportError:
    try:
        from core.board_calculator import BoardCalculator, VALID_DATA_START
        _HAS_BOARD_CALC = True
    except ImportError:
        _HAS_BOARD_CALC = False
        BoardCalculator = None
        VALID_DATA_START = None

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# 实证概率表（2026年7-8月真实数据重新校准，n=2937）
#
# ⚠️ 重要修正：此前使用ai_features_cache全量样本(含1-6月填充数据)校准，
# 发现1-6月数据为填充/重复数据（74只股票每个交易日都"涨停"），
# 导致整体次日涨停率虚高为82.2%。
# 真实整体次日涨停基准（7-8月有效数据）：20.4%
#
# 所有概率表均基于真实连续涨停天数（由board_calculator计算），
# 而非API返回的limit_up_days字段（该字段有14.6%不匹配）。
# ══════════════════════════════════════════════════════════════

# 真实连续涨停板数次日涨停概率（7-8月，board_calculator计算）
# 4板是确定性窗口(54.1%)，3板+0炸=55.6%，3板+封单>=3%=66.7%
# >=3板+封单>=5%+0炸 = 86.4%（最强组合）
# >=2板+封单>=5%+0炸+早封 = 84.8%
PROB_BY_BOARDS = {
    1: 0.171, 2: 0.317, 3: 0.414, 4: 0.541,
    5: 0.316, 6: 0.667, 7: 0.250, 8: 1.000
}

# 封单比区间概率（真实校准，封单越大确定性越高）
PROB_BY_SEAL_RATIO = [
    (0.005, 0.176), (0.02, 0.176), (0.05, 0.285),
    (0.10, 0.493), (0.20, 0.800), (999, 0.714)
]

# 炸板次数概率（0次炸板最优23.5%，1次以上均降至15-17%）
PROB_BY_BREAK = {
    0: 0.235, 1: 0.147, 2: 0.176, 3: 0.176, 4: 0.213, 5: 0.165
}

# 换手率区间概率（真实校准，3-15%最优）
PROB_BY_TURNOVER = [
    (0.03, 0.185), (0.07, 0.400), (0.15, 0.467),
    (0.30, 0.375), (0.50, 0.600)
]

# 量比区间概率（低量比<0.8反而更高，缩量涨停更确定）
PROB_BY_VOLUME_BIAS = [
    (0.8, 0.349), (1.5, 0.201), (3.0, 0.131),
    (5.0, 0.184), (999, 0.184)
]

# 板块封单占比(sector_ratio) — 实时计算时使用
PROB_BY_SECTOR_RATIO = [
    (0.02, 0.15), (0.05, 0.25), (0.10, 0.35),
    (0.30, 0.50), (999, 0.30)
]

# 市场最高板对应的整体次日涨停概率（真实校准）
PROB_BY_MARKET_MAX_BOARD = {
    1: 0.280, 2: 0.238, 3: 0.130, 4: 0.156,
    5: 0.289, 6: 0.175, 7: 0.168, 8: 0.175
}

# 封单变化（来自ai_features_cache，仅8/7前有效）
PROB_BY_SEAL_CHANGE = [
    (-0.01, 0.10), (0.01, 0.25), (0.03, 0.15), (999, 0.25)
]

# 首封时间概率（一字板42.8%，早盘23.2%，尾盘8.6%）
PROB_BY_SEAL_TIME = {
    'yizi': 0.428,      # 09:25-09:30 一字板
    'early': 0.232,     # 09:30-10:00 早盘
    'mid': 0.149,       # 10:00-11:30 中盘
    'afternoon': 0.107, # 13:00-14:00 午后
    'late': 0.086,      # 14:00-15:00 尾盘
}

# 交叉因子组合（最强确定性信号）
PROB_CROSS_FACTORS = {
    'boards3+seal5+break0': 0.864,      # >=3板+封单>=5%+0炸
    'boards2+seal5+break0+early': 0.848, # >=2板+封单>=5%+0炸+早封
    'boards4+break0': 0.714,             # 4板+0炸
    'boards3+break0+seal3': 0.667,       # 3板+0炸+封单>=3%
    'boards2+break0+early': 0.511,       # >=2板+0炸+早封
    'yizi+boards2': 0.515,               # 一字板+>=2板
}

# 真实基础概率（7-8月所有涨停股次日继续涨停率）
PRIOR_PROBABILITY = 0.204

# ── 贝叶斯概率校准表（保序回归，基于7/15-8/19回测n=750）──
# 朴素贝叶斯假设因子独立，但实际因子间存在相关性（如早封+0炸+封单强高度共线），
# 导致raw bayes系统性偏高。以下为raw→实际胜率的分段线性校准映射。
# 校准依据：各概率段实际次日涨停率（回测数据）
_BAYES_CALIBRATION_POINTS = [
    (0.00, 0.170),
    (0.10, 0.170),
    (0.20, 0.186),
    (0.30, 0.195),
    (0.40, 0.198),
    (0.50, 0.243),
    (0.55, 0.260),
    (0.60, 0.271),
    (0.65, 0.347),
    (0.70, 0.405),
    (0.75, 0.433),
    (0.80, 0.450),
    (0.85, 0.462),
    (0.90, 0.563),
    (0.95, 0.687),
    (1.00, 0.720),
]


def _calibrate_bayes_prob(raw_p: float) -> float:
    """将朴素贝叶斯原始概率校准为实际胜率估计（保序分段线性插值）"""
    if raw_p <= 0.0:
        return _BAYES_CALIBRATION_POINTS[0][1]
    if raw_p >= 1.0:
        return _BAYES_CALIBRATION_POINTS[-1][1]
    pts = _BAYES_CALIBRATION_POINTS
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= raw_p <= x1:
            t = (raw_p - x0) / (x1 - x0) if x1 > x0 else 0.0
            return y0 + t * (y1 - y0)
    return raw_p


def _lookup_prob(value, table, default=0.65):
    """从分桶概率表中查找概率"""
    if value is None:
        return default
    for threshold, prob in table:
        if value < threshold:
            return prob
    return table[-1][1] if table else default


def _parse_time_to_minutes(t_str) -> Optional[int]:
    """统一解析封板时间为分钟数。支持 09:25:00 / 092500 / 143827 等格式"""
    if not t_str or t_str == 'None' or str(t_str).strip() == '':
        return None
    s = str(t_str).strip()
    try:
        if ':' in s:
            parts = s.split(':')
            h, m = int(parts[0]), int(parts[1])
        elif len(s) >= 4:
            # 092500 或 143827 格式
            s = s.zfill(6)
            h, m = int(s[:2]), int(s[2:4])
        else:
            return None
        return h * 60 + m
    except (ValueError, IndexError):
        return None


def _safe_turnover(tr) -> Optional[float]:
    """清洗换手率：统一返回小数形式（0.05=5%）。
    数据源中8/6起部分记录以百分数存储（如2.2实际表示2.2%），需除以100。
    >100%视为异常返回None。
    """
    if tr is None:
        return None
    try:
        v = float(tr)
        if v > 1.0:       # 百分数形式（2.2 = 2.2%）
            v = v / 100.0
        if v > 1.0 or v < 0:  # >100%或负值，异常
            return None
        return v
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════
# 主分析类
# ══════════════════════════════════════════════════════════════

class EntryCertaintyAnalyzer:
    """进场确定性深度分析器"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._board_calc = None

    def _get_board_calc(self, conn=None):
        """获取（或创建）连板计算器；导入失败时返回 None，由调用方走 fallback"""
        if not _HAS_BOARD_CALC or BoardCalculator is None:
            return None
        if self._board_calc is None:
            try:
                self._board_calc = BoardCalculator()
            except Exception as e:
                logger.warning(f"BoardCalculator 初始化失败，使用降级模式: {e}")
                return None
        return self._board_calc

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def analyze_stock(self, date: str, code: str,
                      dragon_info: Optional[Dict] = None) -> Dict[str, Any]:
        """
        对单只股票进行完整的进场确定性分析。

        参数:
            date: 分析日期 YYYY-MM-DD
            code: 股票代码
            dragon_info: 龙头检测器输出的信息（可选，含certainty_level/total_score等）

        返回:
            完整的进场确定性分析报告
        """
        conn = self._get_conn()
        try:
            board_calc = self._get_board_calc(conn)
            stock = self._load_stock_data(conn, date, code, board_calc)
            if not stock:
                return {'error': f'未找到{date} {code}的数据'}

            market = self._load_market_context(conn, date, board_calc)
            sector = self._analyze_sector(conn, date, stock.get('concept', ''), board_calc)
            history = self._load_recent_history(conn, code, date, days=10, board_calc=board_calc)

            # 六大维度分析
            d1 = self._dimension_theme_strength(stock, sector, market)
            d2 = self._dimension_positioning(stock, sector, history)
            d3 = self._dimension_turnover_structure(stock, market, history)
            d4 = self._dimension_seal_quality(stock, history)
            d5 = self._dimension_auction_proxy(stock, market, history)
            d6 = self._dimension_next_day_certainty(
                stock, market, sector, dragon_info, history
            )

            dimensions = [d1, d2, d3, d4, d5, d6]
            composite = self._composite_score(dimensions, stock, dragon_info, market)

            # 生成具体操作指令
            operation = self._generate_operation(
                stock, composite, dragon_info, market, dimensions
            )

            return {
                'date': date,
                'code': code,
                'name': stock.get('name', ''),
                'concept': stock.get('concept', ''),
                'boards': stock.get('limit_up_days', 1),
                'price': stock.get('price', 0),
                'dimensions': {
                    'theme_strength': d1,
                    'positioning': d2,
                    'turnover_structure': d3,
                    'seal_quality': d4,
                    'auction_proxy': d5,
                    'next_day_certainty': d6,
                },
                'composite': composite,
                'operation': operation,
                'market_context': market,
            }
        finally:
            conn.close()

    # ─────────── 数据加载 ───────────

    def _load_stock_data(self, conn, date, code, board_calc=None) -> Optional[Dict]:
        """加载个股当日数据，合并xgt_limit_up_detail和ai_features_cache"""
        row = conn.execute("""
            SELECT * FROM xgt_limit_up_detail
            WHERE date=? AND code=?
        """, (date, code)).fetchone()
        if not row:
            return None
        stock = dict(row)

        # 用board_calculator覆盖真实连板数
        if board_calc:
            real_boards = board_calc.get_consecutive_boards(date, code, conn)
            stock['api_boards'] = stock.get('limit_up_days', 1)
            if real_boards > 0:
                stock['limit_up_days'] = real_boards
                stock['consecutive_boards'] = real_boards

        # 补充ai_features_cache（仅8/7前有数据）
        ai = conn.execute("""
            SELECT * FROM ai_features_cache WHERE date=? AND code=?
        """, (date, code)).fetchone()
        if ai:
            for k, v in dict(ai).items():
                if k not in stock or stock[k] is None:
                    stock[k] = v

        return stock

    def _load_market_context(self, conn, date, board_calc=None) -> Dict:
        """加载市场环境（使用board_calculator获取真实最高板和板分布）"""
        row = conn.execute("""
            SELECT * FROM xgt_daily_summary WHERE date=?
        """, (date,)).fetchone()
        if not row:
            return {}
        m = dict(row)

        # 用board_calculator覆盖真实数据
        if board_calc:
            health = board_calc.get_market_health(date, conn)
            m['max_continuous_boards'] = health['max_board']
            m['board_distribution'] = health['board_distribution']
            m['limit_up_count'] = health['limit_up_count']
            m['explosion_rate'] = health['explosion_rate']
        else:
            # fallback
            bd = m.get('board_distribution', '{}')
            try:
                import json
                m['board_distribution'] = json.loads(bd) if isinstance(bd, str) else bd
            except Exception:
                m['board_distribution'] = {}

        return m

    def _analyze_sector(self, conn, date, concept: str, board_calc=None) -> Dict:
        """分析题材/板块强度（使用真实连板数）"""
        if not concept:
            return {'name': '', 'stock_count': 0, 'total_seal': 0,
                    'avg_boards': 0, 'members': [], 'strength': 0,
                    'ladder': '', 'freshness': 'unknown'}

        rows = conn.execute("""
            SELECT code, name, limit_up_days, seal_ratio, turnover_rate,
                   first_limit_up_time, break_times, concept_rank, volume_bias
            FROM xgt_limit_up_detail
            WHERE date=? AND concept=?
            ORDER BY limit_up_days DESC, seal_ratio DESC
        """, (date, concept)).fetchall()

        members = [dict(r) for r in rows]
        # 用真实连板数覆盖
        if board_calc:
            for m in members:
                real_b = board_calc.get_consecutive_boards(date, m['code'])
                if real_b > 0:
                    m['limit_up_days'] = real_b
        count = len(members)
        total_seal = sum(m.get('seal_ratio') or 0 for m in members)
        avg_boards = sum(m.get('limit_up_days') or 1 for m in members) / max(count, 1)

        # 梯队完整性：检查1/2/3/4+板是否都有成员
        boards_set = set(m.get('limit_up_days', 1) for m in members)
        ladder_levels = sum(1 for b in [1, 2, 3, 4] if b in boards_set)
        ladder_desc = ['', '孤板', '两层', '三层', '完整梯队'][ladder_levels]

        # 题材新鲜度：查近5日同概念涨停数
        fresh_count = conn.execute("""
            SELECT COUNT(DISTINCT date) FROM xgt_limit_up_detail
            WHERE concept=? AND date < ? AND date >= date(?, '-5 days')
        """, (concept, date, date)).fetchone()[0]
        freshness = '新题材' if fresh_count <= 1 else (
            '持续发酵' if fresh_count <= 3 else '老题材')

        # 题材强度评分（0-100）
        strength = 0
        strength += min(count * 8, 40)          # 涨停家数（最多40分）
        strength += ladder_levels * 10           # 梯队（最多40分）
        strength += 20 if freshness == '新题材' else (10 if freshness == '持续发酵' else 5)
        strength = min(100, strength)

        return {
            'name': concept,
            'stock_count': count,
            'total_seal_ratio': round(total_seal, 4),
            'avg_boards': round(avg_boards, 1),
            'members': members[:10],  # 最多返回前10
            'member_count_total': count,
            'ladder_levels': ladder_levels,
            'ladder_desc': ladder_desc,
            'freshness': freshness,
            'strength_score': strength,
        }

    def _load_recent_history(self, conn, code, date, days=10, board_calc=None) -> List[Dict]:
        """加载个股近期涨停历史（使用真实连板数）"""
        rows = conn.execute("""
            SELECT date, limit_up_days, seal_ratio, turnover_rate,
                   first_limit_up_time, break_times, volume_bias, concept
            FROM xgt_limit_up_detail
            WHERE code=? AND date < ?
            ORDER BY date DESC LIMIT ?
        """, (code, date, days)).fetchall()
        result = [dict(r) for r in rows]
        if board_calc:
            for r in result:
                real_b = board_calc.get_consecutive_boards(r['date'], code)
                if real_b > 0:
                    r['limit_up_days'] = real_b
        return result

    # ─────────── 维度1：题材强度 ───────────

    def _dimension_theme_strength(self, stock, sector, market) -> Dict:
        """
        题材强度量化（0-100）
        - 板块涨停家数
        - 封单集中度
        - 梯队完整性
        - 题材新鲜度
        - 与市场主线的契合度
        """
        score = 0
        signals = []
        risks = []

        concept = stock.get('concept', '')
        if not concept:
            return {'score': 20, 'grade': 'D', 'signals': ['无明确概念标签'],
                    'risks': ['无法判断题材归属，盲炒风险大'], 'details': {}}

        # 板块涨停家数评分
        count = sector['stock_count']
        if count >= 8:
            score += 30
            signals.append(f"板块{count}家涨停（主线级）")
        elif count >= 5:
            score += 24
            signals.append(f"板块{count}家涨停（强势）")
        elif count >= 3:
            score += 18
            signals.append(f"板块{count}家涨停（活跃）")
        elif count >= 2:
            score += 10
            signals.append(f"板块{count}家涨停（零星）")
        else:
            score += 4
            risks.append("仅1家涨停，孤板难持续")

        # 梯队完整性
        ladder = sector['ladder_levels']
        score += ladder * 10
        if ladder >= 3:
            signals.append(f"{sector['ladder_desc']}（1-4板均有）")
        elif ladder >= 2:
            signals.append(f"{sector['ladder_desc']}（断层风险）")
        else:
            risks.append("梯队断层，无中间力量")

        # 题材新鲜度
        freshness = sector['freshness']
        if freshness == '新题材':
            score += 20
            signals.append("新题材（溢价空间大）")
        elif freshness == '持续发酵':
            score += 12
            signals.append("持续发酵中")
        else:
            score += 5
            risks.append("老题材（持续性存疑）")

        # 市场环境：最高板高度
        max_board = market.get('max_continuous_boards', 0) or 0
        if max_board >= 6:
            score += 10
            signals.append(f"市场高度{max_board}板（情绪高涨）")
        elif max_board >= 4:
            score += 15
            signals.append(f"市场高度{max_board}板（黄金期）")
        elif max_board <= 3:
            score += 5
            risks.append(f"市场仅{max_board}板（情绪低迷/退潮期）")

        # 跌停数（负向）
        limit_down = market.get('limit_down_count', 0) or 0
        if limit_down > 50:
            score -= 15
            risks.append(f"全市场{limit_down}家跌停（系统性风险）")
        elif limit_down > 20:
            score -= 8
            risks.append(f"全市场{limit_down}家跌停（情绪偏弱）")

        score = max(0, min(100, score))
        grade = self._score_to_grade(score)

        return {
            'score': score, 'grade': grade,
            'signals': signals, 'risks': risks,
            'details': {
                'concept': concept,
                'sector_count': count,
                'ladder': sector['ladder_desc'],
                'freshness': freshness,
                'market_max_board': max_board,
                'market_limit_down': limit_down,
            }
        }

    # ─────────── 维度2：卡位分析 ───────────

    def _dimension_positioning(self, stock, sector, history) -> Dict:
        """
        卡位分析（0-100）
        - 在板块内的身位排名
        - 是否为板块内最高板（身位龙）
        - 封板时间先后（日内龙）
        - 与同板竞争对手的比较
        """
        score = 50  # 基础分
        signals = []
        risks = []

        boards = stock.get('limit_up_days', 1) or 1
        concept = stock.get('concept', '')
        first_time = _parse_time_to_minutes(stock.get('first_limit_up_time'))
        seal_ratio = stock.get('seal_ratio') or 0

        if not concept or not sector.get('members'):
            return {'score': 40, 'grade': 'C',
                    'signals': ['无板块数据，无法判断卡位'],
                    'risks': [], 'details': {}}

        members = sector['members']
        # 板块内最高板
        sector_max_boards = max((m.get('limit_up_days') or 1) for m in members)
        is_height_dragon = boards >= sector_max_boards

        if is_height_dragon:
            score += 25
            signals.append(f"板块最高板{boards}板（身位龙）")
        else:
            gap = sector_max_boards - boards
            if gap == 1:
                score += 10
                signals.append(f"跟随龙（落后{gap}板）")
            else:
                score -= 5
                risks.append(f"落后龙头{gap}板，卡位劣势")

        # 封板时间排名（板块内）
        timed_members = [(m, _parse_time_to_minutes(m.get('first_limit_up_time')))
                         for m in members if _parse_time_to_minutes(m.get('first_limit_up_time'))]
        if first_time and timed_members:
            timed_members.sort(key=lambda x: x[1])
            rank = next((i+1 for i, (m, _) in enumerate(timed_members)
                        if m.get('code') == stock.get('code')), 0)
            total_timed = len(timed_members)
            if rank == 1:
                score += 20
                signals.append("板块内最先封板（日内龙）")
            elif rank <= max(1, total_timed // 3):
                score += 12
                signals.append(f"封板时间板块前{rank}/{total_timed}")
            elif rank >= total_timed * 0.7:
                score -= 5
                risks.append(f"封板时间偏后（{rank}/{total_timed}），跟风属性")

        # 封单比在板块内的排名
        if members:
            sorted_by_seal = sorted(members, key=lambda m: m.get('seal_ratio') or 0, reverse=True)
            seal_rank = next((i+1 for i, m in enumerate(sorted_by_seal)
                            if m.get('code') == stock.get('code')), 0)
            if seal_rank == 1 and seal_ratio > 0.03:
                score += 10
                signals.append("封单量板块第一")
            elif seal_rank > len(members) // 2 and seal_ratio < 0.01:
                score -= 5
                risks.append("封单量板块靠后")

        # 历史连续性（近10日涨停天数）
        recent_lu = len(history)
        if recent_lu >= 4:
            score += 8
            signals.append(f"近10日{recent_lu}次涨停（资金记忆强）")
        elif recent_lu >= 2:
            score += 4
        elif recent_lu == 0:
            score -= 3

        # is_dominant（如果ai_features_cache有）
        if stock.get('is_dominant') == 1:
            score += 15
            signals.append("AI标记为板块龙头")

        score = max(0, min(100, score))
        return {
            'score': score, 'grade': self._score_to_grade(score),
            'signals': signals, 'risks': risks,
            'details': {
                'boards': boards,
                'sector_max_boards': sector_max_boards,
                'is_height_dragon': is_height_dragon,
                'first_seal_time': stock.get('first_limit_up_time', ''),
                'sector_member_count': len(members),
            }
        }

    # ─────────── 维度3：换手结构 ───────────

    def _dimension_turnover_structure(self, stock, market, history) -> Dict:
        """
        换手结构分析（0-100）
        - 换手率健康度（实证7-30%最优）
        - 量价配合（量比）
        - 筹码稳定性（连续缩量/放量趋势）
        """
        score = 50
        signals = []
        risks = []

        turnover = _safe_turnover(stock.get('turnover_rate'))
        volume_bias = stock.get('volume_bias')
        boards = stock.get('limit_up_days', 1) or 1

        # 换手率评分
        if turnover is None:
            score -= 10
            risks.append("换手率数据异常（>50%或缺失），数据源可能有问题")
        else:
            if boards <= 2:
                # 首板/2板：适度换手有利于后续
                if 0.05 <= turnover <= 0.20:
                    score += 20
                    signals.append(f"换手{turnover:.1%}（健康，筹码充分交换）")
                elif turnover < 0.03:
                    score += 10
                    signals.append(f"换手{turnover:.1%}（缩量一字，筹码稳定）")
                elif turnover > 0.30:
                    score -= 15
                    risks.append(f"换手{turnover:.1%}过高（抛压沉重）")
                else:
                    score += 8
                    signals.append(f"换手{turnover:.1%}")
            else:
                # 高位板：换手要求更严格
                if 0.10 <= turnover <= 0.30:
                    score += 20
                    signals.append(f"换手{turnover:.1%}（高位充分换手，健康）")
                elif turnover < 0.05:
                    score += 5
                    risks.append(f"换手{turnover:.1%}（缩量加速，一旦开板抛压大）")
                elif turnover > 0.30:
                    score -= 20
                    risks.append(f"换手{turnover:.1%}（高位巨量换手，出货嫌疑）")
                else:
                    score += 5

        # 量比分析
        if volume_bias is not None:
            vb = float(volume_bias)
            if 1.5 <= vb <= 3.0:
                score += 15
                signals.append(f"量比{vb:.1f}（温和放量，量价配合好）")
            elif vb > 5.0:
                score += 5
                risks.append(f"量比{vb:.1f}（巨量，需警惕出货）")
            elif vb < 0.8:
                score += 8
                signals.append(f"量比{vb:.1f}（缩量涨停，惜售）")
            elif 0.8 <= vb < 1.5:
                score += 5
            else:
                score += 10
                signals.append(f"量比{vb:.1f}（放量上攻）")

        # 换手率趋势（与前日比较）
        if history and turnover is not None:
            prev_turnover = _safe_turnover(history[0].get('turnover_rate'))
            if prev_turnover and prev_turnover > 0:
                ratio = turnover / prev_turnover
                if ratio > 2.0 and boards >= 3:
                    score -= 10
                    risks.append(f"换手率较前日放大{ratio:.1f}倍（高位放量危险）")
                elif 0.8 <= ratio <= 1.5:
                    score += 5
                    signals.append("换手率稳定（筹码锁定好）")

        score = max(0, min(100, score))
        return {
            'score': score, 'grade': self._score_to_grade(score),
            'signals': signals, 'risks': risks,
            'details': {
                'turnover_rate': turnover,
                'volume_bias': volume_bias,
                'boards': boards,
            }
        }

    # ─────────── 维度4：封板质量 ───────────

    def _dimension_seal_quality(self, stock, history) -> Dict:
        """
        封板质量深度分析（0-100）
        - 封单比（绝对值和相对值）
        - 首封时间（越早越强）
        - 开板次数和回封能力
        - 封单变化趋势
        """
        score = 50
        signals = []
        risks = []

        seal_ratio = stock.get('seal_ratio') or 0
        break_times = stock.get('break_times') or 0
        first_time = _parse_time_to_minutes(stock.get('first_limit_up_time'))
        boards = stock.get('limit_up_days', 1) or 1

        # 封单比（注意：实证显示整体区分度弱，但极端值有意义）
        if seal_ratio >= 0.05:
            score += 15
            signals.append(f"封单比{seal_ratio:.1%}（强封单）")
        elif seal_ratio >= 0.02:
            score += 10
            signals.append(f"封单比{seal_ratio:.1%}（中等）")
        elif seal_ratio >= 0.005:
            score += 5
        else:
            score -= 5
            risks.append(f"封单比{seal_ratio:.1%}（偏弱）")

        # 首封时间
        if first_time:
            if first_time <= 575:  # 9:35前
                score += 25
                signals.append(f"{stock.get('first_limit_up_time')}封板（开盘快速封板，极强）")
            elif first_time <= 600:  # 10:00前
                score += 18
                signals.append(f"{stock.get('first_limit_up_time')}封板（早盘封板，强）")
            elif first_time <= 690:  # 11:30前
                score += 10
                signals.append(f"{stock.get('first_limit_up_time')}封板（上午封板）")
            elif first_time <= 780:  # 13:00前
                score += 3
                risks.append(f"{stock.get('first_limit_up_time')}午间封板（偏弱）")
            elif first_time <= 840:  # 14:00前
                score -= 5
                risks.append(f"{stock.get('first_limit_up_time')}下午封板（弱势）")
            else:
                score -= 15
                risks.append(f"{stock.get('first_limit_up_time')}尾盘封板（偷袭，次日风险大）")
        else:
            score -= 10
            risks.append("无封板时间数据")

        # 开板次数
        if break_times == 0:
            score += 15
            signals.append("一字封死（无开板）")
        elif break_times == 1:
            score += 5
            signals.append("开板1次（分歧后回封）")
        elif break_times <= 3:
            score -= 5
            risks.append(f"开板{break_times}次（分歧较大）")
        else:
            score -= 20
            risks.append(f"开板{break_times}次（封板极弱）")

        # 封单变化趋势（ai_features_cache）
        seal_change = stock.get('seal_ratio_change')
        if seal_change is not None:
            sc = float(seal_change)
            if sc < -0.01:
                score -= 15
                risks.append(f"封单较前日减弱{abs(sc):.1%}（资金撤离信号）")
            elif sc > 0.01:
                score += 5
                signals.append(f"封单较前日增强{sc:.1%}")

        # is_break_recover：炸板回封
        if stock.get('is_break_recover') == 1:
            score += 5
            signals.append("炸板后回封（有承接）")

        score = max(0, min(100, score))
        return {
            'score': score, 'grade': self._score_to_grade(score),
            'signals': signals, 'risks': risks,
            'details': {
                'seal_ratio': seal_ratio,
                'break_times': break_times,
                'first_seal_time': stock.get('first_limit_up_time', ''),
                'seal_ratio_change': seal_change,
            }
        }

    # ─────────── 维度5：竞价/盘口推演 ───────────

    def _dimension_auction_proxy(self, stock, market, history) -> Dict:
        """
        竞价/盘口推演（0-100）
        由于没有实时竞价数据，用首封时间、量比、封板状态作为竞价强度的代理变量。
        - 集合竞价封板（9:25）= 竞价超预期
        - 开盘秒板（9:30-9:35）= 盘口极强
        - 量比反映竞价参与度
        - 前日封板质量影响次日竞价预期
        """
        score = 50
        signals = []
        risks = []

        first_time = _parse_time_to_minutes(stock.get('first_limit_up_time'))
        volume_bias = stock.get('volume_bias')
        boards = stock.get('limit_up_days', 1) or 1

        # 竞价强度代理
        if first_time:
            if first_time <= 566:  # 9:26前（含集合竞价）
                score += 30
                signals.append("集合竞价封板（竞价超预期，资金抢筹）")
            elif first_time <= 575:  # 9:35前
                score += 20
                signals.append("开盘5分钟内封板（盘口极强）")
            elif first_time <= 600:
                score += 12
                signals.append("开盘30分钟内封板（盘口偏强）")
            else:
                score += 0  # 非竞价强势

        # 量比反映参与度
        if volume_bias:
            vb = float(volume_bias)
            if vb >= 3.0 and first_time and first_time <= 600:
                score += 10
                signals.append(f"竞价放量（量比{vb:.1f}）+早封=真抢筹")
            elif vb < 0.8 and first_time and first_time <= 575:
                score += 5
                signals.append(f"缩量秒板（量比{vb:.1f}）=惜售")
            elif vb > 5 and (not first_time or first_time > 690):
                score -= 10
                risks.append(f"尾盘放量（量比{vb:.1f}）=对倒/出货嫌疑")

        # 次日竞价预期推演
        # 基于前日封板质量推断次日竞价
        if history:
            prev = history[0]
            prev_break = prev.get('break_times', 0) or 0
            prev_seal = prev.get('seal_ratio') or 0
            prev_time = _parse_time_to_minutes(prev.get('first_limit_up_time'))

            if prev_break == 0 and prev_seal >= 0.03 and prev_time and prev_time <= 600:
                score += 10
                signals.append("前日强封板→次日竞价大概率高开")
            elif prev_break >= 3:
                score -= 10
                risks.append("前日多次炸板→次日竞价承压")

        # 市场环境对竞价的影响
        explosion = market.get('explosion_rate', 0) or 0
        if explosion > 0.35:
            score -= 8
            risks.append(f"市场炸板率{explosion:.0%}（恐慌情绪，竞价易低开）")
        elif explosion < 0.15:
            score += 5
            signals.append(f"市场炸板率{explosion:.0%}（情绪稳定）")

        score = max(0, min(100, score))
        return {
            'score': score, 'grade': self._score_to_grade(score),
            'signals': signals, 'risks': risks,
            'details': {
                'first_seal_minutes': first_time,
                'volume_bias': volume_bias,
                'market_explosion_rate': explosion,
            }
        }

    # ─────────── 维度6：次日确定性推演 ───────────

    def _dimension_next_day_certainty(self, stock, market, sector,
                                       dragon_info, history) -> Dict:
        """
        次日确定性推演（0-100）— 基于2026年7-8月真实数据重新校准
        
        贝叶斯融合多个独立因子，先验概率20.4%（真实涨停股次日继续涨停率）。
        核心发现：连板数+封单质量+炸板次数+首封时间是最强区分因子。
        """
        boards = stock.get('limit_up_days', 1) or 1
        seal_ratio = stock.get('seal_ratio') or 0
        break_times = min(stock.get('break_times') or 0, 5)
        turnover = _safe_turnover(stock.get('turnover_rate'))
        volume_bias = stock.get('volume_bias')
        first_time = _parse_time_to_minutes(stock.get('first_limit_up_time'))

        # 市场环境（board_calculator已提供真实数据）
        max_board = market.get('max_continuous_boards', 0) or 0
        limit_up_count = market.get('limit_up_count', 0) or 0
        explosion_rate = market.get('explosion_rate', 0) or 0
        limit_down_count = market.get('limit_down_count', 0) or 0

        # ── 动态市场因子（基于真实数据校准）──
        # 真实市场最高板概率: 1板28%, 2板24%, 3板13%, 4板16%, 5板29%, 6-8板17%
        # 但需要结合炸板率和跌停数动态调整
        market_factor_prob = PROB_BY_MARKET_MAX_BOARD.get(max_board, 0.20)

        if limit_down_count > 80:
            market_factor_prob = min(market_factor_prob, 0.05)  # 系统性崩盘
        elif explosion_rate > 0.40:
            market_factor_prob *= 0.6  # 高炸板率惩罚
        elif explosion_rate < 0.12 and limit_up_count >= 80:
            market_factor_prob = min(0.40, market_factor_prob * 1.3)  # 健康强势加成

        # ── 收集因子概率 ──
        factor_probs = []

        # 1. 连板数（最强单因子）
        p_boards = PROB_BY_BOARDS.get(boards, 0.15)
        factor_probs.append(('连板数', f'{boards}板', p_boards))

        # 2. 封单比
        p_seal = _lookup_prob(seal_ratio, PROB_BY_SEAL_RATIO, 0.18)
        factor_probs.append(('封单比', f'{seal_ratio:.1%}', p_seal))

        # 3. 炸板次数
        p_break = PROB_BY_BREAK.get(break_times, 0.17)
        factor_probs.append(('炸板次数', f'{break_times}次', p_break))

        # 4. 换手率（数据清洗后）
        if turnover is not None:
            p_to = _lookup_prob(turnover, PROB_BY_TURNOVER, 0.18)
            factor_probs.append(('换手率', f'{turnover:.1%}', p_to))

        # 5. 量比
        if volume_bias is not None:
            try:
                p_vb = _lookup_prob(float(volume_bias), PROB_BY_VOLUME_BIAS, 0.20)
                factor_probs.append(('量比', f'{float(volume_bias):.1f}', p_vb))
            except (TypeError, ValueError):
                pass

        # 6. 首封时间
        if first_time is not None:
            if first_time <= 570:
                time_bucket = 'yizi'
                time_label = '一字板'
            elif first_time <= 600:
                time_bucket = 'early'
                time_label = '早盘封'
            elif first_time <= 690:
                time_bucket = 'mid'
                time_label = '中盘封'
            elif first_time <= 840:
                time_bucket = 'afternoon'
                time_label = '午后封'
            else:
                time_bucket = 'late'
                time_label = '尾盘封'
            p_time = PROB_BY_SEAL_TIME.get(time_bucket, 0.15)
            factor_probs.append(('首封时间', time_label, p_time))

        # 7. 市场环境
        factor_probs.append(
            ('市场环境', f'{max_board}板/{limit_up_count}家', market_factor_prob))

        # ── 朴素贝叶斯融合（对数几率法）──
        prior = PRIOR_PROBABILITY  # 20.4%
        logit_sum = 0.0
        for name, val, p in factor_probs:
            p = max(0.02, min(0.95, p))
            logit_sum += math.log(p / (1 - p))
        logit_prior = math.log(prior / (1 - prior))
        logit_sum -= (len(factor_probs) - 1) * logit_prior
        bayes_prob = 1 / (1 + math.exp(-logit_sum))

        # ── 交叉因子加成（最强组合直接覆盖贝叶斯）──
        cross_bonus = 0.0
        cross_signals = []

        # >=3板 + 封单>=5% + 0炸板 = 86.4%
        if boards >= 3 and seal_ratio >= 0.05 and break_times == 0:
            cross_p = PROB_CROSS_FACTORS['boards3+seal5+break0']
            if cross_p > bayes_prob:
                cross_bonus = max(cross_bonus, (cross_p - bayes_prob) * 0.7)
                cross_signals.append(f"🔥高板+大封单+0炸板（历史{cross_p:.0%}）")

        # >=2板 + 封单>=5% + 0炸板 + 早封 = 84.8%
        if (boards >= 2 and seal_ratio >= 0.05 and break_times == 0
                and first_time is not None and first_time <= 600):
            cross_p = PROB_CROSS_FACTORS['boards2+seal5+break0+early']
            if cross_p > bayes_prob + cross_bonus:
                cross_bonus = max(cross_bonus, (cross_p - bayes_prob) * 0.7)
                cross_signals.append(f"🔥连板+大封单+0炸+早封（历史{cross_p:.0%}）")

        # 4板+0炸 = 71.4%
        if boards == 4 and break_times == 0:
            cross_p = PROB_CROSS_FACTORS['boards4+break0']
            if cross_p > bayes_prob + cross_bonus:
                cross_bonus = max(cross_bonus, (cross_p - bayes_prob) * 0.5)
                cross_signals.append(f"4板+0炸板（历史{cross_p:.0%}）")

        # 3板+0炸+封单>=3% = 66.7%
        if boards == 3 and break_times == 0 and seal_ratio >= 0.03:
            cross_p = PROB_CROSS_FACTORS['boards3+break0+seal3']
            if cross_p > bayes_prob + cross_bonus:
                cross_bonus = max(cross_bonus, (cross_p - bayes_prob) * 0.5)
                cross_signals.append(f"3板+0炸+封单≥3%（历史{cross_p:.0%}）")

        # 一字板+>=2板 = 51.5%
        if first_time is not None and first_time <= 570 and boards >= 2:
            cross_p = PROB_CROSS_FACTORS['yizi+boards2']
            if cross_p > bayes_prob + cross_bonus:
                cross_bonus = max(cross_bonus, (cross_p - bayes_prob) * 0.4)
                cross_signals.append(f"一字连板（历史{cross_p:.0%}）")

        bayes_prob += cross_bonus

        # ── 龙头等级加成（适度，不能覆盖基础概率）──
        dragon_bonus = 0
        if dragon_info:
            level = dragon_info.get('certainty_level', '')
            dragon_bonus_map = {'SS': 0.05, 'S': 0.03, 'A': 0.015, 'B': 0.0}
            dragon_bonus = dragon_bonus_map.get(level, 0)

        # ── 系统性风险强制压制 ──
        risk_penalty = 0.0
        if limit_down_count > 80:
            risk_penalty = 0.15  # 崩盘日，即使个股强也大幅降权
            bayes_prob = min(bayes_prob, 0.15)
        elif max_board <= 2 and limit_up_count < 50:
            risk_penalty = 0.05
            bayes_prob = min(bayes_prob, 0.25)

        final_prob = min(0.95, max(0.03, bayes_prob + dragon_bonus - risk_penalty))

        # 情景推演
        scenarios = self._scenario_deduction(
            stock, market, final_prob, boards, seal_ratio, break_times
        )

        # 确定性评分（概率直接映射为分数）
        score = int(final_prob * 100)
        cal_prob = _calibrate_bayes_prob(final_prob)
        signals = [f"次日涨停概率: 校准{cal_prob:.1%} / 模型{final_prob:.1%}（先验{prior:.1%}，{len(factor_probs)}因子）"]
        risks = []

        # 风险信号
        if limit_down_count > 80:
            risks.append(f"⚠️系统性崩盘（{limit_down_count}跌停），强制降权")
        if market_factor_prob < 0.15:
            risks.append(f"市场高度{max_board}板，情绪低迷")
        if boards >= 5:
            risks.append(f"{boards}板高位分歧（历史{p_boards:.0%}）")
        if break_times >= 3:
            risks.append(f"炸板{break_times}次，封板不坚决")
        if first_time is not None and first_time > 840:
            risks.append("尾盘封板，次日溢价低")
        if turnover is not None and turnover > 0.30:
            risks.append(f"换手率{turnover:.0%}偏高")
        if explosion_rate > 0.35:
            risks.append(f"市场炸板率{explosion_rate:.0%}，情绪恐慌")

        # 确认信号
        if boards >= 3 and seal_ratio >= 0.05 and break_times == 0:
            signals.append("✅高板+大封单+零炸板（最强组合）")
        elif boards >= 2 and break_times == 0 and first_time and first_time <= 600:
            signals.append("✅连板+零炸板+早封")
        for cs in cross_signals:
            signals.append(cs)
        if dragon_bonus > 0:
            level = dragon_info.get('certainty_level', '')
            signals.append(f"{level}级龙头加成+{dragon_bonus:.1%}")

        return {
            'score': score, 'grade': self._score_to_grade(score),
            'signals': signals, 'risks': risks,
            'details': {
                'bayes_probability': round(final_prob, 4),
                'prior': prior,
                'factor_count': len(factor_probs),
                'factor_table': [
                    {'factor': n, 'value': str(v), 'prob': round(p, 3)}
                    for n, v, p in factor_probs
                ],
                'cross_bonus': round(cross_bonus, 4),
                'dragon_bonus': dragon_bonus,
                'risk_penalty': risk_penalty,
                'scenarios': scenarios,
            }
        }

    def _scenario_deduction(self, stock, market, prob, boards,
                            seal_ratio, break_times) -> List[Dict]:
        """三种情景推演：最强/中性/最弱"""
        price = stock.get('price', 0) or 0
        limit_pct = 0.20 if (stock.get('code', '').startswith('300') or
                             stock.get('code', '').startswith('688')) else 0.10
        prev_close = price / (1 + limit_pct) if price > 0 else 0

        # 最强情景：一字/高开秒板
        strong = {
            'scenario': '最强',
            'condition': '竞价高开5%+，9:35前封板',
            'probability': round(prob * 0.3, 3),
            'target_price': round(price, 2),
            'action': '竞价排队/开盘打板',
        }

        # 中性情景：换手后封板
        mid = {
            'scenario': '中性',
            'condition': f'高开1-4%，换手后10:30前封板',
            'probability': round(prob * 0.4, 3),
            'target_price': round(price, 2),
            'action': '半路介入（涨幅3-6%区间）',
        }

        # 最弱情景：断板/低开
        weak_prob = max(0.05, 1 - prob - strong['probability'] - mid['probability'])
        weak = {
            'scenario': '最弱',
            'condition': '低开/平开，无法封板',
            'probability': round(weak_prob, 3),
            'target_price': round(prev_close * 0.97, 2),
            'action': '不参与/止损离场',
        }

        return [strong, mid, weak]

    # ─────────── 综合评分 ───────────

    def _composite_score(self, dimensions, stock, dragon_info, market) -> Dict:
        """
        综合评分（以贝叶斯次日涨停概率为核心锚点）。

        设计原则（胜率第一）：
        - next_day_certainty 维度的 bayes_probability 是经过真实数据校准的核心信号，
          直接决定基础分；其他5个维度仅作为 ±15 分的质量调节项。
        - 硬性门槛：bayes 概率 <20% 时最高只能到 C；<30% 最高只能到 B。
          这保证非贝叶斯维度无法把一只低概率股票"刷"成高等级。
        """
        dim_map = {
            'theme_strength': dimensions[0],
            'positioning': dimensions[1],
            'turnover_structure': dimensions[2],
            'seal_quality': dimensions[3],
            'auction_proxy': dimensions[4],
            'next_day_certainty': dimensions[5],
        }

        # 辅助维度权重（仅在贝叶斯基础分上做微调，合计影响 ±15 分）
        aux_weights = {
            'seal_quality':       0.30,
            'positioning':        0.20,
            'theme_strength':     0.20,
            'turnover_structure': 0.15,
            'auction_proxy':      0.15,
        }

        # 1) 贝叶斯基础分（0-100）—— 使用校准后的实际胜率估计
        nxt = dim_map['next_day_certainty']
        raw_bayes = nxt.get('details', {}).get('bayes_probability', 0.20)
        bayes_p = _calibrate_bayes_prob(raw_bayes)
        base_score = bayes_p * 100

        # 2) 辅助维度调节分（-15 ~ +15）
        # 各辅助维度以50分为中性，高于50加分，低于50减分
        adj = 0.0
        for key, w in aux_weights.items():
            s = dim_map[key].get('score', 50)
            adj += (s - 50) * w
        adj = max(-15, min(15, adj * 0.30))  # 缩放至 ±15

        # 3) 市场环境惩罚
        env = market.get('limit_down_count', 0) or 0
        penalty = 0
        if env > 100:
            penalty = 15
        elif env > 50:
            penalty = 10
        elif env > 20:
            penalty = 5

        final_score = max(0, min(100, base_score + adj - penalty))

        # 4) 确定性等级（以校准后贝叶斯概率和综合分双门槛）
        #    S+  : 校准胜率≥45% 且 综合≥50
        #    S   : 校准胜率≥30% 且 综合≥35
        #    A   : 校准胜率≥22% 且 综合≥25
        #    B   : 校准胜率≥18% 且 综合≥18
        #    C   : 校准胜率≥15%
        #    D   : 其他
        if bayes_p >= 0.45 and final_score >= 50:
            certainty = 'S+'
            desc = f'极高确定性（校准胜率{bayes_p:.0%}，多因子极强共振），核心仓位'
        elif bayes_p >= 0.30 and final_score >= 35:
            certainty = 'S'
            desc = f'高确定性（校准胜率{bayes_p:.0%}，封板质量优），标准仓位'
        elif bayes_p >= 0.22 and final_score >= 25:
            certainty = 'A'
            desc = f'较高确定性（校准胜率{bayes_p:.0%}），轻仓参与'
        elif bayes_p >= 0.18 and final_score >= 18:
            certainty = 'B'
            desc = f'中等确定性（校准胜率{bayes_p:.0%}，接近基准），极小仓位试错'
        elif bayes_p >= 0.15:
            certainty = 'C'
            desc = f'低确定性（校准胜率{bayes_p:.0%}），观望为主'
        else:
            certainty = 'D'
            desc = f'极低确定性（校准胜率{bayes_p:.0%}），不参与'

        return {
            'score': round(final_score, 1),
            'certainty_grade': certainty,
            'description': desc,
            'market_penalty': penalty,
            'bayes_probability': round(bayes_p, 4),
            'raw_bayes_probability': round(raw_bayes, 4),
            'aux_adjustment': round(adj, 1),
            'dimension_scores': {
                k: dim_map[k]['score'] for k in
                ['next_day_certainty', 'seal_quality', 'positioning',
                 'theme_strength', 'turnover_structure', 'auction_proxy']
            },
            'dimension_grades': {
                k: dim_map[k]['grade'] for k in
                ['next_day_certainty', 'seal_quality', 'positioning',
                 'theme_strength', 'turnover_structure', 'auction_proxy']
            },
            'weights': {
                'bayes_base': 1.0,
                'aux_adjustment': '±15',
            },
        }

    # ─────────── 操作指令生成 ───────────

    def _generate_operation(self, stock, composite, dragon_info,
                            market, dimensions) -> Dict:
        """根据综合评分生成具体操作指令"""
        score = composite['score']
        grade = composite['certainty_grade']
        boards = stock.get('limit_up_days', 1) or 1
        seal_ratio = stock.get('seal_ratio') or 0
        break_times = stock.get('break_times') or 0
        first_time = _parse_time_to_minutes(stock.get('first_limit_up_time'))
        price = stock.get('price', 0) or 0
        code = stock.get('code', '')

        limit_pct = 0.20 if (code.startswith('300') or code.startswith('688')) else 0.10
        prev_close = price / (1 + limit_pct) if price > 0 else 0

        # 基础决策
        if grade in ('S+', 'S'):
            if first_time and first_time <= 575 and break_times == 0 and seal_ratio >= 0.02:
                action = 'board_hit'
                action_name = '打板'
                timing = '次日9:25竞价挂单或9:30-9:35打板'
                price_desc = f'涨停价{price:.2f}'
                conditions = [
                    f'竞价高开3%-7%（{prev_close*1.03:.2f}~{prev_close*1.07:.2f}）挂单',
                    '一字板则排队不撤',
                    '开盘5分钟内未封板立即撤单',
                ]
            elif seal_ratio >= 0.01:
                action = 'half_way'
                action_name = '半路'
                timing = '次日9:45-10:30'
                price_desc = f'{prev_close*1.03:.2f}~{prev_close*1.06:.2f}（涨幅3-6%）'
                conditions = [
                    '开盘观察15分钟，确认放量上攻',
                    f'回落至{prev_close*1.03:.2f}-{prev_close*1.06:.2f}区间介入',
                    '跌破分时均线不买',
                ]
            else:
                action = 'low_buy'
                action_name = '低吸'
                timing = '次日10:00-14:30分时低点'
                price_desc = f'{prev_close*0.95:.2f}~{prev_close*0.98:.2f}'
                conditions = ['封板偏弱，等回调确认支撑', '5日均线附近考虑']
        elif grade == 'A':
            if first_time and first_time <= 600 and break_times <= 1 and seal_ratio >= 0.02:
                action = 'half_way'
                action_name = '半路'
                timing = '次日9:45-10:30'
                price_desc = f'{prev_close*1.02:.2f}~{prev_close*1.05:.2f}（涨幅2-5%）'
                conditions = ['仅半路不打板', '需放量确认', '跌破均价线止损']
            else:
                action = 'low_buy'
                action_name = '低吸'
                timing = '次日10:00-14:30'
                price_desc = f'{prev_close*0.94:.2f}~{prev_close*0.97:.2f}'
                conditions = ['轻仓低吸', '不追高']
        elif grade == 'B':
            action = 'low_buy'
            action_name = '轻仓低吸'
            timing = '尾盘14:00-14:45'
            price_desc = f'{prev_close*0.92:.2f}~{prev_close*0.96:.2f}'
            conditions = ['仅轻仓试错', '严格止损-3%']
        else:
            action = 'wait'
            action_name = '观望'
            timing = '无'
            price_desc = '无'
            conditions = ['确定性不足，不参与', '等待更明确的信号']

        # 仓位建议
        position_map = {'S+': 0.15, 'S': 0.10, 'A': 0.05, 'B': 0.02, 'C': 0, 'D': 0}
        position_pct = position_map.get(grade, 0)

        # 高板减仓（真实连板5板+胜率骤降）
        if boards >= 6:
            position_pct *= 0.4
        elif boards >= 4:
            position_pct *= 0.7

        # 市场极端恶劣强制空仓
        limit_down = market.get('limit_down_count', 0) or 0
        if limit_down > 100:
            action = 'wait'
            action_name = '空仓观望'
            position_pct = 0
            conditions.insert(0, f'⚠️ 全市场{limit_down}家跌停，系统性风险，强制空仓')

        # 止损止盈
        stop_loss = round(prev_close * 0.97, 2) if prev_close > 0 else 0
        take_profit_1 = round(price * 1.05, 2)
        take_profit_2 = round(price * 1.10, 2)

        return {
            'action': action,
            'action_name': action_name,
            'timing': timing,
            'price_range': price_desc,
            'conditions': conditions,
            'position_pct': round(position_pct, 4),
            'stop_loss': stop_loss,
            'take_profit_1': take_profit_1,
            'take_profit_2': take_profit_2,
            'prev_close': round(prev_close, 2),
            'limit_price': round(price, 2),
            'risk_reward_ratio': round(
                (take_profit_1 - prev_close * 1.03) / max(0.01, prev_close * 1.03 - stop_loss), 2
            ) if prev_close > 0 and action != 'wait' else 0,
        }

    # ─────────── 批量分析 ───────────

    def analyze_date(self, date: str, top_n: int = 20,
                     min_boards: int = 1) -> List[Dict]:
        """批量分析某日所有涨停股"""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT code, name, limit_up_days, seal_ratio, concept
                FROM xgt_limit_up_detail
                WHERE date=? AND limit_up_days >= ?
                ORDER BY limit_up_days DESC, seal_ratio DESC
                LIMIT ?
            """, (date, min_boards, top_n)).fetchall()

            results = []
            for r in rows:
                analysis = self.analyze_stock(date, r['code'])
                if 'error' not in analysis:
                    results.append(analysis)

            # 按综合分排序
            results.sort(key=lambda x: x['composite']['score'], reverse=True)
            return results
        finally:
            conn.close()

    def format_report(self, analysis: Dict) -> str:
        """格式化为文本报告"""
        if 'error' in analysis:
            return f"❌ {analysis['error']}"

        lines = []
        c = analysis['composite']
        op = analysis['operation']
        lines.append(f"{'='*60}")
        lines.append(f"🎯 {analysis['name']}({analysis['code']}) "
                     f"{analysis['boards']}板 | {analysis['concept']}")
        lines.append(f"   综合确定性: {c['score']}分 [{c['certainty_grade']}] {c['description']}")
        lines.append(f"{'='*60}")

        for key, dim in analysis['dimensions'].items():
            icon = {'theme_strength': '📚', 'positioning': '🎯',
                    'turnover_structure': '🔄', 'seal_quality': '🔒',
                    'auction_proxy': '⏰', 'next_day_certainty': '🎲'}.get(key, '📊')
            name_map = {
                'theme_strength': '题材强度', 'positioning': '卡位分析',
                'turnover_structure': '换手结构', 'seal_quality': '封板质量',
                'auction_proxy': '竞价推演', 'next_day_certainty': '次日确定性'
            }
            lines.append(f"\n{icon} {name_map.get(key,key)}: {dim['score']}分 [{dim['grade']}]")
            for s in dim.get('signals', []):
                lines.append(f"   ✅ {s}")
            for r in dim.get('risks', []):
                lines.append(f"   ⚠️ {r}")

        lines.append(f"\n{'─'*60}")
        lines.append(f"📋 操作指令: 【{op['action_name']}】")
        lines.append(f"   时机: {op['timing']}")
        lines.append(f"   价位: {op['price_range']}")
        lines.append(f"   仓位: {op['position_pct']:.0%}")
        lines.append(f"   止损: {op['stop_loss']} | 止盈1: {op['take_profit_1']} | 止盈2: {op['take_profit_2']}")
        for cond in op['conditions']:
            lines.append(f"   • {cond}")

        return '\n'.join(lines)

    @staticmethod
    def _score_to_grade(score):
        if score >= 80: return 'S'
        if score >= 65: return 'A'
        if score >= 50: return 'B'
        if score >= 35: return 'C'
        return 'D'


# ══════════════════════════════════════════════════════════════
# 建表与持久化
# ══════════════════════════════════════════════════════════════

def init_tables(db_path: str = DB_PATH):
    """创建进场确定性分析结果表"""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entry_certainty_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            concept TEXT,
            boards INTEGER,
            composite_score REAL,
            certainty_grade TEXT,
            theme_score INTEGER,
            positioning_score INTEGER,
            turnover_score INTEGER,
            seal_quality_score INTEGER,
            auction_score INTEGER,
            next_day_score INTEGER,
            bayes_probability REAL,
            raw_bayes_probability REAL,
            action TEXT,
            action_name TEXT,
            position_pct REAL,
            stop_loss REAL,
            take_profit_1 REAL,
            take_profit_2 REAL,
            conditions TEXT,
            signals TEXT,
            risks TEXT,
            created_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, code)
        );

        CREATE INDEX IF NOT EXISTS idx_eca_date ON entry_certainty_analysis(date);
        CREATE INDEX IF NOT EXISTS idx_eca_grade ON entry_certainty_analysis(certainty_grade);
        CREATE INDEX IF NOT EXISTS idx_eca_score ON entry_certainty_analysis(composite_score DESC);
    """)
    # 兼容旧表：若缺 raw_bayes_probability 列则补上
    cols = {r[1] for r in conn.execute("PRAGMA table_info(entry_certainty_analysis)").fetchall()}
    if 'raw_bayes_probability' not in cols:
        try:
            conn.execute("ALTER TABLE entry_certainty_analysis ADD COLUMN raw_bayes_probability REAL")
        except Exception:
            pass
    conn.commit()
    conn.close()
    logger.info("entry_certainty_analysis 表已就绪")


def save_analysis(date: str, analyses: List[Dict], db_path: str = DB_PATH):
    """批量保存分析结果"""
    import json
    conn = sqlite3.connect(db_path)
    for a in analyses:
        if 'error' in a:
            continue
        c = a['composite']
        op = a['operation']
        dims = a['dimensions']

        all_signals = []
        all_risks = []
        for d in dims.values():
            all_signals.extend(d.get('signals', []))
            all_risks.extend(d.get('risks', []))

        conn.execute("""
            INSERT OR REPLACE INTO entry_certainty_analysis
            (date, code, name, concept, boards, composite_score, certainty_grade,
             theme_score, positioning_score, turnover_score, seal_quality_score,
             auction_score, next_day_score, bayes_probability, raw_bayes_probability,
             action, action_name, position_pct, stop_loss, take_profit_1, take_profit_2,
             conditions, signals, risks)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            date, a['code'], a['name'], a.get('concept', ''), a['boards'],
            c['score'], c['certainty_grade'],
            dims['theme_strength']['score'], dims['positioning']['score'],
            dims['turnover_structure']['score'], dims['seal_quality']['score'],
            dims['auction_proxy']['score'], dims['next_day_certainty']['score'],
            c.get('bayes_probability', 0),
            c.get('raw_bayes_probability', 0),
            op['action'], op['action_name'], op['position_pct'],
            op['stop_loss'], op['take_profit_1'], op['take_profit_2'],
            json.dumps(op['conditions'], ensure_ascii=False),
            json.dumps(all_signals, ensure_ascii=False),
            json.dumps(all_risks, ensure_ascii=False),
        ))
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════
# 便捷入口
# ══════════════════════════════════════════════════════════════

def analyze_date(date: str = None, top_n: int = 20, db_path: str = DB_PATH):
    """分析指定日期全部涨停股的进场确定性"""
    if date is None:
        conn = sqlite3.connect(db_path)
        date = conn.execute(
            "SELECT MAX(date) FROM xgt_limit_up_detail"
        ).fetchone()[0]
        conn.close()

    analyzer = EntryCertaintyAnalyzer(db_path)
    results = analyzer.analyze_date(date, top_n=top_n)
    init_tables(db_path)
    save_analysis(date, results, db_path)
    return date, results


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO)
    d = sys.argv[1] if len(sys.argv) > 1 else None
    date, results = analyze_date(d)
    print(f"\n{'#'*60}")
    print(f"# {date} 进场确定性分析（共{len(results)}只）")
    print(f"{'#'*60}\n")
    for r in results[:10]:
        print(EntryCertaintyAnalyzer().format_report(r))
        print()
