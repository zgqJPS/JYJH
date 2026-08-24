"""
dragon_detector.py - 确定性龙头识别引擎
========================================
基于6维度评分模型，从涨停池中识别真正的"确定性龙头"，
区分龙头类型（总龙头/板块龙/补涨龙/切换龙）和生命周期阶段，
为操作计划提供高置信度标的。

设计原则：
  - 胜率第一：宁缺毋滥，只输出真正具备龙头特质的标的
  - 稳定性：每个维度都有数据校验和降级处理
  - 可解释：每个评分都附带具体理由

6大维度及权重：
  1. 身位优势   25%  — 连板高度、梯队领先距离
  2. 封板坚决度 25%  — 封单比、开板次数、封板时间
  3. 板块带动性 20%  — 同概念涨停数、跟风股数量、板块涨幅
  4. 市场辨识度 15%  — 最高板唯一性、媒体/资金关注度、历史股性
  5. 概念纯正度 10%  — 概念匹配度、是否多概念叠加、主业关联
  6. 逆势强度   5%   — 炸板潮中抗跌、砸盘系数高时仍封板

用法:
  from dragon_detector import DragonDetector
  detector = DragonDetector(db_path)
  dragons = detector.detect_dragons('2026-08-12')
  report = detector.format_dragon_report(dragons)
"""

import sqlite3
import json
import logging
import os
from collections import defaultdict, Counter
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime

from config import DB_PATH

try:
    from board_calculator import BoardCalculator
    _HAS_BOARD_CALC = True
except ImportError:
    _HAS_BOARD_CALC = False

logger = logging.getLogger('dragon_detector')

# ─────────────────────────── 维度权重 ───────────────────────────
DRAGON_DIMENSION_WEIGHTS = {
    'board_position':    0.20,   # 身位优势
    'seal_resolution':   0.25,   # 封板坚决度（最核心）
    'sector_leadership': 0.20,   # 板块带动性
    'market_recognition': 0.12,  # 市场辨识度
    'concept_purity':    0.08,   # 概念纯正度
    'counter_trend':     0.10,   # 逆势强度（恶劣环境封板含金量高）
    'continuity':        0.05,   # 龙头连续性（历史龙头地位延续）
}

# ─────────────────────────── 龙头类型定义 ───────────────────────────
DRAGON_TYPES = {
    'total_dragon': {
        'name': '总龙头',
        'desc': '全市场最高板，具备绝对身位优势和市场号召力',
        'color': '🔴',
    },
    'sector_dragon': {
        'name': '板块龙',
        'desc': '所属概念板块内最高板，带动板块集体上涨',
        'color': '🟠',
    },
    'catch_up_dragon': {
        'name': '补涨龙',
        'desc': '主线题材中后起之秀，身位低于龙头但走势强劲',
        'color': '🟡',
    },
    'switch_dragon': {
        'name': '切换龙',
        'desc': '新题材破冰者，在老周期退潮时率先启动',
        'color': '🟢',
    },
}

# ─────────────────────────── 生命周期阶段 ───────────────────────────
LIFECYCLE_STAGES = {
    'launch':    {'name': '启动期', 'desc': '首板或2板初启动，辨识度正在建立', 'icon': '🚀'},
    'acceleration': {'name': '加速期', 'desc': '3-4板加速确认，龙头地位确立', 'icon': '⚡'},
    'climax':    {'name': '高潮期', 'desc': '5板+市场共识，缩量加速或爆量分歧', 'icon': '🔥'},
    'decline':   {'name': '衰退期', 'desc': '高位放量滞涨或断板，龙头地位动摇', 'icon': '📉'},
}

# ─────────────────────────── 龙头确定性等级 ───────────────────────────
CERTAINTY_LEVELS = {
    'SS': {'name': 'SS级-绝对龙头', 'min_score': 90, 'desc': '全市场唯一最高板+强封单+板块效应，胜率极高'},
    'S':  {'name': 'S级-确定性龙头', 'min_score': 80, 'desc': '板块绝对龙头，封板质量优秀，胜率高'},
    'A':  {'name': 'A级-强势龙头', 'min_score': 68, 'desc': '板块领涨股，具备龙头特质，胜率较高'},
    'B':  {'name': 'B级-潜在龙头', 'min_score': 55, 'desc': '有龙头潜质但需确认，谨慎参与'},
}


class DragonDetector:
    """确定性龙头识别引擎"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._board_calc = None
        if _HAS_BOARD_CALC:
            try:
                conn = sqlite3.connect(db_path)
                self._board_calc = BoardCalculator(conn)
            except Exception as e:
                logger.warning(f"BoardCalculator初始化失败: {e}")

    def _apply_real_boards(self, stocks: List[Dict], date: str) -> List[Dict]:
        """用BoardCalculator真实连板数覆盖API的limit_up_days字段"""
        if not self._board_calc or not stocks:
            return stocks
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            for s in stocks:
                real = self._board_calc.get_consecutive_boards(date, s['code'], conn)
                if real > 0:
                    s['api_limit_up_days'] = s.get('limit_up_days', 1)
                    s['limit_up_days'] = real
        except Exception as e:
            logger.warning(f"真实连板数覆盖失败({date}): {e}")
        finally:
            if conn:
                conn.close()
        return stocks

    # ─────────────────────────── 数据库工具 ───────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_limit_up_stocks(self, date: str) -> List[Dict]:
        """获取当日涨停股完整数据"""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT code, name, price, change_percent, limit_up_days,
                       first_limit_up_time, last_limit_up_time, break_times,
                       seal_ratio, turnover_rate, volume_bias, flow_capital,
                       total_capital, concept, reason
                FROM xgt_limit_up_detail
                WHERE date = ?
                ORDER BY limit_up_days DESC, seal_ratio DESC
            """, (date,)).fetchall()
            result = []
            for r in rows:
                stock = dict(r)
                # 换手率归一化：>1视为百分数（如5=5%），>100或负数视为异常
                tr = stock.get('turnover_rate')
                if tr is not None:
                    try:
                        tr = float(tr)
                        if tr > 100 or tr < 0:
                            stock['turnover_rate'] = 0.15
                        elif tr > 1.0:
                            stock['turnover_rate'] = tr / 100.0
                    except (TypeError, ValueError):
                        stock['turnover_rate'] = 0.15
                # 首封时间归一化："092500" → "09:25:00"
                ft = stock.get('first_limit_up_time')
                if ft and ':' not in str(ft):
                    ft_str = str(ft).strip()
                    if len(ft_str) >= 6:
                        stock['first_limit_up_time'] = f"{ft_str[:2]}:{ft_str[2:4]}:{ft_str[4:6]}"
                    elif len(ft_str) == 4:
                        stock['first_limit_up_time'] = f"{ft_str[:2]}:{ft_str[2:]}:00"
                result.append(stock)
            # 用真实连板数覆盖API字段
            self._apply_real_boards(result, date)
            return result
        finally:
            conn.close()

    def _get_daily_summary(self, date: str) -> Optional[Dict]:
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT date, limit_up_count, limit_down_count, break_limit_up_count,
                       explosion_rate, market_heat, max_continuous_boards, board_distribution
                FROM xgt_daily_summary WHERE date = ?
            """, (date,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _get_smash_coefficient(self, date: str) -> Optional[float]:
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT smash_coefficient FROM smash_coefficients
                WHERE trade_date = ?
            """, (date,)).fetchone()
            if row and row['smash_coefficient'] is not None:
                return row['smash_coefficient']
            row = conn.execute("""
                SELECT smash_coefficient FROM smash_coefficients
                WHERE trade_date < ? AND smash_coefficient IS NOT NULL
                ORDER BY trade_date DESC LIMIT 1
            """, (date,)).fetchone()
            return row['smash_coefficient'] if row else None
        finally:
            conn.close()

    def _get_concept_stats(self, date: str) -> Dict[str, int]:
        """获取当日概念统计"""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT concept, count FROM concept_statistics
                WHERE date = ? ORDER BY count DESC
            """, (date,)).fetchall()
            return {r['concept']: r['count'] for r in rows}
        finally:
            conn.close()

    def _get_prev_trading_day(self, date: str) -> Optional[str]:
        """获取前一交易日"""
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT MAX(date) as d FROM xgt_daily_summary WHERE date < ?
            """, (date,)).fetchone()
            return row['d'] if row and row['d'] else None
        finally:
            conn.close()

    def _get_stock_history(self, code: str, date: str, days: int = 10) -> List[Dict]:
        """获取个股近N日涨停历史"""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT date, code, name, limit_up_days, seal_ratio,
                       turnover_rate, break_times, first_limit_up_time, concept
                FROM xgt_limit_up_detail
                WHERE code = ? AND date <= ?
                ORDER BY date DESC LIMIT ?
            """, (code, date, days)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _batch_get_history(self, codes: List[str], date: str,
                            days: int = 10) -> Dict[str, List[Dict]]:
        """批量获取多只个股历史，减少N+1查询"""
        if not codes:
            return {}
        conn = self._get_conn()
        try:
            placeholders = ','.join(['?' for _ in codes])
            rows = conn.execute(f"""
                SELECT date, code, name, limit_up_days, seal_ratio,
                       turnover_rate, break_times, first_limit_up_time, concept
                FROM xgt_limit_up_detail
                WHERE code IN ({placeholders}) AND date <= ?
                ORDER BY code, date DESC
            """, list(codes) + [date]).fetchall()
            result = defaultdict(list)
            for r in rows:
                d = dict(r)
                if len(result[d['code']]) < days:
                    result[d['code']].append(d)
            return dict(result)
        finally:
            conn.close()

    def _get_prev_day_stocks(self, prev_date: str) -> List[Dict]:
        """获取前一交易日涨停股"""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT code, name, limit_up_days, concept, seal_ratio
                FROM xgt_limit_up_detail
                WHERE date = ?
            """, (prev_date,)).fetchall()
            result = [dict(r) for r in rows]
            self._apply_real_boards(result, prev_date)
            return result
        finally:
            conn.close()

    def _get_break_stocks(self, date: str) -> List[Dict]:
        """获取当日炸板股"""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT code, name, limit_up_days, concept, break_times
                FROM xgt_break_limit_up WHERE date = ?
            """, (date,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _get_prev_dragon_info(self, code: str, date: str,
                                lookback_days: int = 5) -> Optional[Dict]:
        """
        获取个股近期龙头历史：
        - 前N个交易日内是否被识别为龙头
        - 最高确定性等级、连续龙头天数
        - 是否断板（昨天是龙头但今天不在涨停列表）
        返回 None 表示近期无龙头记录
        """
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT detect_date, certainty_level, dragon_type,
                       total_score, limit_up_days, lifecycle_stage
                FROM dragon_detections
                WHERE code = ? AND detect_date < ?
                ORDER BY detect_date DESC LIMIT ?
            """, (code, date, lookback_days)).fetchall()
            if not rows:
                return None
            # 计算连续龙头天数（从最近一天往前数）
            all_dates = [r['detect_date'] for r in conn.execute("""
                SELECT DISTINCT detect_date FROM dragon_detections
                WHERE detect_date < ? ORDER BY detect_date DESC LIMIT 30
            """, (date,)).fetchall()]
            consecutive = 0
            for d in all_dates:
                hit = any(r['detect_date'] == d for r in rows)
                if hit:
                    consecutive += 1
                else:
                    break
            best_level = max(rows, key=lambda r: (
                {'SS':4,'S':3,'A':2,'B':1}.get(r['certainty_level'],0)
            ))
            return {
                'consecutive_days': consecutive,
                'best_level': best_level['certainty_level'],
                'best_score': best_level['total_score'],
                'prev_level': rows[0]['certainty_level'],
                'prev_score': rows[0]['total_score'],
                'prev_type': rows[0]['dragon_type'],
                'prev_boards': rows[0]['limit_up_days'],
                'prev_lifecycle': rows[0]['lifecycle_stage'],
                'recent_count': len(rows),
                'history': [dict(r) for r in rows],
            }
        finally:
            conn.close()

    def _score_continuity(self, stock: Dict, prev_dragon: Optional[Dict],
                           all_stocks: List[Dict]) -> Tuple[float, List[str]]:
        """
        维度7：龙头连续性（5%）
        - 连续多日被识别为龙头，市场地位已确立，加分
        - 昨日龙头今日继续涨停（成功晋级），加分
        - 昨日高位龙头今日衰退（断板/大跌），大幅扣分
        - 新晋龙头（首次入选），中性偏正
        """
        reasons = []
        score = 50.0  # 基础分（无历史 = 中性）

        if not prev_dragon:
            # 首次进入龙头候选
            score = 55.0
            reasons.append("新晋龙头候选，市场地位待确认")
            return score, reasons

        consec = prev_dragon['consecutive_days']
        prev_level = prev_dragon['prev_level']
        prev_score = prev_dragon['prev_score']
        prev_lifecycle = prev_dragon['prev_lifecycle']
        prev_boards = prev_dragon['prev_boards']
        boards = stock.get('limit_up_days', 1) or 1

        level_score = {'SS': 15, 'S': 10, 'A': 6, 'B': 2}
        bonus = level_score.get(prev_level, 0)

        if consec >= 4:
            score = 85 + bonus
            reasons.append(f"🔥 连续{consec}日龙头（前值{prev_level}级{prev_score}分），市场核心地位稳固")
        elif consec >= 2:
            score = 70 + bonus
            reasons.append(f"连续{consec}日龙头（前值{prev_level}级），地位逐步确认")
        else:
            score = 55 + bonus
            reasons.append(f"前日龙头（{prev_level}级），今日延续")

        # 晋级/断板判断
        if boards > prev_boards:
            score += 8
            reasons.append(f"成功晋级（{prev_boards}板→{boards}板），龙头继续走强")
        elif boards < prev_boards:
            # 板数下降但仍在涨停（可能是数据源问题或换手后重新启动）
            score -= 5
            reasons.append(f"板数下降（{prev_boards}板→{boards}板），需确认是否重新启动")

        # 前日高潮期/衰退期，今日继续涨停的加分
        if prev_lifecycle == 'climax' and boards >= prev_boards:
            score += 5
            reasons.append("高位继续封板，超预期强势")

        # 前日衰退期但今日重新封板（龙回头）
        if prev_lifecycle == 'decline':
            score -= 10
            reasons.append("前期龙头衰退后反弹，不确定性高")

        return max(0, min(score, 100)), reasons

    # ─────────────────────────── 建表 ───────────────────────────

    @staticmethod
    def init_tables(db_path: str = DB_PATH):
        """创建龙头识别相关表"""
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dragon_detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detect_date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                dragon_type TEXT,
                certainty_level TEXT,
                total_score REAL,
                board_position_score REAL,
                seal_resolution_score REAL,
                sector_leadership_score REAL,
                market_recognition_score REAL,
                concept_purity_score REAL,
                counter_trend_score REAL,
                lifecycle_stage TEXT,
                concept TEXT,
                limit_up_days INTEGER,
                seal_ratio REAL,
                reasons TEXT,
                risks TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(detect_date, code)
            );

            CREATE TABLE IF NOT EXISTS dragon_lifecycle (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                name TEXT,
                concept TEXT,
                first_seen_date TEXT,
                peak_boards INTEGER DEFAULT 0,
                current_stage TEXT,
                total_limit_up_days INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                end_date TEXT,
                end_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(code, first_seen_date)
            );

            CREATE TABLE IF NOT EXISTS dragon_cycle_context (
                date TEXT PRIMARY KEY,
                total_dragon_code TEXT,
                total_dragon_name TEXT,
                total_dragon_boards INTEGER,
                top_concept TEXT,
                top_concept_count INTEGER,
                dragon_tier TEXT,
                market_phase TEXT,
                smash_coefficient REAL,
                details TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        conn.close()
        logger.info("龙头识别相关表初始化完成")

    # ─────────────────────────── 6维度评分 ───────────────────────────

    def _score_board_position(self, stock: Dict, all_stocks: List[Dict],
                               max_boards: int) -> Tuple[float, List[str]]:
        """
        维度1：身位优势（25%）
        评估个股在连板梯队中的领先程度。
        - 最高板且唯一：满分
        - 最高板但有并列：扣分
        - 与最高板差距越大，分数越低
        - 2板以下基本不具备龙头身位
        """
        reasons = []
        boards = stock.get('limit_up_days', 1) or 1
        score = 30.0  # 基础分

        # 统计各板级数量
        board_counts = Counter(s.get('limit_up_days', 1) or 1 for s in all_stocks)
        same_level_count = board_counts.get(boards, 1)

        if boards >= max_boards and boards >= 3:
            # 最高板梯队
            if same_level_count == 1:
                score = 95.0
                reasons.append(f"🏆 全市场唯一{boards}板，绝对身位优势")
            else:
                score = 80.0
                reasons.append(f"⚔️ {boards}板并列最高（共{same_level_count}只），身位优势被分流")
        elif boards >= max_boards - 1 and boards >= 2:
            # 次高板
            gap = max_boards - boards
            score = 65.0 - gap * 5
            reasons.append(f"{boards}板，距最高板差{gap}个身位")
        elif boards >= 3:
            score = 50.0
            reasons.append(f"{boards}板，中高梯队但身位不突出")
        elif boards == 2:
            score = 35.0
            reasons.append("2连板，身位偏低，需后续确认")
        else:
            score = 20.0
            reasons.append("首板，无身位优势")

        # 连板天数奖励（3板以上额外加分）
        if boards >= 5:
            score += 5
            reasons.append(f"高标{boards}板，市场辨识度极高")
        elif boards >= 3:
            score += 3

        return min(score, 100), reasons

    def _score_seal_resolution(self, stock: Dict, explosion_rate: float) -> Tuple[float, List[str]]:
        """
        维度2：封板坚决度（25%）
        评估封板质量：封单比、开板次数、封板时间、换手率。
        龙头股必须封板坚决，反复开板的不是真龙头。
        """
        reasons = []
        score = 50.0

        seal_ratio = stock.get('seal_ratio') or 0
        break_times = stock.get('break_times') or 0
        first_time = stock.get('first_limit_up_time', '') or ''
        turnover = stock.get('turnover_rate') or 0

        # 封单比评分（最重要）
        if seal_ratio >= 0.10:
            score += 25
            reasons.append(f"封单比{seal_ratio:.1%}（极强，大单封死）")
        elif seal_ratio >= 0.05:
            score += 18
            reasons.append(f"封单比{seal_ratio:.1%}（强）")
        elif seal_ratio >= 0.03:
            score += 10
            reasons.append(f"封单比{seal_ratio:.1%}（中等）")
        elif seal_ratio >= 0.01:
            score += 3
            reasons.append(f"封单比{seal_ratio:.1%}（偏弱）")
        else:
            score -= 5
            reasons.append(f"封单比{seal_ratio:.1%}（弱，封板不坚决）")

        # 开板次数
        if break_times == 0:
            score += 15
            reasons.append("全天零开板，封板坚决")
        elif break_times <= 1:
            score += 8
            reasons.append(f"开板{break_times}次后回封，分歧转一致")
        elif break_times <= 3:
            score -= 2
            reasons.append(f"开板{break_times}次，封板有分歧")
        else:
            score -= 15
            reasons.append(f"开板{break_times}次，封板极不坚决")

        # 封板时间
        if first_time:
            try:
                parts = first_time.split(':')
                h, m = int(parts[0]), int(parts[1])
                minutes = h * 60 + m
                # 交易时段：9:25=565(集合竞价), 9:30=570, 10:00=600, 11:30=690, 13:00=780, 14:00=840, 15:00=900
                if minutes <= 570:
                    score += 12
                    reasons.append(f"{first_time}封板（集合竞价/开盘秒板，极强）")
                elif minutes <= 600:
                    score += 8
                    reasons.append(f"{first_time}封板（早盘快速封板）")
                elif minutes <= 690:
                    score += 4
                    reasons.append(f"{first_time}封板（上午封板）")
                elif minutes <= 840:
                    score += 0
                    reasons.append(f"{first_time}封板（下午封板）")
                else:
                    score -= 5
                    reasons.append(f"{first_time}封板（尾盘封板，偏弱）")
            except (ValueError, IndexError):
                pass

        # 换手率（龙头股换手不宜过高或过低）
        boards = stock.get('limit_up_days', 1) or 1
        if boards >= 3:
            # 高位股适度换手健康，过高则危险
            if 0.05 <= turnover <= 0.20:
                score += 5
                reasons.append(f"换手{turnover:.1%}（健康换手）")
            elif turnover > 0.30:
                score -= 8
                reasons.append(f"换手{turnover:.1%}（过高，抛压沉重）")
            elif turnover < 0.03:
                score += 3
                reasons.append(f"换手{turnover:.1%}（缩量一字，筹码稳定）")
        else:
            # 低位股放量涨停说明资金认可
            if 0.05 <= turnover <= 0.25:
                score += 3
                reasons.append(f"换手{turnover:.1%}（活跃）")

        # 炸板率环境加成：在高炸板率环境中仍封住的，更显坚决
        if explosion_rate > 0.25 and break_times == 0 and seal_ratio >= 0.03:
            score += 5
            reasons.append(f"炸板率{explosion_rate:.0%}环境下零开板，逆势封板坚决")

        return max(0, min(score, 100)), reasons

    def _score_sector_leadership(self, stock: Dict, all_stocks: List[Dict],
                                  concept_stats: Dict[str, int]) -> Tuple[float, List[str]]:
        """
        维度3：板块带动性（20%）
        评估个股对所属概念板块的带动能力。
        - 同概念涨停家数越多，带动性越强
        - 是否为该概念最高板
        - 概念是否为当日主流热点
        """
        reasons = []
        score = 30.0

        concept = (stock.get('concept') or '').strip()
        boards = stock.get('limit_up_days', 1) or 1

        if not concept:
            return 30.0, ["概念数据缺失，无法评估板块带动性"]

        # 分割多概念（分号分隔）
        concepts = [c.strip() for c in concept.split(';') if c.strip()]
        primary_concept = concepts[0] if concepts else concept

        # 该概念当日涨停家数
        concept_count = concept_stats.get(primary_concept, 0)
        # 也检查其他概念
        for c in concepts[1:]:
            concept_count = max(concept_count, concept_stats.get(c, 0))

        # 同概念个股（精确匹配：任意一个概念完全相等才算同板块）
        same_concept_stocks = []
        concept_set = set(concepts)
        for s in all_stocks:
            s_concept = (s.get('concept') or '').strip()
            if not s_concept:
                continue
            s_concepts = set(c.strip() for c in s_concept.split(';') if c.strip())
            if concept_set & s_concepts:  # 交集
                same_concept_stocks.append(s)

        same_concept_count = len(same_concept_stocks)

        # 该概念内最高板
        concept_max_board = max(
            (s.get('limit_up_days', 1) or 1) for s in same_concept_stocks
        ) if same_concept_stocks else boards

        # 评分
        if same_concept_count >= 8:
            score = 90.0
            reasons.append(f"🔥 所属「{primary_concept}」板块{same_concept_count}只涨停，板块效应极强")
        elif same_concept_count >= 5:
            score = 78.0
            reasons.append(f"「{primary_concept}」板块{same_concept_count}只涨停，板块效应强")
        elif same_concept_count >= 3:
            score = 60.0
            reasons.append(f"「{primary_concept}」板块{same_concept_count}只涨停，有一定板块效应")
        elif same_concept_count >= 2:
            score = 45.0
            reasons.append(f"「{primary_concept}」板块仅{same_concept_count}只涨停，板块效应弱")
        else:
            score = 25.0
            reasons.append(f"「{primary_concept}」板块孤板，无带动效应")

        # 是否为概念内最高板
        if boards >= concept_max_board and same_concept_count >= 2:
            score += 8
            reasons.append(f"为「{primary_concept}」板块最高板({boards}板)，领涨地位明确")
        elif boards < concept_max_board:
            score -= 5
            reasons.append(f"板块内最高板为{concept_max_board}板，身位非最高")

        # 概念排名加成
        sorted_concepts = sorted(concept_stats.items(), key=lambda x: x[1], reverse=True)
        concept_rank = next((i+1 for i, (c, _) in enumerate(sorted_concepts)
                            if primary_concept in c or c in concepts), None)
        if concept_rank == 1:
            score += 5
            reasons.append("所属概念为当日第一大热点")
        elif concept_rank and concept_rank <= 3:
            score += 3
            reasons.append(f"所属概念当日热度排名第{concept_rank}")

        return max(0, min(score, 100)), reasons

    def _score_market_recognition(self, stock: Dict, all_stocks: List[Dict],
                                   max_boards: int,
                                   stock_history: List[Dict]) -> Tuple[float, List[str]]:
        """
        维度4：市场辨识度（15%）
        评估个股在全市场的认知度。
        - 最高板唯一性
        - 历史股性（是否经常涨停）
        - 市值适中（小盘龙头弹性大，但过小流动性差）
        - 是否有知名游资参与（龙虎榜数据，如有）
        """
        reasons = []
        score = 40.0

        boards = stock.get('limit_up_days', 1) or 1
        flow_cap = stock.get('flow_capital') or 0

        # 最高板辨识度
        board_counts = Counter(s.get('limit_up_days', 1) or 1 for s in all_stocks)
        same_level = board_counts.get(boards, 1)

        if boards == max_boards and boards >= 3:
            if same_level == 1:
                score += 30
                reasons.append(f"全市场唯一{boards}板，市场焦点所在")
            else:
                score += 15
                reasons.append(f"{boards}板最高板梯队（{same_level}只并列），辨识度被分流")
        elif boards >= max_boards - 1 and boards >= 3:
            score += 10
            reasons.append(f"{boards}板次高标，有一定市场关注度")

        # 历史股性：近10日涨停次数
        history_limit_up_count = len(stock_history)
        if history_limit_up_count >= 5:
            score += 10
            reasons.append(f"近10日{history_limit_up_count}次涨停，股性极为活跃")
        elif history_limit_up_count >= 3:
            score += 6
            reasons.append(f"近10日{history_limit_up_count}次涨停，股性活跃")
        elif history_limit_up_count >= 2:
            score += 3
            reasons.append(f"近10日{history_limit_up_count}次涨停，股性尚可")

        # 市值评分（龙头股偏好中小盘，30-80亿最佳）
        if flow_cap > 0:
            if 20 <= flow_cap <= 80:
                score += 10
                reasons.append(f"流通市值{flow_cap:.0f}亿（黄金市值区间，弹性好）")
            elif 10 <= flow_cap < 20:
                score += 7
                reasons.append(f"流通市值{flow_cap:.0f}亿（小盘，弹性大但波动也大）")
            elif 80 < flow_cap <= 150:
                score += 5
                reasons.append(f"流通市值{flow_cap:.0f}亿（中盘，稳定性好）")
            elif flow_cap > 200:
                score -= 3
                reasons.append(f"流通市值{flow_cap:.0f}亿（偏大，连板弹性受限）")
        else:
            reasons.append("市值数据缺失")

        return max(0, min(score, 100)), reasons

    def _score_concept_purity(self, stock: Dict, concept_stats: Dict[str, int]) -> Tuple[float, List[str]]:
        """
        维度5：概念纯正度（10%）
        评估个股与热点概念的关联程度。
        - 单一纯正概念 > 多概念叠加
        - 概念排序越靠前越正
        - 涨停原因中是否明确提及该概念
        """
        reasons = []
        score = 50.0

        concept = (stock.get('concept') or '').strip()
        reason_text = (stock.get('reason') or '').strip()

        if not concept:
            return 40.0, ["无概念数据"]

        concepts = [c.strip() for c in concept.split(';') if c.strip()]

        # 概念数量：单一概念最纯，多概念加分流
        if len(concepts) == 1:
            score += 20
            reasons.append(f"单一纯正概念「{concepts[0]}」")
        elif len(concepts) == 2:
            score += 10
            reasons.append(f"双概念「{'/'.join(concepts)}」，主业较清晰")
        elif len(concepts) >= 4:
            score -= 5
            reasons.append(f"概念过多（{len(concepts)}个），主线不清晰")
        else:
            score += 5
            reasons.append(f"三概念「{'/'.join(concepts)}」")

        # 涨停原因与概念匹配
        primary = concepts[0]
        if reason_text and primary in reason_text:
            score += 15
            reasons.append(f"涨停原因明确涉及「{primary}」，概念纯正")
        elif reason_text:
            # 检查reason中是否有任何概念关键词
            matched = [c for c in concepts if c in reason_text]
            if matched:
                score += 8
                reasons.append(f"涨停原因涉及「{matched[0]}」")
            else:
                score -= 5
                reasons.append("涨停原因与概念关联度不明确")

        # 概念热度
        hot_count = concept_stats.get(primary, 0)
        if hot_count >= 5:
            score += 10
        elif hot_count >= 3:
            score += 5

        return max(0, min(score, 100)), reasons

    def _score_counter_trend(self, stock: Dict, smash: Optional[float],
                              explosion_rate: float,
                              break_stocks: List[Dict]) -> Tuple[float, List[str]]:
        """
        维度6：逆势强度（10%）
        评估个股在恶劣市场环境中的抗压能力。
        - 高砸盘系数时仍能封板
        - 高炸板率时零开板
        - 同板块其他股炸板时它仍封住
        逆势封板的龙头含金量远高于牛市跟风涨停
        """
        reasons = []
        score = 40.0  # 基础分降低，拉开逆势股差距

        boards = stock.get('limit_up_days', 1) or 1
        break_times = stock.get('break_times') or 0
        seal_ratio = stock.get('seal_ratio') or 0
        concept = (stock.get('concept') or '').strip()

        # 砸盘系数高时仍封板（主要区分项）
        if smash is not None:
            if smash >= 7.0 and break_times == 0 and seal_ratio >= 0.03:
                score += 35
                reasons.append(f"砸盘系数{smash:.1f}（极端恶劣）仍零开板强封，逆势龙头")
            elif smash >= 7.0:
                score += 15
                reasons.append(f"砸盘系数{smash:.1f}极端环境仍涨停（开板{break_times}次）")
            elif smash >= 5.5 and break_times == 0 and seal_ratio >= 0.02:
                score += 25
                reasons.append(f"砸盘系数{smash:.1f}（恶劣）零开板封板，逆势强")
            elif smash >= 5.5 and break_times <= 1:
                score += 15
                reasons.append(f"砸盘系数{smash:.1f}偏高仅开板{break_times}次")
            elif smash >= 4.5 and break_times == 0:
                score += 12
                reasons.append(f"砸盘系数{smash:.1f}不低仍零开板")
            elif smash < 3.0 and break_times == 0:
                score += 8
                reasons.append(f"砸盘系数{smash:.1f}（友好），环境助力")
            else:
                score += 5

        # 炸板率高时表现
        if explosion_rate > 0.30 and break_times == 0 and seal_ratio >= 0.03:
            score += 18
            reasons.append(f"炸板率{explosion_rate:.0%}环境下强势零开板")
        elif explosion_rate > 0.30 and break_times <= 1:
            score += 10
        elif explosion_rate > 0.20 and break_times == 0:
            score += 8

        # 同概念有炸板股但它封住（精确概念匹配）
        if concept and break_stocks:
            concepts = set(c.strip() for c in concept.split(';') if c.strip())
            same_concept_breaks = []
            for b in break_stocks:
                bc = (b.get('concept') or '').strip()
                if bc:
                    bc_set = set(c.strip() for c in bc.split(';') if c.strip())
                    if concepts & bc_set:
                        same_concept_breaks.append(b)
            if same_concept_breaks and break_times == 0:
                score += 12
                reasons.append(f"同板块{len(same_concept_breaks)}只炸板，该股仍封住")

        # 高板逆势更可贵
        if boards >= 4 and smash is not None and smash >= 5.0 and break_times == 0:
            score += 8
            reasons.append(f"{boards}板高标在砸盘{smash:.1f}中零开板，极为难得")
        elif boards >= 5 and smash is not None and smash >= 4.0:
            score += 4

        return max(0, min(score, 100)), reasons

    # ─────────────────────────── 龙头分类 ───────────────────────────

    def _classify_dragon(self, stock: Dict, all_stocks: List[Dict],
                          max_boards: int, concept_stats: Dict[str, int],
                          smash: Optional[float],
                          prev_stocks: List[Dict],
                          total_score: float = 0,
                          lifecycle: str = 'launch') -> str:
        """
        判断龙头类型：
        - 总龙头：全市场最高板（≥3板），且封板最坚决、非衰退期、总分≥65
        - 板块龙：非全市场最高，但所属概念板块内最高
        - 补涨龙：主线概念中，身位低于板块龙但2板+强势
        - 切换龙：新题材首日爆发，在砸盘系数高/老周期退潮时启动
        """
        boards = stock.get('limit_up_days', 1) or 1
        concept = (stock.get('concept') or '').strip()
        seal_ratio = stock.get('seal_ratio') or 0
        break_times = stock.get('break_times') or 0

        # 总龙头判断：全市场最高板，但必须满足质量门槛
        if boards >= max_boards and boards >= 3:
            board_counts = Counter(s.get('limit_up_days', 1) or 1 for s in all_stocks)
            same_level = board_counts.get(boards, 1)
            # 衰退期高板股不算总龙头（如10板decline）
            if lifecycle == 'decline':
                pass  # 落到后续判断
            elif total_score >= 65 and same_level == 1:
                return 'total_dragon'
            elif same_level > 1:
                # 并列最高板中，封单比最高且总分达标才算总龙头
                same_level_stocks = [s for s in all_stocks
                                     if (s.get('limit_up_days', 1) or 1) == boards]
                best_seal = max((s.get('seal_ratio') or 0) for s in same_level_stocks)
                if seal_ratio >= best_seal - 0.005 and total_score >= 65:
                    return 'total_dragon'
                else:
                    return 'sector_dragon'
            elif total_score >= 65:
                return 'total_dragon'

        # 板块龙判断：概念内最高板
        if concept:
            concepts = [c.strip() for c in concept.split(';') if c.strip()]
            primary = concepts[0]
            same_concept = []
            for s in all_stocks:
                s_concept = (s.get('concept') or '').strip()
                s_concepts = [c.strip() for c in s_concept.split(';') if c.strip()]
                if any(c in s_concepts for c in concepts):
                    same_concept.append(s)
            if same_concept:
                concept_max = max((s.get('limit_up_days', 1) or 1) for s in same_concept)
                if boards >= concept_max and boards >= 2:
                    return 'sector_dragon'

        # 切换龙判断：新题材+高砸盘/退潮期
        if smash is not None and smash >= 5.0 and boards <= 2:
            # 检查是否是新题材（前一日该概念无涨停）
            if concept and prev_stocks:
                prev_concepts = set()
                for ps in prev_stocks:
                    pc = (ps.get('concept') or '').strip()
                    if pc:
                        prev_concepts.update(c.strip() for c in pc.split(';') if c.strip())
                concepts = [c.strip() for c in concept.split(';') if c.strip()]
                is_new_concept = not any(c in prev_concepts for c in concepts)
                if is_new_concept and seal_ratio >= 0.03 and break_times == 0:
                    return 'switch_dragon'

        # 补涨龙判断
        if concept and boards >= 2:
            concepts = [c.strip() for c in concept.split(';') if c.strip()]
            same_concept_max = 0
            for s in all_stocks:
                s_concept = (s.get('concept') or '').strip()
                s_concepts = [c.strip() for c in s_concept.split(';') if c.strip()]
                if any(c in s_concepts for c in concepts):
                    same_concept_max = max(same_concept_max, s.get('limit_up_days', 1) or 1)
            if same_concept_max > boards:
                return 'catch_up_dragon'

        # 默认为板块龙（有板块效应的2板+）
        if boards >= 2:
            return 'sector_dragon'

        return 'catch_up_dragon'

    def _determine_lifecycle(self, stock: Dict, stock_history: List[Dict],
                              smash: Optional[float],
                              prev_dragon: Optional[Dict] = None) -> str:
        """
        判断龙头生命周期阶段：
        - 启动期 launch：1-2板，刚开始
        - 加速期 acceleration：3-4板，确认龙头
        - 高潮期 climax：5板+，市场共识
        - 衰退期 decline：高换手/炸板/封单极弱/砸盘极高/前日已衰退

        新增高位分歧判断（仍归为climax但标注分歧）：
        - 5板+开板1-2次+换手20-35% = 高潮分歧
        - 5板+封单<3% = 高潮末期
        """
        boards = stock.get('limit_up_days', 1) or 1
        turnover = stock.get('turnover_rate') or 0
        break_times = stock.get('break_times') or 0
        seal_ratio = stock.get('seal_ratio') or 0
        first_time = stock.get('first_limit_up_time', '') or ''

        # 解析封板时间
        minutes = 999
        if first_time:
            try:
                parts = first_time.split(':')
                minutes = int(parts[0]) * 60 + int(parts[1])
            except (ValueError, IndexError):
                pass

        # 衰退信号检测（更严格）
        decline_signals = 0
        # 1. 高位+极高换手（>35%）= 筹码大换手，主力出货
        if turnover > 0.35 and boards >= 4:
            decline_signals += 2  # 权重加倍
        # 2. 多次开板（≥3次）= 封板极不坚决
        if break_times >= 3:
            decline_signals += 1
        # 3. 砸盘系数极高+高位股
        if smash is not None and smash >= 7.0 and boards >= 4:
            decline_signals += 1
        # 4. 封单极弱（<1%）+3板以上
        if seal_ratio < 0.01 and boards >= 3:
            decline_signals += 1
        # 5. 尾盘封板（14:00后）+高位+有开板
        if minutes >= 840 and boards >= 4 and break_times >= 1:
            decline_signals += 1
        # 6. 前日已衰退，今日虽涨停但弱势
        if prev_dragon and prev_dragon.get('prev_lifecycle') == 'decline':
            decline_signals += 2
        # 兼容简单dict格式
        if prev_dragon and prev_dragon.get('lifecycle_stage') == 'decline':
            decline_signals += 2

        if decline_signals >= 2:
            return 'decline'

        # 高位（5板+）但有分歧信号（开板1-2次或换手偏高），仍属高潮但记录分歧
        if boards >= 5:
            return 'climax'
        elif boards >= 3:
            return 'acceleration'
        else:
            return 'launch'

    # ─────────────────────────── 主检测流程 ───────────────────────────

    def detect_dragons(self, date: str, save: bool = True) -> List[Dict[str, Any]]:
        """
        检测指定日期的确定性龙头。

        返回按总分排序的龙头列表，每个龙头包含：
        - 基本信息、6维度评分、龙头类型、生命周期、确定性等级
        - 推荐理由和风险提示
        """
        logger.info(f"[{date}] 开始龙头识别...")

        # 获取数据
        all_stocks = self._get_limit_up_stocks(date)
        if not all_stocks:
            logger.warning(f"[{date}] 无涨停数据")
            return []

        summary = self._get_daily_summary(date)
        smash = self._get_smash_coefficient(date)
        concept_stats = self._get_concept_stats(date)
        prev_date = self._get_prev_trading_day(date)
        prev_stocks = self._get_prev_day_stocks(prev_date) if prev_date else []
        break_stocks = self._get_break_stocks(date)

        max_boards = max((s.get('limit_up_days', 1) or 1) for s in all_stocks)
        explosion_rate = (summary.get('explosion_rate', 0) if summary else 0) or 0

        logger.info(f"[{date}] 涨停{len(all_stocks)}只, 最高{max_boards}板, "
                    f"炸板率{explosion_rate:.1%}, 砸盘系数{smash}")

        # 过滤ST
        candidates = [s for s in all_stocks
                      if s.get('name') and 'ST' not in s['name']]

        # 批量获取个股历史（避免N+1查询）
        candidate_codes = [s['code'] for s in candidates]
        history_map = self._batch_get_history(candidate_codes, date, days=10)

        # 预取所有候选股的前日龙头信息（避免N+1）
        prev_dragon_map = {}
        conn = self._get_conn()
        try:
            for code in candidate_codes:
                info = self._get_prev_dragon_info(code, date, lookback_days=5)
                if info:
                    prev_dragon_map[code] = info
        finally:
            conn.close()

        dragons = []
        for stock in candidates:
            boards = stock.get('limit_up_days', 1) or 1

            # 首板不参与龙头评选（除非是唯一涨停或特殊切换龙）
            if boards < 2 and len(candidates) > 3:
                continue

            # 获取个股历史（从批量缓存）
            stock_history = history_map.get(stock['code'], [])

            # 前日龙头信息
            prev_dragon = prev_dragon_map.get(stock['code'])

            # 7维度评分
            bp_score, bp_reasons = self._score_board_position(
                stock, candidates, max_boards)
            sr_score, sr_reasons = self._score_seal_resolution(
                stock, explosion_rate)
            sl_score, sl_reasons = self._score_sector_leadership(
                stock, candidates, concept_stats)
            mr_score, mr_reasons = self._score_market_recognition(
                stock, candidates, max_boards, stock_history)
            cp_score, cp_reasons = self._score_concept_purity(
                stock, concept_stats)
            ct_score, ct_reasons = self._score_counter_trend(
                stock, smash, explosion_rate, break_stocks)
            co_score, co_reasons = self._score_continuity(
                stock, prev_dragon, candidates)

            # 加权总分
            total = (
                bp_score * DRAGON_DIMENSION_WEIGHTS['board_position'] +
                sr_score * DRAGON_DIMENSION_WEIGHTS['seal_resolution'] +
                sl_score * DRAGON_DIMENSION_WEIGHTS['sector_leadership'] +
                mr_score * DRAGON_DIMENSION_WEIGHTS['market_recognition'] +
                cp_score * DRAGON_DIMENSION_WEIGHTS['concept_purity'] +
                ct_score * DRAGON_DIMENSION_WEIGHTS['counter_trend'] +
                co_score * DRAGON_DIMENSION_WEIGHTS['continuity']
            )
            total = round(total, 1)

            # 生命周期（先判断，用于分类）
            lifecycle = self._determine_lifecycle(
                stock, stock_history, smash, prev_dragon)

            # 龙头分类（传入总分和生命周期做质量门槛）
            dragon_type = self._classify_dragon(
                stock, candidates, max_boards, concept_stats, smash,
                prev_stocks, total_score=total, lifecycle=lifecycle)

            # 确定性等级
            certainty = self._get_certainty_level(total, dragon_type, lifecycle)

            # 合并理由
            all_reasons = (bp_reasons + sr_reasons + sl_reasons +
                          mr_reasons + cp_reasons + ct_reasons + co_reasons)

            # 风险提示
            risks = self._generate_risks(
                stock, dragon_type, lifecycle, smash, explosion_rate)

            # 总龙头加分（如果是总龙头，额外+5分辨识度加成已在维度中体现）
            # 衰退期降权
            if lifecycle == 'decline':
                total = round(total * 0.8, 1)
                risks.append("⚠️ 龙头已进入衰退期，盈亏比恶化")

            dragon = {
                'code': stock['code'],
                'name': stock['name'],
                'price': stock.get('price', 0),
                'concept': stock.get('concept', ''),
                'limit_up_days': boards,
                'seal_ratio': stock.get('seal_ratio', 0),
                'turnover_rate': stock.get('turnover_rate', 0),
                'flow_capital': stock.get('flow_capital', 0),
                'first_limit_up_time': stock.get('first_limit_up_time', ''),
                'break_times': stock.get('break_times', 0),
                'dragon_type': dragon_type,
                'dragon_type_name': DRAGON_TYPES[dragon_type]['name'],
                'dragon_type_desc': DRAGON_TYPES[dragon_type]['desc'],
                'lifecycle_stage': lifecycle,
                'lifecycle_name': LIFECYCLE_STAGES[lifecycle]['name'],
                'lifecycle_desc': LIFECYCLE_STAGES[lifecycle]['desc'],
                'certainty_level': certainty['level'],
                'certainty_name': certainty['name'],
                'total_score': total,
                'dimension_scores': {
                    'board_position': round(bp_score, 1),
                    'seal_resolution': round(sr_score, 1),
                    'sector_leadership': round(sl_score, 1),
                    'market_recognition': round(mr_score, 1),
                    'concept_purity': round(cp_score, 1),
                    'counter_trend': round(ct_score, 1),
                    'continuity': round(co_score, 1),
                },
                'dimension_reasons': {
                    'board_position': bp_reasons,
                    'seal_resolution': sr_reasons,
                    'sector_leadership': sl_reasons,
                    'market_recognition': mr_reasons,
                    'concept_purity': cp_reasons,
                    'counter_trend': ct_reasons,
                    'continuity': co_reasons,
                },
                'prev_dragon_level': prev_dragon['prev_level'] if prev_dragon else None,
                'reasons': all_reasons,
                'top_reasons': self._select_top_reasons(all_reasons, total),
                'risks': risks,
                'detect_date': date,
            }
            dragons.append(dragon)

        # 按总分排序
        dragons.sort(key=lambda x: x['total_score'], reverse=True)

        # 保存到数据库
        if save and dragons:
            self._save_detections(date, dragons)
            self._update_cycle_context(date, dragons, concept_stats, smash, summary)

        logger.info(f"[{date}] 龙头识别完成: {len(dragons)}只候选, "
                    f"最高评分: {dragons[0]['total_score'] if dragons else 'N/A'}")

        return dragons

    def _get_certainty_level(self, total_score: float, dragon_type: str,
                              lifecycle: str) -> Dict[str, str]:
        """根据总分和龙头类型确定确定性等级"""
        # 衰退期降级
        adjusted_score = total_score
        if lifecycle == 'decline':
            adjusted_score -= 15

        if adjusted_score >= CERTAINTY_LEVELS['SS']['min_score']:
            level = 'SS'
        elif adjusted_score >= CERTAINTY_LEVELS['S']['min_score']:
            level = 'S'
        elif adjusted_score >= CERTAINTY_LEVELS['A']['min_score']:
            level = 'A'
        else:
            level = 'B'

        return {
            'level': level,
            'name': CERTAINTY_LEVELS[level]['name'],
            'desc': CERTAINTY_LEVELS[level]['desc'],
        }

    def _select_top_reasons(self, all_reasons: List[str], score: float) -> List[str]:
        """选择最关键的3-5条理由"""
        # 优先选择含有关键emoji的理由
        priority_keywords = ['🏆', '🔥', '唯一', '极强', '绝对', '逆势', '零开板', '封单比']
        scored = []
        for r in all_reasons:
            priority = sum(2 for kw in priority_keywords if kw in r)
            scored.append((priority, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:4]]

    def _generate_risks(self, stock: Dict, dragon_type: str,
                         lifecycle: str, smash: Optional[float],
                         explosion_rate: float) -> List[str]:
        """生成风险提示"""
        risks = []
        boards = stock.get('limit_up_days', 1) or 1
        turnover = stock.get('turnover_rate') or 0
        break_times = stock.get('break_times') or 0
        seal_ratio = stock.get('seal_ratio') or 0

        if boards >= 6:
            risks.append(f"已{boards}板，高位接力风险极大，随时可能断板")
        elif boards >= 4:
            risks.append(f"{boards}板高位，注意分歧加剧风险")

        if turnover > 0.30 and boards >= 3:
            risks.append(f"换手率{turnover:.1%}过高，获利盘抛压沉重")

        if break_times >= 2:
            risks.append(f"今日开板{break_times}次，封板稳定性不足")

        if seal_ratio < 0.02 and boards >= 2:
            risks.append(f"封单比仅{seal_ratio:.1%}，封板资金不足")

        if smash is not None and smash >= 6.0:
            risks.append(f"砸盘系数{smash:.1f}偏高，市场系统性风险大")

        if explosion_rate > 0.25:
            risks.append(f"炸板率{explosion_rate:.0%}，市场情绪不稳定")

        if lifecycle == 'climax':
            risks.append("龙头处于高潮期，随时可能见顶回落")
        elif lifecycle == 'decline':
            risks.append("龙头已进入衰退期，不建议接力")

        if dragon_type == 'catch_up_dragon':
            risks.append("补涨龙身位低于龙头，一旦龙头断板容易被拖累")

        if not risks:
            risks.append("龙头状态健康，注意设置止损位")

        return risks

    # ─────────────────────────── 数据持久化 ───────────────────────────

    def _save_detections(self, date: str, dragons: List[Dict]):
        """保存龙头识别结果"""
        conn = self._get_conn()
        try:
            for d in dragons:
                conn.execute("""
                    INSERT OR REPLACE INTO dragon_detections
                    (detect_date, code, name, dragon_type, certainty_level,
                     total_score, board_position_score, seal_resolution_score,
                     sector_leadership_score, market_recognition_score,
                     concept_purity_score, counter_trend_score,
                     lifecycle_stage, concept, limit_up_days, seal_ratio,
                     reasons, risks)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    date, d['code'], d['name'], d['dragon_type'],
                    d['certainty_level'], d['total_score'],
                    d['dimension_scores']['board_position'],
                    d['dimension_scores']['seal_resolution'],
                    d['dimension_scores']['sector_leadership'],
                    d['dimension_scores']['market_recognition'],
                    d['dimension_scores']['concept_purity'],
                    d['dimension_scores']['counter_trend'],
                    d['lifecycle_stage'], d['concept'],
                    d['limit_up_days'], d['seal_ratio'],
                    json.dumps(d['reasons'], ensure_ascii=False),
                    json.dumps(d['risks'], ensure_ascii=False),
                ))
            conn.commit()
        finally:
            conn.close()

    def _update_cycle_context(self, date: str, dragons: List[Dict],
                               concept_stats: Dict[str, float],
                               smash: Optional[float],
                               summary: Optional[Dict]):
        """更新周期上下文（修复3个月未更新问题）"""
        conn = self._get_conn()
        try:
            top_dragon = dragons[0] if dragons else None
            top_concept = max(concept_stats.items(), key=lambda x: x[1])[0] if concept_stats else ''
            top_concept_count = concept_stats.get(top_concept, 0) if top_concept else 0

            # 龙头梯队描述
            tier_parts = []
            for d in dragons[:5]:
                tier_parts.append(f"{d['name']}{d['limit_up_days']}板({d['dragon_type_name']})")
            dragon_tier = ' > '.join(tier_parts)

            # 市场阶段判断
            if top_dragon:
                market_phase = top_dragon['lifecycle_name']
            else:
                market_phase = '无龙头'

            details = {
                'dragon_count': len(dragons),
                'top3': [
                    {'name': d['name'], 'code': d['code'],
                     'score': d['total_score'], 'type': d['dragon_type_name'],
                     'level': d['certainty_level']}
                    for d in dragons[:3]
                ],
                'concept_distribution': dict(list(concept_stats.items())[:10]),
            }

            conn.execute("""
                INSERT OR REPLACE INTO dragon_cycle_context
                (date, total_dragon_code, total_dragon_name, total_dragon_boards,
                 top_concept, top_concept_count, dragon_tier, market_phase,
                 smash_coefficient, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date,
                top_dragon['code'] if top_dragon else '',
                top_dragon['name'] if top_dragon else '',
                top_dragon['limit_up_days'] if top_dragon else 0,
                top_concept, top_concept_count,
                dragon_tier, market_phase, smash,
                json.dumps(details, ensure_ascii=False),
            ))
            conn.commit()
            logger.info(f"[{date}] cycle_context 已更新")
        finally:
            conn.close()

    # ─────────────────────────── 查询接口 ───────────────────────────

    def get_top_dragons(self, date: str, min_level: str = 'B',
                         limit: int = 5) -> List[Dict]:
        """获取指定日期的龙头列表（按确定性等级过滤）"""
        dragons = self.detect_dragons(date, save=True)
        level_order = {'SS': 0, 'S': 1, 'A': 2, 'B': 3}
        min_priority = level_order.get(min_level, 3)
        filtered = [d for d in dragons
                    if level_order.get(d['certainty_level'], 3) <= min_priority]
        return filtered[:limit]

    def get_total_dragon(self, date: str) -> Optional[Dict]:
        """获取指定日期的总龙头"""
        dragons = self.detect_dragons(date, save=True)
        for d in dragons:
            if d['dragon_type'] == 'total_dragon':
                return d
        # 如果没有总龙头，返回评分最高的
        return dragons[0] if dragons else None

    def get_dragons_by_concept(self, date: str, concept: str) -> List[Dict]:
        """获取指定概念的龙头"""
        dragons = self.detect_dragons(date, save=True)
        return [d for d in dragons if concept in (d.get('concept') or '')]

    def get_dragon_history(self, code: str, days: int = 30) -> List[Dict]:
        """获取个股的龙头识别历史"""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT * FROM dragon_detections
                WHERE code = ? ORDER BY detect_date DESC LIMIT ?
            """, (code, days)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ─────────────────────────── 输出格式化 ───────────────────────────

    def format_dragon_report(self, dragons: List[Dict], date: str = '') -> str:
        """格式化龙头识别报告"""
        lines = []
        lines.append(f"{'='*65}")
        lines.append(f"  🐉 确定性龙头识别报告 | {date}")
        lines.append(f"{'='*65}")
        lines.append("")

        if not dragons:
            lines.append("  ❌ 当日未识别到确定性龙头")
            lines.append("  建议：市场无明确主线，观望为主")
            lines.append("")
            return '\n'.join(lines)

        for i, d in enumerate(dragons[:8], 1):
            type_info = DRAGON_TYPES[d['dragon_type']]
            lifecycle_info = LIFECYCLE_STAGES[d['lifecycle_stage']]
            level_icon = {'SS': '🔴', 'S': '🟠', 'A': '🟡', 'B': '⚪'}
            icon = level_icon.get(d['certainty_level'], '⚪')

            lines.append(f"  {i}. {icon} 【{d['certainty_level']}级】{d['name']}({d['code']})")
            lines.append(f"     类型: {type_info['color']}{d['dragon_type_name']} | "
                        f"阶段: {lifecycle_info['icon']}{d['lifecycle_name']} | "
                        f"总分: {d['total_score']}")
            lines.append(f"     {d['limit_up_days']}板 | "
                        f"封单比{(d['seal_ratio'] or 0):.1%} | "
                        f"换手{(d['turnover_rate'] or 0):.1%} | "
                        f"流通{d.get('flow_capital', 0):.0f}亿 | "
                        f"概念: {d['concept']}")

            # 6维度雷达数据
            ds = d['dimension_scores']
            lines.append(f"     维度: 身位{ds['board_position']:.0f} | "
                        f"封板{ds['seal_resolution']:.0f} | "
                        f"带动{ds['sector_leadership']:.0f} | "
                        f"辨识{ds['market_recognition']:.0f} | "
                        f"纯正{ds['concept_purity']:.0f} | "
                        f"逆势{ds['counter_trend']:.0f}")

            # 核心理由
            if d.get('top_reasons'):
                lines.append(f"     ✅ {d['top_reasons'][0]}")
                for r in d['top_reasons'][1:3]:
                    lines.append(f"        {r}")

            # 风险
            if d.get('risks'):
                lines.append(f"     ⚠️ {d['risks'][0]}")

            lines.append("")

        lines.append(f"{'='*65}")
        lines.append("⚠️ 声明：以上为AI模型分析结果，仅供参考，不构成投资建议。")
        lines.append(f"{'='*65}")
        return '\n'.join(lines)


# ─────────────────────────── 便捷函数 ───────────────────────────

def detect_dragons(date: str = None, db_path: str = DB_PATH,
                    top_n: int = 5) -> List[Dict]:
    """便捷函数：检测龙头"""
    detector = DragonDetector(db_path)
    DragonDetector.init_tables(db_path)
    if date is None:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT MAX(date) as d FROM xgt_daily_summary").fetchone()
        date = row['d'] if row else None
        conn.close()
    if not date:
        return []
    return detector.detect_dragons(date)[:top_n]


# ─────────────────────────── 主程序 ───────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='确定性龙头识别')
    parser.add_argument('--date', type=str, default=None, help='指定日期')
    parser.add_argument('--top', type=int, default=8, help='显示数量')
    parser.add_argument('--db', type=str, default=DB_PATH, help='数据库路径')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                       format='%(asctime)s [%(levelname)s] %(message)s',
                       datefmt='%Y-%m-%d %H:%M:%S')

    DragonDetector.init_tables(args.db)
    detector = DragonDetector(args.db)

    if args.date:
        date = args.date
    else:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT MAX(date) as d FROM xgt_daily_summary").fetchone()
        date = row['d'] if row else None
        conn.close()

    if not date:
        print("无法获取最新交易日")
        exit(1)

    print(f"分析日期: {date}\n")
    dragons = detector.detect_dragons(date)
    report = detector.format_dragon_report(dragons, date)
    print(report)
