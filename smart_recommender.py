"""
smart_recommender.py - 智能个股推荐引擎
========================================
基于市场周期阶段、概念热度、连板梯队、封板质量等多维度分析，
对涨停池 + 炸板池中的个股进行评分，输出带推荐理由和胜率估计的推荐列表。
市场周期统一使用 CycleModel 的4阶段：冰点酝酿期、蓄力爬升期、爆发高潮期、崩塌退潮期

主要功能:
  - analyze_current_market(db)   分析当前市场状态（统一使用 CycleModel）
  - score_stock(stock_data, market_state)  个股评分（0-100）
  - generate_recommendations(db, top_n=5)  生成推荐列表
  - recommend_for_next_day(db)   次日策略推荐

用法:
  python smart_recommender.py                    # 生成当前推荐
  python smart_recommender.py --date 2026-07-29  # 指定日期推荐
"""

import sqlite3
import json
import os
import sys
import logging
import argparse
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from typing import Dict, List, Any, Optional, Tuple

from config import DB_PATH
from predictor import SentimentStateEngine   # 新增导入，用于统一周期判断
from db import Database                     # 用于创建 Database 实例

# 真实连板计算器（修正API limit_up_days不可靠问题）
try:
    from board_calculator import BoardCalculator
    _board_calc = BoardCalculator()
except Exception as e:
    _board_calc = None
    logging.getLogger('smart_recommender').warning(f"BoardCalculator加载失败: {e}")

# 整体量价走势分析引擎（筛选/进场首要依据）
try:
    from volume_price_analyzer import (
        analyze_stock_volume_price, analyze_market_volume_price, load_stock_history
    )
    _HAS_VP_ANALYZER = True
except Exception as e:
    _HAS_VP_ANALYZER = False
    logging.getLogger('smart_recommender').warning(f"VolumePriceAnalyzer加载失败: {e}")

# ─────────────────────────── 日志配置 ───────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('smart_recommender')

# 评分维度权重（初始值，可被 self_upgrader 动态调整）
# 确定性优先（2026-08-30调整）：整体量价走势提升为筛选/进场首要依据——
# 高位缩量一字、量能骤断、高位巨量、尾盘偷袭、炸板不回封等量价恶化形态，
# 是打板/接力的核心亏损来源，故量价为第一权重+硬性闸门（fail一票否决）。
DEFAULT_WEIGHTS = {
    'volume_price':   0.30,  # 量价走势（首要依据：量能阶梯/换手轨迹/封单量能配合/分歧节奏）
    'seal_quality':   0.24,  # 封板质量
    'dragon_bonus':   0.14,  # 龙头确定性加成（来自dragon_detector）
    'board_position': 0.16,  # 板级位置
    'concept_heat':   0.08,
    'cap_fit':        0.08,
}

# 龙头确定性等级 → 推荐胜率基准映射
DRAGON_CERTAINTY_WINRATE = {
    'SS': 0.88,  # SS级龙头，极高确定性
    'S':  0.78,  # S级龙头，高确定性
    'A':  0.68,  # A级龙头，较高确定性
    'B':  0.55,  # B级龙头，中等确定性
}

# 龙头类型对评分的加成系数
DRAGON_TYPE_SCORE_BONUS = {
    'total_dragon':     15,  # 总龙头：最大加成
    'sector_dragon':    10,  # 板块龙：显著加成
    'switch_dragon':     8,  # 切换龙：加成
    'catch_up_dragon':   5,  # 补涨龙：小幅加成
}

# ─────────────────────────── 信心等级定义 ───────────────────────────
CONFIDENCE_LEVELS = {
    'SS': {
        'name': 'SS级-确定性极高',
        'condition': '板级>=4 且 封单比>=8% 且 零开板',
        'min_win_rate': 0.92,
        'filter': lambda stock: (stock.get('limit_up_days', 1) >= 4 and
                                  (stock.get('seal_ratio', 0) or 0) >= 0.08 and
                                  (stock.get('break_times', 0) or 0) == 0),
        'condition_desc': lambda stock: f"{stock.get('limit_up_days',1)}板+封单比{(stock.get('seal_ratio',0) or 0):.1%}+零开板",
    },
    'S': {
        'name': 'S级-确定性极高',
        'condition': '板级>=3 且 封单比>=5%',
        'min_win_rate': 0.90,
        'filter': lambda stock: (stock.get('limit_up_days', 1) >= 3 and
                                  (stock.get('seal_ratio', 0) or 0) >= 0.05),
        'condition_desc': lambda stock: f"{stock.get('limit_up_days',1)}板+封单比{(stock.get('seal_ratio',0) or 0):.1%}",
    },
    'A': {
        'name': 'A级-确定性高',
        'condition': '板级>=2 且 封单比>=5%',
        'min_win_rate': 0.85,
        'filter': lambda stock: (stock.get('limit_up_days', 1) >= 2 and
                                  (stock.get('seal_ratio', 0) or 0) >= 0.05),
        'condition_desc': lambda stock: f"{stock.get('limit_up_days',1)}板+封单比{(stock.get('seal_ratio',0) or 0):.1%}",
    },
    'B': {
        'name': 'B级-较高确定性',
        'condition': '板级>=2 且 封单比>=3%',
        'min_win_rate': 0.60,
        'filter': lambda stock: (stock.get('limit_up_days', 1) >= 2 and
                                  (stock.get('seal_ratio', 0) or 0) >= 0.03),
        'condition_desc': lambda stock: f"{stock.get('limit_up_days',1)}板+封单比{(stock.get('seal_ratio',0) or 0):.1%}",
    },
    'C': {
        'name': 'C级-中等确定性',
        'condition': '龙头股(最高板) 且 封单比>=3%',
        'min_win_rate': 0.50,
        'needs_max_board': True,
        'filter': lambda stock, max_board: (stock.get('limit_up_days', 1) == max_board and
                                             (stock.get('seal_ratio', 0) or 0) >= 0.03),
        'condition_desc': lambda stock: f"龙头{stock.get('limit_up_days',1)}板+封单比{(stock.get('seal_ratio',0) or 0):.1%}",
    },
}

CONFIDENCE_PRIORITY = ['SS', 'S', 'A', 'B', 'C']

# 周期阶段与市值偏好映射（仅保留 CycleModel 的4阶段）
CYCLE_CAP_PREFERENCE = {
    '冰点酝酿期': 'small',
    '蓄力爬升期': 'small',
    '爆发高潮期': 'large',
    '崩塌退潮期': 'small',
}

BOARD_PROMOTION_BASELINE = {
    1: 0.30,
    2: 0.45,
    3: 0.40,
    4: 0.30,
    5: 0.20,
    6: 0.15,
    7: 0.10,
}

# ─────────────────────────── 数据库工具 ───────────────────────────

def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_new_tables(db_path: str = DB_PATH):
    """确保推荐系统辅助表存在（db.py统一创建，这里做安全检查）"""
    conn = get_conn(db_path)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS recommendation_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rec_date TEXT NOT NULL,
        target_date TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        score REAL,
        reason TEXT,
        win_rate_estimate REAL,
        suggested_action TEXT,
        actual_result TEXT,
        actual_return REAL,
        is_correct INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS weight_adjustment_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        adjust_date TEXT NOT NULL,
        dimension TEXT NOT NULL,
        old_weight REAL,
        new_weight REAL,
        reason TEXT,
        accuracy_before REAL,
        accuracy_after REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS signal_weights (
        signal_id INTEGER PRIMARY KEY,
        weight REAL DEFAULT 1.0,
        trigger_threshold REAL DEFAULT 1.0,
        consecutive_success INTEGER DEFAULT 0,
        consecutive_failure INTEGER DEFAULT 0,
        total_triggers INTEGER DEFAULT 0,
        total_correct INTEGER DEFAULT 0,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()
    logger.info("推荐系统辅助表检查完成")

def get_latest_date(db_path: str = DB_PATH, table: str = 'xgt_limit_up_detail') -> Optional[str]:
    conn = get_conn(db_path)
    try:
        row = conn.execute(f"SELECT MAX(date) as d FROM {table}").fetchone()
        return row['d'] if row and row['d'] else None
    finally:
        conn.close()

def get_limit_up_stocks(date: str, db_path: str = DB_PATH) -> List[Dict]:
    conn = get_conn(db_path)
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
            # 数据清洗：换手率异常值修正（数据源8/6出现3272%等异常值，正常不超过100%）
            tr = stock.get('turnover_rate')
            if tr is not None and (tr > 1.0 or tr < 0):
                stock['turnover_rate'] = 0.15  # 异常值用默认中性换手代替
            result.append(stock)
        return result
    finally:
        conn.close()

def get_break_limit_up_stocks(date: str, db_path: str = DB_PATH) -> List[Dict]:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("""
            SELECT code, name, change_percent, limit_up_days, break_times, concept
            FROM xgt_break_limit_up
            WHERE date = ?
        """, (date,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def get_daily_summary(date: str, db_path: str = DB_PATH) -> Optional[Dict]:
    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT date, limit_up_count, limit_down_count, break_limit_up_count,
                   rise_count, fall_count, explosion_rate, rise_fall_ratio,
                   market_heat, max_continuous_boards, board_distribution
            FROM xgt_daily_summary WHERE date = ?
        """, (date,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

# ==================== 修改：get_smash_coefficient 优先返回当天有效值 ====================
def get_smash_coefficient(date: str, db_path: str = DB_PATH) -> Optional[float]:
    conn = get_conn(db_path)
    try:
        # 先查当天
        row = conn.execute("""
            SELECT smash_coefficient FROM smash_coefficients
            WHERE trade_date = ?
        """, (date,)).fetchone()
        if row and row['smash_coefficient'] is not None:
            return row['smash_coefficient']
        # 若无当天有效值，取最近一天的有效值（非 NULL）
        row = conn.execute("""
            SELECT smash_coefficient FROM smash_coefficients
            WHERE trade_date < ? AND smash_coefficient IS NOT NULL
            ORDER BY trade_date DESC LIMIT 1
        """, (date,)).fetchone()
        return row['smash_coefficient'] if row else None
    finally:
        conn.close()
# =======================================================================

def get_dragon_detections(date: str, db_path: str = DB_PATH) -> Dict[str, Dict]:
    """
    从 dragon_detections 表获取当日龙头识别结果。
    返回 {code: dragon_info} 字典，供推荐引擎融合使用。
    """
    conn = get_conn(db_path)
    try:
        rows = conn.execute("""
            SELECT code, name, dragon_type, certainty_level, total_score,
                   lifecycle_stage, concept, limit_up_days, seal_ratio,
                   board_position_score, seal_resolution_score,
                   sector_leadership_score, market_recognition_score,
                   concept_purity_score, counter_trend_score, reasons, risks
            FROM dragon_detections
            WHERE detect_date = ?
        """, (date,)).fetchall()
        result = {}
        for r in rows:
            d = dict(r)
            # 解析JSON字段
            for field in ('reasons', 'risks'):
                val = d.get(field)
                if val and isinstance(val, str):
                    try:
                        d[field] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        d[field] = []
            result[d['code']] = d
        return result
    except Exception as e:
        logger.warning(f"获取龙头识别结果失败: {e}")
        return {}
    finally:
        conn.close()


def get_capital_flow(date: str, db_path: str = DB_PATH) -> Optional[Dict]:
    """
    从 capital_flow_analysis 表获取当日资金流分析结果。
    """
    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT composite_score, composite_level, guidance, position_multiplier,
                   attack_score, attack_level, persistence_score, persistence_level,
                   rotation_score, rotation_pattern, combo_signals
            FROM capital_flow_analysis
            WHERE date = ?
        """, (date,)).fetchone()
        if not row:
            return None
        result = dict(row)
        if result.get('combo_signals'):
            try:
                result['combo_signals'] = json.loads(result['combo_signals'])
            except (json.JSONDecodeError, TypeError):
                result['combo_signals'] = []
        return result
    except Exception as e:
        logger.warning(f"获取资金流分析结果失败: {e}")
        return None
    finally:
        conn.close()


def get_concept_statistics(date: str, db_path: str = DB_PATH) -> List[Dict]:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("""
            SELECT concept, count FROM concept_statistics
            WHERE date = ? ORDER BY count DESC
        """, (date,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def get_recent_concepts(days: int = 5, db_path: str = DB_PATH) -> Dict[str, int]:
    conn = get_conn(db_path)
    try:
        dates = conn.execute("""
            SELECT DISTINCT date FROM concept_statistics
            ORDER BY date DESC LIMIT ?
        """, (days,)).fetchall()
        if not dates:
            return {}
        date_list = [r['date'] for r in dates]
        placeholders = ','.join(['?' for _ in date_list])
        rows = conn.execute(f"""
            SELECT concept, SUM(count) as total_count
            FROM concept_statistics
            WHERE date IN ({placeholders})
            GROUP BY concept ORDER BY total_count DESC
        """, date_list).fetchall()
        return {r['concept']: r['total_count'] for r in rows}
    finally:
        conn.close()

_promotion_rate_cache: Dict[int, float] = {}

def _compute_all_promotion_rates(db_path: str = DB_PATH) -> Dict[int, float]:
    """
    计算各板级次日晋级率。
    使用A股市场经验基准值，因为数据源 limit_up_days 字段存在严重质量问题：
    - 1月回填数据每天重复同样的5只4板股，污染统计
    - 大量连续涨停股被标记为1板（光迅科技连续8天涨停均标1板）
    - 4板股次日大量标记为4板（应递增为5板），板数字段不连续递增
    实际统计无法可靠区分"首板晋级"和"连续涨停中"，故使用经验基准。
    贝叶斯平滑仍会结合实际数据微调，但权重极低。
    """
    global _promotion_rate_cache
    if _promotion_rate_cache:
        return _promotion_rate_cache
    conn = get_conn(db_path)
    try:
        # 经验基准：A股涨停次日晋级率（基于游资复盘统计）
        EMPIRICAL_BASELINE = {
            1: 0.15,  # 首板→2板约15%
            2: 0.28,  # 2板→3板约28%
            3: 0.35,  # 3板→4板约35%
            4: 0.30,  # 4板→5板约30%
            5: 0.25,  # 5板→6板约25%
            6: 0.20,  # 6板→7板约20%
            7: 0.15,  # 7板+约15%
        }
        dates_rows = conn.execute("""
            SELECT DISTINCT date FROM xgt_limit_up_detail
            WHERE date NOT LIKE '2026-01-%'
            ORDER BY date
        """).fetchall()
        date_list = [r['date'] for r in dates_rows]
        if len(date_list) < 2:
            _promotion_rate_cache = dict(EMPIRICAL_BASELINE)
            return _promotion_rate_cache
        date_to_next = {}
        for i in range(len(date_list) - 1):
            date_to_next[date_list[i]] = date_list[i + 1]
        all_data = conn.execute("""
            SELECT date, code, limit_up_days FROM xgt_limit_up_detail
            WHERE date NOT LIKE '2026-01-%'
        """).fetchall()
        data_map = {}
        for r in all_data:
            data_map[(r['date'], r['code'])] = r['limit_up_days']
        level_stats = defaultdict(lambda: {'total': 0, 'promoted': 0})
        for r in all_data:
            d, code, boards = r['date'], r['code'], r['limit_up_days']
            if boards is None or boards < 1:
                continue
            next_date = date_to_next.get(d)
            if not next_date:
                continue
            level_stats[boards]['total'] += 1
            if (next_date, code) in data_map:
                next_boards = data_map[(next_date, code)]
                if next_boards and next_boards > boards:
                    level_stats[boards]['promoted'] += 1
        # 以经验基准为先验，实际数据权重极低（PRIOR_WEIGHT=200）
        PRIOR_WEIGHT = 200
        for level in range(1, 8):
            prior = EMPIRICAL_BASELINE.get(level, 0.15)
            stats = level_stats.get(level, {'total': 0, 'promoted': 0})
            total = stats['total']
            promoted = stats['promoted']
            if total >= 10:
                smoothed = (promoted + prior * PRIOR_WEIGHT) / (total + PRIOR_WEIGHT)
            else:
                smoothed = prior
            _promotion_rate_cache[level] = round(smoothed, 4)
        return _promotion_rate_cache
    except Exception as e:
        logger.warning(f"晋级率计算失败: {e}, 使用基准值")
        _promotion_rate_cache = dict(BOARD_PROMOTION_BASELINE)
        return _promotion_rate_cache
    finally:
        conn.close()

def get_historical_promotion_rate(board_level: int, db_path: str = DB_PATH) -> float:
    rates = _compute_all_promotion_rates(db_path)
    return rates.get(board_level, BOARD_PROMOTION_BASELINE.get(board_level, 0.25))

def get_model_weights(db_path: str = DB_PATH) -> Dict[str, float]:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT factor_name, weight, credibility FROM model_weights").fetchall()
        return {r['factor_name']: {'weight': r['weight'], 'credibility': r['credibility']} for r in rows}
    finally:
        conn.close()

# ─────────────────────────── 市场分析（统一使用 SentimentStateEngine） ───────────────────────────

def analyze_current_market(date: str, db_path: str = DB_PATH) -> Dict[str, Any]:
    """
    分析当前市场状态 - 统一使用 SentimentStateEngine 获取周期
    """
    from cycle_model import CycleModel  # 保留备用

    summary = get_daily_summary(date, db_path)
    smash = get_smash_coefficient(date, db_path)  # 使用修改后的函数
    concepts = get_concept_statistics(date, db_path)

    smash_trend = 'stable'
    if smash is not None:
        conn = get_conn(db_path)
        try:
            prev_rows = conn.execute("""
                SELECT trade_date, smash_coefficient FROM smash_coefficients
                WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 3
            """, (date,)).fetchall()
            if len(prev_rows) >= 2:
                curr = prev_rows[0]['smash_coefficient']
                prev = prev_rows[-1]['smash_coefficient']
                if curr - prev > 1.0:
                    smash_trend = 'rising'
                elif prev - curr > 1.0:
                    smash_trend = 'falling'
        finally:
            conn.close()

    explosion_rate = 0.0
    explosion_trend = 'stable'
    if summary:
        explosion_rate = summary.get('explosion_rate', 0.0) or 0.0
        conn = get_conn(db_path)
        try:
            prev_rows = conn.execute("""
                SELECT date, explosion_rate FROM xgt_daily_summary
                WHERE date <= ? ORDER BY date DESC LIMIT 3
            """, (date,)).fetchall()
            if len(prev_rows) >= 2:
                curr_er = prev_rows[0]['explosion_rate'] or 0
                prev_er = prev_rows[-1]['explosion_rate'] or 0
                if curr_er - prev_er > 0.05:
                    explosion_trend = 'rising'
                elif prev_er - curr_er > 0.05:
                    explosion_trend = 'falling'
        finally:
            conn.close()

    hot_concepts = concepts[:5] if concepts else []
    recent_concepts = get_recent_concepts(5, db_path)

    board_dist = {}
    if summary and summary.get('board_distribution'):
        try:
            board_dist = json.loads(summary['board_distribution'])
        except (json.JSONDecodeError, TypeError):
            pass

    max_boards = summary.get('max_continuous_boards', 0) if summary else 0
    limit_up_count = summary.get('limit_up_count', 0) if summary else 0

    # ==================== 统一使用 SentimentStateEngine 获取周期阶段 ====================
    cycle_phase = '蓄力爬升期'  # 默认
    try:
        # 创建 Database 实例（用于 SentimentStateEngine）
        db_obj = Database(db_path)
        state_engine = SentimentStateEngine(db_obj)
        state_info = state_engine.infer_state(date)
        state_eng = state_info.get('state', 'MAIN_RISE')
        # 映射英文状态到中文（CycleModel 的4阶段）
        phase_map = {
            'ICEPOINT': '冰点酝酿期',
            'STARTUP': '蓄力爬升期',
            'MAIN_RISE': '蓄力爬升期',  # 近似
            'CLIMAX': '爆发高潮期',
            'EBB': '崩塌退潮期',
        }
        cycle_phase = phase_map.get(state_eng, '蓄力爬升期')
        logger.info(f"[周期] 使用 SentimentStateEngine 推断: {state_eng} -> {cycle_phase}")
        db_obj.close()  # 关闭连接
    except Exception as e:
        logger.warning(f"SentimentStateEngine 调用失败，使用备用方法: {e}")
        # 备用方法：基于砸盘系数和涨停数简单判断（原逻辑）
        if smash is not None:
            if smash >= 7.0:
                cycle_phase = '崩塌退潮期'
            elif smash >= 4.5:
                cycle_phase = '爆发高潮期'
            elif smash >= 3.0:
                cycle_phase = '蓄力爬升期'
            elif smash >= 1.5:
                cycle_phase = '蓄力爬升期'
            else:
                cycle_phase = '冰点酝酿期'
        else:
            lu = summary.get('limit_up_count', 50) if summary else 50
            if lu >= 80:
                cycle_phase = '爆发高潮期'
            elif lu >= 60:
                cycle_phase = '蓄力爬升期'
            elif lu >= 40:
                cycle_phase = '蓄力爬升期'
            else:
                cycle_phase = '冰点酝酿期'
        logger.info(f"[周期] 备用方法推断: {cycle_phase}")
    # ===============================================================================

    # 市值偏好映射（仅4阶段）
    cap_pref = CYCLE_CAP_PREFERENCE.get(cycle_phase, 'medium')

    sentiment = 'neutral'
    if limit_up_count >= 70 and explosion_rate < 0.20:
        sentiment = 'bullish'
    elif limit_up_count < 40 or explosion_rate > 0.35:
        sentiment = 'bearish'

    # 获取龙头识别结果和资金流分析（如果已运行）
    dragon_map = get_dragon_detections(date, db_path)
    capital_flow = get_capital_flow(date, db_path)

    result = {
        'date': date,
        'cycle_phase': cycle_phase,
        'smash_coefficient': smash,
        'smash_trend': smash_trend,
        'explosion_rate': explosion_rate,
        'explosion_trend': explosion_trend,
        'hot_concepts_top5': [c['concept'] for c in hot_concepts],
        'hot_concepts_detail': hot_concepts,
        'recent_concept_heat': recent_concepts,
        'board_distribution': board_dist,
        'max_boards': max_boards,
        'limit_up_count': limit_up_count,
        'market_heat': summary.get('market_heat', 0) if summary else 0,
        'cap_preference': cap_pref,
        'sentiment': sentiment,
        'limit_down_count': summary.get('limit_down_count', 0) if summary else 0,
        'rise_fall_ratio': summary.get('rise_fall_ratio', 1.0) if summary else 1.0,
        'dragon_map': dragon_map,
        'capital_flow': capital_flow,
    }
    # 资金流结果对市场情绪的修正
    if capital_flow:
        cf_level = capital_flow.get('composite_level', '')
        cf_score = capital_flow.get('composite_score', 50)
        if cf_level == 'aggressive' and sentiment != 'bearish':
            result['sentiment'] = 'bullish'
        elif cf_level == 'defensive' and sentiment != 'bullish':
            result['sentiment'] = 'bearish'
        logger.info(f"[资金流] 综合{cf_score:.1f}({cf_level}), "
                    f"仓位系数x{capital_flow.get('position_multiplier', 1.0)}, "
                    f"情绪修正为{result['sentiment']}")

    # ── 整体量价走势环境（首要依据：涨停趋势/炸板率/跌停/砸盘/平均量比）──
    if _HAS_VP_ANALYZER:
        try:
            vp_market = analyze_market_volume_price(date, db_path)
            result['volume_price_market'] = vp_market
            # 量价闸门为"全场无买点"时，直接收紧情绪与最高等级
            if vp_market.get('gate') == '全场无买点':
                result['sentiment'] = 'bearish'
                logger.info(f"[量价] 市场量价闸门触发：{vp_market.get('state_label')}，全场无买点")
        except Exception as e:
            logger.warning(f"量价市场环境分析失败: {e}")
            result['volume_price_market'] = None
    else:
        result['volume_price_market'] = None

    result['action_advice'] = _get_action_advice(result)

    # ── 市场量价闸门硬收紧（首要依据）：全场无买点时，只留最强龙头等级 ──
    # 补缺口：市场量价引擎判"全场无买点"（砸盘≥6.5/炸板率≥40%/跌停潮）时，
    # 不能仅改情绪标签，必须同步收紧推荐允许等级，否则非龙头候选仍会按A级放出。
    vp_mkt = result.get('volume_price_market')
    if vp_mkt and vp_mkt.get('gate') == '全场无买点':
        result['action_advice'] = _tighten_advice(
            result['action_advice'], 'S',
            f"市场量价闸门触发（{vp_mkt.get('state_label','量价恶化')}）：全场无买点，仅S级及以上最强龙头")
    return result

_LEVEL_ORDER = {'SS': 0, 'S': 1, 'A': 2, 'B': 3, 'C': 4}

def _tighten_advice(advice: Dict, new_max_level: str, reason: str) -> Dict:
    """将建议收紧到不高于 new_max_level 的等级（只收紧不放松）"""
    cur_max = advice.get('max_confidence', 'A')
    if _LEVEL_ORDER.get(new_max_level, 2) >= _LEVEL_ORDER.get(cur_max, 2):
        return advice  # 新阈值比当前更宽松，保持不变
    allowed = []
    for lv in CONFIDENCE_PRIORITY:
        allowed.append(lv)
        if lv == new_max_level:
            break
    return {
        'max_confidence': new_max_level,
        'advice_text': reason,
        'allowed_levels': allowed,
    }

def _get_action_advice(market_state: Dict) -> Dict[str, str]:
    smash = market_state.get('smash_coefficient')
    explosion_rate = market_state.get('explosion_rate', 0) or 0
    smash_val = smash if smash is not None else 3.5
    # 确定性优先：恶劣市场仅SS/S级，差市场S/A级，温和市场才放到B
    if smash_val > 7.0 or (smash_val > 6.0 and explosion_rate > 0.30):
        max_level = 'S'
        advice_text = (f"当前砸盘系数{smash_val:.1f}（极高）+炸板率{explosion_rate:.0%}，"
                      f"市场风险极大，仅操作S级及以上确定性龙头")
    elif smash_val > 6.0:
        # 高位分歧区（6.0~6.5）：见顶风险大，从A级收紧到S级，宁缺毋滥
        max_level = 'S'
        advice_text = (f"当前砸盘系数{smash_val:.1f}（高位分歧）+炸板率{explosion_rate:.0%}，"
                      f"抛压偏大/见顶风险高，仅建议S级及以上最强龙头，不做跟风")
    elif explosion_rate > 0.40:
        max_level = 'A'
        advice_text = (f"当前炸板率{explosion_rate:.0%}（>40%，极高）+砸盘系数{smash_val:.1f}，"
                      f"市场分歧极大，建议操作A级及以上龙头")
    elif smash_val < 3.0:
        max_level = 'B'
        advice_text = (f"当前砸盘系数{smash_val:.1f}（偏低）+炸板率{explosion_rate:.0%}，"
                      f"市场状态温和，可操作至B级龙头")
    else:
        max_level = 'A'
        advice_text = (f"当前砸盘系数{smash_val:.1f}+炸板率{explosion_rate:.0%}，"
                      f"建议操作A级及以上确定性龙头")
    allowed_levels = []
    for level in CONFIDENCE_PRIORITY:
        allowed_levels.append(level)
        if level == max_level:
            break
    return {
        'max_confidence': max_level,
        'advice_text': advice_text,
        'allowed_levels': allowed_levels,
    }

# ─────────────────────────── 一字板与分歧/一致节奏识别 ───────────────────────────

def _parse_seal_time_minutes(t_str) -> Optional[int]:
    """解析首封时间为分钟数，支持 09:25:00 / 092500 等格式"""
    if not t_str or str(t_str).strip() in ('', 'None'):
        return None
    s = str(t_str).strip()
    try:
        if ':' in s:
            parts = s.split(':')
            return int(parts[0]) * 60 + int(parts[1])
        if len(s) >= 4:
            s = s.zfill(6)
            return int(s[:2]) * 60 + int(s[2:4])
    except (ValueError, IndexError):
        return None
    return None


def is_yizi_limit_up(stock: Dict) -> bool:
    """
    判断当日是否为一字涨停（缩量加速板）。
    可靠判据：集合竞价/开盘5分钟内封死 + 全天0开板（筹码未经过交换检验）。
    注意：换手率字段数据源口径不一致（小数/百分数混用），不作为硬判据。
    """
    if (stock.get('break_times') or 0) != 0:
        return False
    t_min = _parse_seal_time_minutes(stock.get('first_limit_up_time'))
    if t_min is None or t_min > 575:  # 9:35后才封板不算一字
        return False
    return True


def assess_divergence_state(stock: Dict, market_state: Dict) -> Dict[str, Any]:
    """
    评估个股当日"分歧/一致"节奏状态（游资交易节奏核心）：

    - consensus                一致（一字/秒板封死，全天无分歧）
                                 低位(1-3板)：强势可排队；高位(>=4板)：缩量加速陷阱
    - divergence_to_consensus  分歧转一致（盘中开板1-2次后放量回封，封单回流）—— 最佳买点
    - consensus_to_divergence  一致转分歧（开板后封单弱/未能有效回封）—— 卖点/不追
    - high_divergence          高分歧（炸板3次以上）—— 不参与

    市场砸盘系数过高(>=6)或炸板率>=35%时，分歧过大=无买点，整体收紧。
    """
    break_times = stock.get('break_times') or 0
    boards = stock.get('limit_up_days', 1) or 1
    seal_ratio = stock.get('seal_ratio') or 0
    yizi = is_yizi_limit_up(stock)
    smash = market_state.get('smash_coefficient')
    explosion = market_state.get('explosion_rate', 0) or 0

    market_high_div = (smash is not None and smash >= 6.0) or explosion >= 0.35

    if yizi:
        state = 'consensus'
        label = '一致（一字缩量封死）'
        action_hint = 'high_yizi_trap' if boards >= 4 else 'low_yizi_queue'
    elif break_times >= 3:
        state = 'high_divergence'
        label = f'高分歧（全天炸板{break_times}次）'
        action_hint = 'no_buy'
    elif break_times >= 1 and seal_ratio >= 0.02:
        state = 'divergence_to_consensus'
        label = f'分歧转一致（开板{break_times}次后放量回封）'
        action_hint = 'best_buy'
    elif break_times >= 1:
        state = 'consensus_to_divergence'
        label = f'一致转分歧（开板{break_times}次封单偏弱）'
        action_hint = 'sell_or_wait'
    else:
        state = 'consensus'
        label = '一致（早盘封死无分歧）'
        action_hint = 'normal'

    # 高砸盘/高炸板率环境：分歧过大，除最强分歧转一致外全部收紧
    if market_high_div and action_hint in ('best_buy', 'normal', 'low_yizi_queue'):
        if state == 'divergence_to_consensus' and seal_ratio >= 0.05 and boards <= 4:
            action_hint = 'best_buy_cautious'
            label += '；但市场砸盘/炸板率偏高，仅轻仓'
        else:
            action_hint = 'no_buy_high_smash'
            label += '；市场分歧过大（砸盘系数/炸板率高），无买点'

    return {
        'state': state,
        'label': label,
        'action_hint': action_hint,
        'is_yizi': yizi,
        'market_high_divergence': market_high_div,
    }


# ─────────────────────────── 个股评分 ───────────────────────────

def _score_concept_heat(stock: Dict, market_state: Dict) -> Tuple[float, str]:
    concept = (stock.get('concept') or '').strip()
    hot_top5 = market_state.get('hot_concepts_top5', [])
    recent_heat = market_state.get('recent_concept_heat', {})
    score = 30.0
    reason_parts = []
    if concept in hot_top5:
        rank = hot_top5.index(concept)
        rank_score = 40 - rank * 6
        score += rank_score
        reason_parts.append(f"热门概念「{concept}」排名第{rank+1}")
    elif concept in recent_heat:
        heat = recent_heat[concept]
        score += min(heat * 2, 25)
        reason_parts.append(f"近期活跃概念「{concept}」(近5日{heat}次)")
    else:
        reason_parts.append(f"概念「{concept}」非当前热点")
    return min(score, 100), '; '.join(reason_parts)

def _score_board_position(stock: Dict, market_state: Dict) -> Tuple[float, str]:
    boards = stock.get('limit_up_days', 1) or 1
    max_boards = market_state.get('max_boards', 1)
    sentiment = market_state.get('sentiment', 'neutral')
    promo_rate = get_historical_promotion_rate(boards)
    score = 40.0
    reason_parts = []
    if boards >= max_boards and boards >= 3:
        score += 35
        reason_parts.append(f"{boards}连板(最高板梯队)，龙头溢价")
    elif boards >= 3:
        score += 20 + promo_rate * 20
        reason_parts.append(f"{boards}连板，历史晋级率{promo_rate:.0%}")
    elif boards == 2:
        score += 15 + promo_rate * 15
        reason_parts.append(f"2连板，晋级率{promo_rate:.0%}")
    else:
        if sentiment == 'bullish':
            score += 20
            reason_parts.append("首板，市场情绪偏多可参与")
        else:
            score += 10
            reason_parts.append("首板，需关注板块效应")
    return min(score, 100), '; '.join(reason_parts)

def _score_seal_quality(stock: Dict, market_state: Dict) -> Tuple[float, str]:
    seal_ratio = stock.get('seal_ratio') or 0
    turnover = stock.get('turnover_rate') or 0
    break_times = stock.get('break_times') or 0
    first_time = stock.get('first_limit_up_time', '')
    score = 50.0
    reason_parts = []
    if seal_ratio >= 0.05:
        score += 20
        reason_parts.append(f"封单比{seal_ratio:.2%}(强)")
    elif seal_ratio >= 0.02:
        score += 10
        reason_parts.append(f"封单比{seal_ratio:.2%}(中)")
    else:
        score -= 5
        reason_parts.append(f"封单比{seal_ratio:.2%}(弱)")
    boards_sq = stock.get('limit_up_days', 1) or 1
    if 0.03 <= turnover <= 0.15:
        score += 15
        reason_parts.append(f"换手{turnover:.1%}(健康)")
    elif turnover > 0.15:
        score += 5
        reason_parts.append(f"换手{turnover:.1%}(偏高)")
    else:
        if boards_sq >= 4 and (stock.get('break_times') or 0) == 0:
            score -= 5
            reason_parts.append(f"换手{turnover:.1%}({boards_sq}板缩量一字，筹码未交换，开板即承压)")
        else:
            score += 8
            reason_parts.append(f"换手{turnover:.1%}(偏低)")
    boards = stock.get('limit_up_days', 1) or 1
    is_yizi = is_yizi_limit_up(stock)
    if break_times == 0:
        if is_yizi and boards >= 4:
            # 高位一字缩量：未经分歧检验，能买进的一字往往是出货，不给封板强分
            score += 2
            reason_parts.append(f"{boards}板一字封死(高位缩量加速，未经分歧，炸板风险高)")
        else:
            score += 15
            reason_parts.append("零开板")
    elif break_times <= 2:
        score += 5
        reason_parts.append(f"开板{break_times}次(分歧后回封，承接有效)")
    else:
        score -= 10
        reason_parts.append(f"开板{break_times}次(封板不稳)")
    if first_time:
        try:
            h, m, s = [int(x) for x in first_time.split(':')]
            minutes = h * 60 + m
            # 交易时段：9:30=570, 10:00=600, 11:30=690, 13:00=780, 14:00=840, 15:00=900
            if minutes <= 570:
                if boards >= 4:
                    score += 2
                    reason_parts.append(f"{first_time}竞价/秒板({boards}板高位一字，缩量无换手，排板危险)")
                else:
                    score += 15
                    reason_parts.append(f"{first_time}封板(集合竞价/开盘秒板，低位强势)")
            elif minutes <= 600:
                score += 12
                reason_parts.append(f"{first_time}封板(早盘快速封板)")
            elif minutes <= 690:
                score += 8
                reason_parts.append(f"{first_time}封板(上午封板)")
            elif minutes <= 840:
                score += 3
                reason_parts.append(f"{first_time}封板(下午封板)")
            elif minutes <= 930:
                score -= 5
                reason_parts.append(f"{first_time}封板(尾盘封板，偏弱)")
            else:
                score -= 10
                reason_parts.append(f"{first_time}封板(异常时间)")
        except (ValueError, IndexError):
            pass
    return max(0, min(score, 100)), '; '.join(reason_parts)

def _score_cap_fit(stock: Dict, market_state: Dict) -> Tuple[float, str]:
    flow_cap = stock.get('flow_capital') or 0
    cap_pref = market_state.get('cap_preference', 'medium')
    score = 50.0
    reason_parts = []
    if flow_cap <= 0:
        return score, "市值数据缺失"
    if cap_pref == 'small':
        if flow_cap < 30:
            score += 30
            reason_parts.append(f"流通市值{flow_cap:.0f}亿(小盘，适配当前周期)")
        elif flow_cap < 60:
            score += 15
            reason_parts.append(f"流通市值{flow_cap:.0f}亿(中小盘)")
        else:
            score -= 10
            reason_parts.append(f"流通市值{flow_cap:.0f}亿(偏大)")
    elif cap_pref == 'medium':
        if 30 <= flow_cap <= 150:
            score += 30
            reason_parts.append(f"流通市值{flow_cap:.0f}亿(中等，适配当前周期)")
        elif flow_cap < 30:
            score += 10
            reason_parts.append(f"流通市值{flow_cap:.0f}亿(偏小)")
        else:
            score += 10
            reason_parts.append(f"流通市值{flow_cap:.0f}亿(偏大)")
    else:  # large
        if flow_cap >= 100:
            score += 30
            reason_parts.append(f"流通市值{flow_cap:.0f}亿(大盘，适配当前周期)")
        elif flow_cap >= 50:
            score += 15
            reason_parts.append(f"流通市值{flow_cap:.0f}亿(中大盘)")
        else:
            score -= 10
            reason_parts.append(f"流通市值{flow_cap:.0f}亿(偏小)")
    return min(score, 100), '; '.join(reason_parts)

def _score_volume_price(stock: Dict, market_state: Dict) -> Tuple[float, str]:
    """
    量价走势评分（首要维度）。
    优先使用 volume_price_analyzer 的整体量价形态分析结果（在
    generate_recommendations 中批量预计算并挂到 stock['_vp']）；
    无预计算结果时走轻量兜底（量比+换手），保证函数可独立调用。
    """
    vp = stock.get('_vp')
    if vp is not None:
        reason_parts = [f"量价形态：{vp.get('pattern', '未知')}（{vp.get('action_gate', '')}）"]
        reason_parts.extend(vp.get('signals', [])[:3])
        if vp.get('risks'):
            reason_parts.append("⚠️" + "；".join(vp.get('risks', [])[:2]))
        return float(vp.get('score', 50)), '；'.join(reason_parts)

    # ── 兜底：无引擎/无预计算时的轻量评分 ──
    volume_bias = stock.get('volume_bias') or 1.0
    turnover = stock.get('turnover_rate') or 0
    score = 50.0
    reason_parts = []
    if 1.5 <= volume_bias <= 3.0:
        score += 25
        reason_parts.append(f"量比{volume_bias:.2f}(温和放量)")
    elif 1.0 <= volume_bias < 1.5:
        score += 15
        reason_parts.append(f"量比{volume_bias:.2f}(适度)")
    elif 3.0 < volume_bias <= 5.0:
        score += 5
        reason_parts.append(f"量比{volume_bias:.2f}(放量偏大)")
    elif volume_bias > 5.0:
        score -= 10
        reason_parts.append(f"量比{volume_bias:.2f}(过度放量，分歧大)")
    else:
        score += 5
        reason_parts.append(f"量比{volume_bias:.2f}(缩量)")
    if 0.05 <= turnover <= 0.20:
        score += 20
        reason_parts.append(f"换手{turnover:.1%}(活跃)")
    elif turnover > 0.20:
        score += 5
        reason_parts.append(f"换手{turnover:.1%}(过高)")
    else:
        score += 10
        reason_parts.append(f"换手{turnover:.1%}(偏低)")
    return min(score, 100), '; '.join(reason_parts)

def _score_dragon_bonus(stock: Dict, market_state: Dict) -> Tuple[float, str]:
    """
    龙头确定性加成维度：基于 dragon_detector 的识别结果。
    这是"确定性优先"原则的核心——被龙头识别系统确认的标的应获得显著加分。
    """
    code = stock.get('code', '')
    dragon_map = market_state.get('dragon_map', {})
    dragon = dragon_map.get(code)

    if not dragon:
        return 50.0, "未进入龙头候选"

    certainty = dragon.get('certainty_level', 'B')
    dragon_type = dragon.get('dragon_type', '')
    lifecycle = dragon.get('lifecycle_stage', '')
    dragon_score = dragon.get('total_score', 50)

    # 基础分：龙头识别总分映射到60-95区间
    base = 60 + (dragon_score - 50) * 0.7  # 50分→60, 70分→74, 90分→88
    base = min(base, 95)

    # 确定性等级加成
    certainty_bonus = {'SS': 10, 'S': 8, 'A': 5, 'B': 2}.get(certainty, 0)

    # 龙头类型加成
    type_bonus = DRAGON_TYPE_SCORE_BONUS.get(dragon_type, 0)

    # 生命周期调整
    lifecycle_adj = 0
    if lifecycle == 'acceleration':
        lifecycle_adj = 5  # 加速期最佳
    elif lifecycle == 'launch':
        lifecycle_adj = 2
    elif lifecycle == 'climax':
        lifecycle_adj = -5  # 高潮期风险
    elif lifecycle == 'decline':
        lifecycle_adj = -20  # 衰退期大幅降分

    final = base + certainty_bonus + type_bonus + lifecycle_adj
    final = max(0, min(final, 100))

    type_names = {
        'total_dragon': '总龙头', 'sector_dragon': '板块龙',
        'switch_dragon': '切换龙', 'catch_up_dragon': '补涨龙'
    }
    lifecycle_names = {
        'launch': '启动期', 'acceleration': '加速期',
        'climax': '高潮期', 'decline': '衰退期'
    }
    reason = (f"🏆{certainty}级{type_names.get(dragon_type, '龙头')}"
              f"({lifecycle_names.get(lifecycle, '')}), "
              f"龙头评分{dragon_score:.1f}")
    return final, reason

def score_stock(stock: Dict, market_state: Dict,
                weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    w = weights or DEFAULT_WEIGHTS
    c_score, c_reason = _score_concept_heat(stock, market_state)
    b_score, b_reason = _score_board_position(stock, market_state)
    s_score, s_reason = _score_seal_quality(stock, market_state)
    m_score, m_reason = _score_cap_fit(stock, market_state)
    v_score, v_reason = _score_volume_price(stock, market_state)
    d_score, d_reason = _score_dragon_bonus(stock, market_state)
    total = (
        c_score * w.get('concept_heat', 0.10) +
        b_score * w.get('board_position', 0.25) +
        s_score * w.get('seal_quality', 0.40) +
        m_score * w.get('cap_fit', 0.10) +
        v_score * w.get('volume_price', 0.10) +
        d_score * w.get('dragon_bonus', 0.05)
    )
    is_broken = stock.get('_from_break_pool', False)
    if is_broken:
        total *= 0.80
    sentiment = market_state.get('sentiment', 'neutral')
    if sentiment == 'bearish':
        total *= 0.90
    elif sentiment == 'bullish':
        total *= 1.05

    # 资金流仓位系数对评分的微调（不超过±5分，胜率第一原则）
    capital_flow = market_state.get('capital_flow')
    if capital_flow:
        cf_mult = capital_flow.get('position_multiplier', 1.0)
        if cf_mult >= 1.3:
            total = min(100, total + 2)  # 强资金面小幅加分
        elif cf_mult <= 0.5:
            total = max(0, total - 3)  # 弱资金面小幅减分

    # ── 分歧/一致节奏调整（交易节奏核心，基于真实可成交性）──
    divergence = assess_divergence_state(stock, market_state)
    hint = divergence['action_hint']
    is_high_yizi = divergence['is_yizi'] and (stock.get('limit_up_days', 1) or 1) >= 4
    if hint == 'best_buy':
        total += 6   # 分歧转一致回封：经分歧检验的买点，溢价高
    elif hint == 'best_buy_cautious':
        total += 2
    elif hint == 'high_yizi_trap':
        total -= 18  # 高位一字：排到即接盘
    elif hint == 'no_buy':
        total -= 15  # 高分歧炸板
    elif hint == 'sell_or_wait':
        total -= 8
    elif hint == 'no_buy_high_smash':
        total -= 10  # 砸盘系数过高，无买点
    # low_yizi_queue / normal / consensus 不调整

    # ── 量价走势闸门（首要依据：fail 一票否决，caution 降级）──
    vp = stock.get('_vp')
    vp_grade = None
    vp_pattern = None
    vp_gate = None
    vp_veto = []
    if vp is not None:
        vp_grade = vp.get('grade')
        vp_pattern = vp.get('pattern')
        vp_gate = vp.get('action_gate')
        vp_veto = vp.get('veto_reasons', [])
        if vp_grade == 'fail':
            total -= 25  # 量价结构恶化：一票否决（最终过滤阶段会直接剔除）
        elif vp_grade == 'caution':
            total -= 6   # 量价存疑：降权，仍可保留观察
        elif vp_grade == 'pass':
            total += 3   # 量价健康：小幅加分

    total = round(min(max(total, 0), 100), 1)
    action = _suggest_action(total, stock, market_state, divergence)
    risks = _generate_risks(stock, market_state, total, divergence)

    # 获取龙头信息（用于结果输出）
    code = stock.get('code', '')
    dragon_info = market_state.get('dragon_map', {}).get(code)

    return {
        'code': stock.get('code', ''),
        'name': stock.get('name', ''),
        'total_score': total,
        'dimension_scores': {
            'concept_heat': round(c_score, 1),
            'board_position': round(b_score, 1),
            'seal_quality': round(s_score, 1),
            'cap_fit': round(m_score, 1),
            'volume_price': round(v_score, 1),
            'dragon_bonus': round(d_score, 1),
        },
        'dimension_reasons': {
            'concept_heat': c_reason,
            'board_position': b_reason,
            'seal_quality': s_reason,
            'cap_fit': m_reason,
            'volume_price': v_reason,
            'dragon_bonus': d_reason,
        },
        'dragon_info': {
            'certainty_level': dragon_info.get('certainty_level') if dragon_info else None,
            'dragon_type': dragon_info.get('dragon_type') if dragon_info else None,
            'lifecycle_stage': dragon_info.get('lifecycle_stage') if dragon_info else None,
            'dragon_score': dragon_info.get('total_score') if dragon_info else None,
        } if dragon_info else None,
        'suggested_action': action,
        'risk_notes': risks,
        'concept': stock.get('concept', ''),
        'limit_up_days': stock.get('limit_up_days', 1),
        'seal_ratio': stock.get('seal_ratio', 0),
        'is_yizi': divergence['is_yizi'],
        'divergence_state': divergence['state'],
        'divergence_label': divergence['label'],
        'vp_grade': vp_grade,
        'vp_pattern': vp_pattern,
        'vp_gate': vp_gate,
        'vp_veto': vp_veto,
        'vp_score': round(vp['score'], 1) if vp is not None else None,
    }

def _suggest_action(score: float, stock: Dict, market_state: Dict,
                    divergence: Optional[Dict] = None) -> str:
    boards = stock.get('limit_up_days', 1) or 1
    is_broken = stock.get('_from_break_pool', False)
    phase = market_state.get('cycle_phase', '蓄力爬升期')
    code = stock.get('code', '')
    dragon_map = market_state.get('dragon_map', {})
    dragon = dragon_map.get(code)

    if divergence is None:
        divergence = assess_divergence_state(stock, market_state)

    # ── 一字板/分歧节奏硬规则（优先于一切龙头/评分判断）──
    is_total_dragon = bool(dragon and dragon.get('dragon_type') == 'total_dragon')
    if divergence['is_yizi'] and boards >= 4 and not is_total_dragon:
        return '回避(高位一字缩量，排到即接盘，等分歧回封)'
    if divergence['action_hint'] == 'no_buy':
        return '回避(全天高分歧，封板不坚决)'
    if divergence['action_hint'] == 'no_buy_high_smash':
        return '观望(砸盘系数/炸板率过高，分歧过大无买点)'
    if divergence['action_hint'] == 'sell_or_wait':
        return '观望(一致转分歧，封单减弱，是卖点非买点)'
    if divergence['action_hint'] in ('best_buy', 'best_buy_cautious'):
        tag = '轻仓' if divergence['action_hint'] == 'best_buy_cautious' else ''
        return f'{tag}打板/半路(分歧转一致回封，承接有效)'

    # ── 量价走势闸门（首要依据）：量价 fail/caution 的动作收紧 ──
    vp = stock.get('_vp')
    if vp is not None:
        if vp.get('grade') == 'fail':
            return f'回避(量价结构不通过：{vp.get("pattern", "量价恶化")})'
        if vp.get('grade') == 'caution':
            # 量价存疑：只允许轻仓观察，不给打板
            if score < 75:
                return f'观望(量价存疑：{vp.get("pattern", "")}，等确认)'
    if divergence['is_yizi'] and boards < 4:
        if phase == '崩塌退潮期':
            return '观望(退潮期不排一字)'
        return '竞价排队(低位一字，强势但严格设止损)'

    # 龙头标的的动作建议更保守
    if dragon:
        certainty = dragon.get('certainty_level', 'B')
        lifecycle = dragon.get('lifecycle_stage', '')
        if lifecycle == 'decline':
            return '回避(龙头衰退期)'
        if phase == '崩塌退潮期':
            return '观望(退潮期不接力)'
        if certainty in ('SS', 'S') and lifecycle in ('launch', 'acceleration'):
            return '打板(龙头确认)'
        if certainty == 'A' and lifecycle in ('launch', 'acceleration'):
            return '半路(龙头低吸)'
        if lifecycle == 'climax':
            return '观望(龙头高潮期，不追高)'
        return '低吸(龙头回踩确认)'

    # 非龙头的常规建议
    if score >= 80:
        if boards >= 3 and phase == '爆发高潮期':
            return '追涨(龙头确认)'
        return '打板(强势股)'
    elif score >= 65:
        if is_broken:
            return '观望(炸板回封确认)'
        return '低吸(优质标的)'
    elif score >= 50:
        return '观望(等待确认信号)'
    else:
        return '回避(综合评分偏低)'

def _generate_risks(stock: Dict, market_state: Dict, score: float,
                    divergence: Optional[Dict] = None) -> List[str]:
    risks = []
    boards = stock.get('limit_up_days', 1) or 1
    break_times = stock.get('break_times', 0) or 0
    explosion_rate = market_state.get('explosion_rate', 0)
    phase = market_state.get('cycle_phase', '蓄力爬升期')
    smash = market_state.get('smash_coefficient')
    if divergence is None:
        divergence = assess_divergence_state(stock, market_state)

    # ── 交易节奏纪律（用户实盘核心教训：三次一字开板回撤）──
    if divergence['is_yizi'] and boards >= 4:
        dragon = market_state.get('dragon_map', {}).get(stock.get('code', ''))
        is_total = bool(dragon and dragon.get('dragon_type') == 'total_dragon')
        if not is_total:
            risks.append(f"⚠️{boards}板一字缩量：竞价排板能成交往往是资金出货，"
                         f"当日无法有效介入，严禁竞价挂单，等次日分歧放量回封再看")
        else:
            risks.append(f"{boards}板市场总龙头一字：可少量排板但开板放量立即走，不补仓")
    if divergence['state'] == 'divergence_to_consensus':
        risks.append("分歧转一致回封是买点；若次日低开或开板不回封（转分歧），按卖点纪律离场")
    if divergence['state'] == 'consensus_to_divergence':
        risks.append("一致转分歧：封单减弱/开板不回封是卖点，不追不买")
    if divergence['state'] == 'high_divergence':
        risks.append("高分歧（多次炸板）：不具备买点，视为风险")
    if smash is not None and smash >= 6.0:
        risks.append(f"砸盘系数{smash:.1f}过高：分歧过大无买点，空仓或只看最强龙头分歧回封")

    # ── 量价走势风险（首要依据）──
    vp = stock.get('_vp')
    if vp is not None:
        if vp.get('grade') == 'fail':
            for v in vp.get('veto_reasons', [])[:2]:
                risks.append(f"⛔量价否决：{v}")
        elif vp.get('grade') == 'caution':
            for r_ in vp.get('risks', [])[:2]:
                risks.append(f"量价警示：{r_}")
        elif vp.get('signals'):
            risks.append(f"量价：{vp.get('pattern','')}，{vp['signals'][0]}")

    if boards >= 5:
        risks.append(f"已{boards}连板，高位风险较大，注意控制仓位")
    if break_times >= 3:
        risks.append(f"今日开板{break_times}次，封板稳定性存疑")
    if explosion_rate > 0.30:
        risks.append(f"当前炸板率{explosion_rate:.0%}，整体封板成功率偏低")
    if phase in ('崩塌退潮期', '爆发高潮期'):
        risks.append(f"当前处于{phase}，注意周期转换风险")
    if score < 60:
        risks.append("综合评分偏低，建议降低仓位或等待更优机会")
    if not risks:
        risks.append("正常参与，注意设好止损位")
    return risks

def estimate_win_rate(stock_score: Dict, market_state: Dict,
                      db_path: str = DB_PATH) -> float:
    """
    估算个股次日晋级胜率。
    确定性优先原则：
    1. 如果被 dragon_detector 确认为龙头，直接使用龙头确定性等级对应的胜率基准
    2. 否则区分龙头候选（高板+强封单）与跟风股
    3. 资金流分析结果作为乘数修正
    """
    score = stock_score['total_score']
    boards = stock_score.get('limit_up_days', 1)
    phase = market_state.get('cycle_phase', '蓄力爬升期')
    seal_ratio = stock_score.get('seal_ratio', 0) or 0
    code = stock_score.get('code', '')

    # 历史晋级率
    promo = get_historical_promotion_rate(boards, db_path)

    # 优先使用 dragon_detector 的确定性等级
    dragon_map = market_state.get('dragon_map', {})
    dragon_info = dragon_map.get(code)

    if dragon_info:
        certainty = dragon_info.get('certainty_level', 'B')
        dragon_type = dragon_info.get('dragon_type', '')
        lifecycle = dragon_info.get('lifecycle_stage', '')
        dragon_total = dragon_info.get('total_score', 50)

        # 直接以龙头确定性等级为胜率基准
        base_rate = DRAGON_CERTAINTY_WINRATE.get(certainty, 0.55)

        # 龙头总分微调（±5%）
        base_rate += (dragon_total - 70) * 0.002

        # 龙头类型修正
        if dragon_type == 'total_dragon':
            base_rate += 0.03  # 总龙头溢价
        elif dragon_type == 'catch_up_dragon':
            base_rate -= 0.05  # 补涨龙确定性低

        # 生命周期修正
        if lifecycle == 'acceleration':
            base_rate *= 1.05
        elif lifecycle == 'launch':
            base_rate *= 1.00
        elif lifecycle == 'climax':
            base_rate *= 0.85  # 高潮期风险大
        elif lifecycle == 'decline':
            base_rate *= 0.60  # 衰退期大幅降低

        # 混合历史晋级率（5%权重）——龙头确定性等级已是主要依据，
        # 历史晋级率仅作极微调参考，避免数据源板数质量问题拉低龙头胜率
        base_rate = base_rate * 0.95 + promo * 0.05

        is_dragon_candidate = True
        win_cap = 0.90 if certainty in ('SS', 'S') else 0.80
    else:
        # 非龙头：判断是否为龙头候选（高板+强封单）
        max_boards = market_state.get('max_boards', boards)
        is_dragon_candidate = (
            boards >= max_boards or
            (boards >= 3 and seal_ratio >= 0.05)
        )

        if is_dragon_candidate:
            base_rate = 0.50
            base_rate += (score - 60) * 0.006
            base_rate = base_rate * 0.6 + promo * 0.4
        else:
            base_rate = 0.38
            base_rate += (score - 50) * 0.004
            base_rate = base_rate * 0.6 + promo * 0.4

        win_cap = 0.65 if is_dragon_candidate else 0.50

    # 周期调整
    if phase in ('爆发高潮期',):
        base_rate *= 1.10
    elif phase in ('蓄力爬升期',):
        base_rate *= 1.05
    elif phase in ('崩塌退潮期',):
        base_rate *= 0.70
    elif phase in ('冰点酝酿期',):
        # 冰点期砸盘系数低、恐慌释放充分，龙头反而有修复溢价
        base_rate *= 0.95
    else:
        base_rate *= 0.85

    # 封单质量修正
    if seal_ratio >= 0.05:
        base_rate *= 1.05
    elif seal_ratio < 0.01:
        base_rate *= 0.90

    # 资金流修正
    capital_flow = market_state.get('capital_flow')
    if capital_flow:
        cf_level = capital_flow.get('composite_level', '')
        if cf_level == 'aggressive':
            base_rate *= 1.05
        elif cf_level == 'positive':
            base_rate *= 1.02
        elif cf_level == 'cautious':
            base_rate *= 0.92
        elif cf_level == 'defensive':
            base_rate *= 0.80
        # 组合信号：强攻+强持续=黄金窗口
        combo = capital_flow.get('combo_signals', [])
        if combo:
            for sig in combo:
                if isinstance(sig, dict):
                    sig_type = sig.get('type', '')
                    if sig_type == 'golden_window':
                        base_rate *= 1.03
                    elif sig_type == 'one_day_wonder':
                        base_rate *= 0.85
                    elif sig_type == 'danger_zone':
                        base_rate *= 0.75

    # ── 一字板/分歧节奏修正（可成交性决定真实胜率）──
    is_yizi = stock_score.get('is_yizi', False)
    div_state = stock_score.get('divergence_state', '')
    is_total_dragon = bool(dragon_info and dragon_info.get('dragon_type') == 'total_dragon')
    if is_yizi and boards >= 4:
        if not is_total_dragon:
            base_rate *= 0.55   # 非总龙头4板+一字：缩量加速，次日开板概率高
            win_cap = min(win_cap, 0.45)
        else:
            base_rate *= 0.80   # 总龙头高位一字：仍强但开板风险上升
    elif is_yizi and boards < 4:
        base_rate *= 1.02       # 低位一字：正常强势
    if div_state == 'divergence_to_consensus':
        base_rate *= 1.08       # 分歧转一致回封：经分歧检验，次日溢价高
        win_cap = min(0.90, max(win_cap, 0.75))
    elif div_state == 'high_divergence':
        base_rate *= 0.70
    elif div_state == 'consensus_to_divergence':
        base_rate *= 0.80

    # ── 整体量价走势修正（首要依据）──
    vp_grade = stock_score.get('vp_grade')
    if vp_grade == 'fail':
        base_rate *= 0.60
        win_cap = min(win_cap, 0.35)   # 量价恶化：胜率封顶35%
    elif vp_grade == 'caution':
        base_rate *= 0.88
    elif vp_grade == 'pass':
        base_rate *= 1.06              # 量价健康：温和提升
        win_cap = min(0.92, win_cap + 0.02)

    # 龙头胜率兜底：B级以上龙头在非衰退期，胜率不应低于45%
    # （否则与龙头身份矛盾，说明修正因子过度惩罚）
    # 注意：高位一字惩罚后的胜率可以低于兜底（一字风险是实盘硬教训）
    if dragon_info and lifecycle != 'decline' and not (is_yizi and boards >= 4 and not is_total_dragon):
        certainty_floor = {'SS': 0.65, 'S': 0.58, 'A': 0.50, 'B': 0.45}
        floor = certainty_floor.get(certainty, 0.45)
        base_rate = max(base_rate, floor)

    return round(max(0.10, min(base_rate, win_cap)), 2)

# ─────────────────────────── 推荐生成 ───────────────────────────

def generate_recommendations(date: str, top_n: int = 5,
                              db_path: str = DB_PATH) -> List[Dict]:
    init_new_tables(db_path)
    market_state = analyze_current_market(date, db_path)
    action_advice = market_state.get('action_advice', {})
    allowed_levels = action_advice.get('allowed_levels', ['S', 'A'])
    logger.info(f"[{date}] 市场状态: 周期={market_state['cycle_phase']}, "
                f"涨停={market_state['limit_up_count']}, "
                f"炸板率={market_state['explosion_rate']:.1%}, "
                f"砸盘系数={market_state.get('smash_coefficient', 'N/A')}, "
                f"情绪={market_state['sentiment']}")
    logger.info(f"[{date}] 出击建议: {action_advice.get('advice_text', '未知')}, "
                f"允许等级: {allowed_levels}")
    limit_up_stocks = get_limit_up_stocks(date, db_path)
    break_stocks = get_break_limit_up_stocks(date, db_path)
    logger.info(f"涨停池: {len(limit_up_stocks)} 只, 炸板池: {len(break_stocks)} 只")
    for s in limit_up_stocks:
        s['_from_break_pool'] = False
    for s in break_stocks:
        s['_from_break_pool'] = True
        s.setdefault('seal_ratio', 0)
        s.setdefault('turnover_rate', 0)
        s.setdefault('volume_bias', 1.0)
        s.setdefault('flow_capital', 0)
        s.setdefault('first_limit_up_time', '')
        s.setdefault('reason', '')
    all_candidates = limit_up_stocks + break_stocks
    all_candidates = [s for s in all_candidates
                      if s.get('name') and '*ST' not in s['name'] and 'ST' not in s['name']]

    # 用board_calculator覆盖真实连板数（API字段14.6%不匹配）
    if _board_calc is not None and all_candidates:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            for s in all_candidates:
                real = _board_calc.get_consecutive_boards(date, s.get('code', ''), conn)
                if real and real > 0:
                    s['api_boards'] = s.get('limit_up_days', 1)
                    s['limit_up_days'] = real
            # 同时修正市场最高板
            real_max = _board_calc.get_daily_max_boards(date, conn)
            if real_max and real_max > 0:
                market_state['max_boards'] = real_max
            conn.close()
        except Exception as e:
            logger.warning(f"board_calculator覆盖失败: {e}")

    # ── 量价走势预计算（首要依据）：对全部候选用量价引擎批量分析 ──
    # 每只股票挂 _vp 结果；grade=fail 的在后续硬过滤阶段一票否决。
    if _HAS_VP_ANALYZER and all_candidates:
        try:
            vp_market = market_state.get('volume_price_market')
            vp_conn = sqlite3.connect(db_path)
            vp_conn.row_factory = sqlite3.Row
            for s in all_candidates:
                code = s.get('code', '')
                hist = load_stock_history(vp_conn, code, date, days=10)
                dinfo = market_state.get('dragon_map', {}).get(code)
                is_td = bool(dinfo and dinfo.get('dragon_type') == 'total_dragon')
                try:
                    s['_vp'] = analyze_stock_volume_price(
                        s, hist, market_state, is_total_dragon=is_td)
                except Exception as ve:
                    logger.warning(f"量价分析失败 {code}: {ve}")
                    s['_vp'] = None
            vp_conn.close()
            if vp_market:
                logger.info(f"[量价] 市场闸门={vp_market.get('gate')}，"
                            f"状态={vp_market.get('state_label')}，"
                            f"评分={vp_market.get('score')}")
        except Exception as e:
            logger.warning(f"量价预计算失败: {e}")

    # ── 硬过滤：4板及以上仍为一字涨停的标的，除市场总龙头外一律不纳入推荐 ──
    # 逻辑：高位缩量一字=市场一致到极致，筹码未经分歧交换；
    # 竞价/排板能成交往往就是场内资金借高开出货的开板日（实盘三次回撤教训）。
    # 仅 dragon_detector 确认的 total_dragon（市场总龙头）享有豁免，但仍降仓位。
    kept_candidates = []
    excluded_yizi = []
    for s in all_candidates:
        b = s.get('limit_up_days', 1) or 1
        if b >= 4 and is_yizi_limit_up(s):
            dinfo = market_state.get('dragon_map', {}).get(s.get('code', ''))
            is_total = bool(dinfo and dinfo.get('dragon_type') == 'total_dragon')
            if not is_total:
                excluded_yizi.append(f"{s.get('name','')}({b}板一字)")
                continue
        kept_candidates.append(s)
    if excluded_yizi:
        logger.info(f"[一字硬过滤] 排除{len(excluded_yizi)}只4板+一字(非总龙头): "
                    f"{', '.join(excluded_yizi)}")
    all_candidates = kept_candidates

    if not all_candidates:
        logger.info(f"[{date}] 无候选股票（或全部被一字硬过滤），返回空推荐")
        return []
    max_board = max(s.get('limit_up_days', 1) for s in all_candidates)
    weights = _get_current_weights(db_path)
    scored_list = []
    for stock in all_candidates:
        result = score_stock(stock, market_state, weights)
        win_rate = estimate_win_rate(result, market_state, db_path)
        result['win_rate'] = win_rate
        result['rec_date'] = date
        result['reason'] = _build_reason_string(result)
        result['_stock_data'] = stock
        scored_list.append(result)

    # ── 量价一票否决（首要依据）：量价结构 fail 的标的直接剔除，不进入推荐 ──
    vp_failed = []
    kept = []
    for r in scored_list:
        if r.get('vp_grade') == 'fail':
            veto = '；'.join(r.get('vp_veto', [])[:1]) or '量价结构恶化'
            vp_failed.append(f"{r.get('name','')}({r.get('vp_pattern','量价fail')}:{veto[:24]})")
            continue
        kept.append(r)
    if vp_failed:
        logger.info(f"[量价闸门] 一票否决剔除{len(vp_failed)}只: {', '.join(vp_failed[:8])}")
    scored_list = kept

    final_recommendations = _filter_by_confidence_levels(
        scored_list, allowed_levels, max_board, top_n
    )
    for rec in final_recommendations:
        rec.pop('_stock_data', None)
    if final_recommendations:
        _save_recommendations(date, final_recommendations, db_path)
        rec_summary = ', '.join(
            f"{r['name']}({r['confidence_level']}级)" for r in final_recommendations
        )
        logger.info(f"[{date}] 最终推荐 {len(final_recommendations)} 只: {rec_summary}")
    else:
        logger.info(f"[{date}] 当前无符合信心等级的标的，宁缺毋滥，返回空推荐")
    return final_recommendations

def _filter_by_confidence_levels(
    scored_list: List[Dict],
    allowed_levels: List[str],
    max_board: int,
    top_n: int
) -> List[Dict]:
    """
    确定性优先的推荐过滤：
    1. 先按 dragon_detector 确定性等级筛选（SS/S/A/B）
    2. 未被识别为龙头的，按原 CONFIDENCE_LEVELS 条件筛选
    3. 龙头确定性等级优先于传统板级+封单比条件
    4. 非龙头股胜率上限45%，胜率不足40%的不推荐
    """
    final = []
    assigned_codes = set()

    # 第一阶段：按龙头确定性等级排序筛选
    dragon_rank = {'SS': 0, 'S': 1, 'A': 2, 'B': 3}
    dragon_items = []
    for item in scored_list:
        dinfo = item.get('dragon_info')
        if dinfo and dinfo.get('certainty_level'):
            level = dinfo['certainty_level']
            dragon_items.append((dragon_rank.get(level, 9), item))
    dragon_items.sort(key=lambda x: (x[0], -x[1]['total_score']))

    for rank, item in dragon_items:
        if len(final) >= top_n:
            break
        code = item['code']
        if code in assigned_codes:
            continue
        dinfo = item['dragon_info']
        level = dinfo['certainty_level']
        lifecycle = dinfo.get('lifecycle_stage', '')

        # 衰退期龙头不推荐
        if lifecycle == 'decline':
            continue

        # 高位一字硬过滤：4板+一字只有市场总龙头可豁免
        boards = item.get('limit_up_days', 1) or 1
        if item.get('is_yizi') and boards >= 4 and dinfo.get('dragon_type') != 'total_dragon':
            continue

        mapped_level = level
        if mapped_level not in allowed_levels:
            continue
        assigned_codes.add(code)
        item['confidence_level'] = level
        item['confidence_name'] = f"{level}级·龙头确定性"
        item['historical_win_rate'] = DRAGON_CERTAINTY_WINRATE.get(level, 0.55)
        dragon_type_names = {
            'total_dragon': '总龙头', 'sector_dragon': '板块龙',
            'switch_dragon': '切换龙', 'catch_up_dragon': '补涨龙'
        }
        type_name = dragon_type_names.get(dinfo.get('dragon_type', ''), '龙头')
        item['condition_match'] = (
            f"{type_name}/{dinfo.get('lifecycle_stage', '')}/"
            f"龙头评分{dinfo.get('dragon_score', 0):.1f}"
        )
        final.append(item)

    # 第二阶段：对未被龙头选中的，用传统条件补充
    # 注意：传统条件的等级不应高于龙头确定性等级
    # 如果一只股票已是B级龙头，不应因3板+封单5%而被标为S级
    for level in CONFIDENCE_PRIORITY:
        if level not in allowed_levels:
            continue
        if len(final) >= top_n:
            break
        level_config = CONFIDENCE_LEVELS[level]
        filter_fn = level_config['filter']
        needs_max_board = level_config.get('needs_max_board', False)
        matched = []
        for item in scored_list:
            code = item['code']
            if code in assigned_codes:
                continue
            # 如果这只股票已被龙头识别但等级较低，不允许传统条件提升其等级
            dinfo = item.get('dragon_info')
            if dinfo and dinfo.get('certainty_level'):
                dragon_level = dinfo['certainty_level']
                level_order = {'SS': 0, 'S': 1, 'A': 2, 'B': 3, 'C': 4}
                if level_order.get(level, 4) < level_order.get(dragon_level, 3):
                    continue  # 传统条件等级高于龙头等级，跳过
            stock = item['_stock_data']
            # 高位一字非总龙头：不进入传统条件推荐
            if item.get('is_yizi') and (item.get('limit_up_days', 1) or 1) >= 4:
                continue
            try:
                if needs_max_board:
                    is_match = filter_fn(stock, max_board)
                else:
                    is_match = filter_fn(stock)
            except TypeError:
                is_match = False
            if is_match:
                # 非龙头股胜率门槛：低于45%不推荐（跟风票确定性弱于龙头，
                # 需更高bar；量价/砸盘环境恶化时进一步宁缺毋滥）
                win_rate = item.get('win_rate', 0)
                is_dragon = item.get('dragon_info') is not None
                if not is_dragon and win_rate < 0.45:
                    continue
                matched.append((item, stock))
        matched.sort(key=lambda x: x[0]['total_score'], reverse=True)
        for item, stock in matched:
            if len(final) >= top_n:
                break
            code = item['code']
            if code in assigned_codes:
                continue
            assigned_codes.add(code)
            try:
                condition_desc = level_config.get('condition_desc', lambda s: level_config['condition'])(stock)
            except Exception:
                condition_desc = level_config['condition']
            item['confidence_level'] = level
            item['confidence_name'] = level_config['name']
            item['historical_win_rate'] = level_config['min_win_rate']
            item['condition_match'] = condition_desc
            final.append(item)
    for item in final:
        if 'confidence_level' not in item or not item['confidence_level']:
            item['confidence_level'] = 'C'
            item['confidence_name'] = 'C级·中等'
            item['historical_win_rate'] = 0.50
            item['condition_match'] = '默认等级'
    return final

def _get_current_weights(db_path: str) -> Dict[str, float]:
    """
    获取当前评分权重。
    数据库中的权重是 self_upgrader 动态调整的，可能只覆盖部分维度。
    未覆盖的维度使用 DEFAULT_WEIGHTS。
    最终权重会归一化到总和=1.0，确保评分尺度一致。
    """
    try:
        conn = get_conn(db_path)
        rows = conn.execute("""
            SELECT dimension, new_weight FROM (
                SELECT dimension, new_weight,
                       ROW_NUMBER() OVER (PARTITION BY dimension ORDER BY adjust_date DESC, id DESC) as rn
                FROM weight_adjustment_log
            ) WHERE rn = 1
        """).fetchall()
        conn.close()

        weights = DEFAULT_WEIGHTS.copy()
        if rows:
            for r in rows:
                dim = r['dimension']
                if dim in weights:
                    weights[dim] = r['new_weight']

        # 归一化：确保所有权重之和为1.0
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}

        return weights
    except Exception:
        return DEFAULT_WEIGHTS.copy()

def _build_reason_string(result: Dict) -> str:
    parts = []
    # 量价走势是首要依据，量价形态标签前置展示
    if result.get('vp_pattern'):
        gate_tag = result.get('vp_gate') or ''
        parts.append(f"【量价·{result['vp_pattern']}】{gate_tag}")
    reasons = result.get('dimension_reasons', {})
    scores = result.get('dimension_scores', {})
    sorted_dims = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_dims = sorted_dims[:2]
    for dim, score in top_dims:
        if dim in reasons and score >= 50:
            parts.append(reasons[dim])
    if len(parts) <= 1:
        parts.append(reasons.get('concept_heat', ''))
    return '；'.join(p for p in parts if p)

def _save_recommendations(date: str, recommendations: List[Dict], db_path: str):
    conn = get_conn(db_path)
    try:
        for rec in recommendations:
            existing = conn.execute("""
                SELECT id FROM recommendation_log
                WHERE rec_date = ? AND code = ?
            """, (date, rec['code'])).fetchone()
            if existing:
                # 已存在则更新（评分逻辑修复后用最新结果覆盖）
                conn.execute("""
                    UPDATE recommendation_log SET
                        name=?, score=?, reason=?, win_rate_estimate=?,
                        suggested_action=?, created_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (
                    rec['name'], rec['total_score'], rec['reason'],
                    rec.get('win_rate', 0), rec['suggested_action'],
                    existing['id'],
                ))
            else:
                conn.execute("""
                    INSERT INTO recommendation_log
                    (rec_date, target_date, code, name, score, reason,
                     win_rate_estimate, suggested_action)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    date, date, rec['code'], rec['name'],
                    rec['total_score'], rec['reason'],
                    rec.get('win_rate', 0), rec['suggested_action'],
                ))
        conn.commit()
    finally:
        conn.close()

# ─────────────────────────── 次日策略 ───────────────────────────

def recommend_for_next_day(date: str, db_path: str = DB_PATH) -> Dict[str, Any]:
    market_state = analyze_current_market(date, db_path)
    phase = market_state['cycle_phase']
    sentiment = market_state['sentiment']
    explosion_rate = market_state['explosion_rate']
    max_boards = market_state['max_boards']
    if phase in ('爆发高潮期',):
        target_height = f"{max_boards}~{max_boards+1}板"
    elif phase in ('蓄力爬升期',):
        target_height = f"{max(2, max_boards-1)}~{max_boards}板"
    else:  # 冰点酝酿期 / 崩塌退潮期
        target_height = "首板~2板为主"
    focus = market_state['hot_concepts_top5'][:3]
    risk_parts = []
    if explosion_rate > 0.25:
        risk_parts.append(f"炸板率偏高({explosion_rate:.0%})，建议降低仓位至3成以内")
    elif explosion_rate > 0.15:
        risk_parts.append(f"炸板率适中({explosion_rate:.0%})，建议仓位5成")
    else:
        risk_parts.append(f"炸板率良好({explosion_rate:.0%})，仓位可适当放大")
    if market_state.get('smash_coefficient') and market_state['smash_coefficient'] > 5:
        risk_parts.append(f"砸盘系数{market_state['smash_coefficient']:.1f}偏高，注意高位股抛压")
    if sentiment == 'bearish':
        risk_parts.append("情绪偏空，严格止损(-3%~-5%)")
    if phase in ('冰点酝酿期',):
        strategy = "冰点期防守为主，轻仓试错首板，重点观察是否有新题材破冰"
    elif phase in ('蓄力爬升期',):
        strategy = "蓄力期可逐步加仓，关注2板确认股，等待主线明确"
    elif phase in ('爆发高潮期',):
        strategy = "高潮期享受利润但提高警惕，关注龙头断板信号，准备撤退"
    elif phase in ('崩塌退潮期',):
        strategy = "崩塌期空仓或极轻仓观望，等待新的冰点机会"
    else:
        strategy = "市场周期未明确，建议观望"
    top_picks = generate_recommendations(date, top_n=3, db_path=db_path)
    return {
        'date': date,
        'target_board_height': target_height,
        'focus_concepts': focus,
        'risk_control': '；'.join(risk_parts),
        'overall_strategy': strategy,
        'top_picks': top_picks,
        'market_state': market_state,
    }

# ─────────────────────────── 输出格式化 ───────────────────────────

def format_recommendation_report(date: str, recommendations: List[Dict],
                                  market_state: Dict) -> str:
    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  📊 智能推荐报告（信心等级制）| {date}")
    lines.append(f"{'='*60}")
    lines.append("")
    lines.append(f"📈 市场状态:")
    lines.append(f"  周期阶段: {market_state.get('cycle_phase', '未知')}")
    lines.append(f"  涨停数量: {market_state.get('limit_up_count', 0)}")
    lines.append(f"  炸板率:   {market_state.get('explosion_rate', 0):.1%}")
    lines.append(f"  砸盘系数: {market_state.get('smash_coefficient', 'N/A')}")
    lines.append(f"  市场情绪: {market_state.get('sentiment', 'N/A')}")
    lines.append(f"  热门概念: {', '.join(market_state.get('hot_concepts_top5', [])[:5])}")
    lines.append(f"  连板梯队: {market_state.get('board_distribution', {})}")
    action_advice = market_state.get('action_advice', {})
    if action_advice:
        lines.append(f"\n💡 出击建议: {action_advice.get('advice_text', '')}")
    lines.append("")
    if not recommendations:
        lines.append("🎯 当前无符合信心等级的标的")
        lines.append("   市场条件下找不到确定性足够高的股票，宁缺毋滥，建议观望。")
        lines.append("")
    else:
        lines.append(f"🎯 推荐个股（共{len(recommendations)}只）:")
        lines.append(f"{'-'*60}")
        for i, rec in enumerate(recommendations, 1):
            conf_level = rec.get('confidence_level', '?')
            conf_name = rec.get('confidence_name', '未知')
            hist_wr = rec.get('historical_win_rate', 0)
            cond_match = rec.get('condition_match', '')
            lines.append(f"  {i}. 【{conf_level}级】{rec['name']}({rec['code']})  "
                        f"评分: {rec['total_score']}")
            lines.append(f"     信心等级: {conf_name} | 历史胜率: {hist_wr:.0%}")
            lines.append(f"     匹配条件: {cond_match}")
            lines.append(f"     连板: {rec.get('limit_up_days', 1)}板 | "
                        f"封单比: {(rec.get('seal_ratio', 0) or 0):.2%} | "
                        f"概念: {rec.get('concept', 'N/A')}")
            lines.append(f"     综合胜率: {rec.get('win_rate', 0):.0%} | "
                        f"建议: {rec['suggested_action']}")
            lines.append(f"     理由: {rec['reason']}")
            if rec.get('risk_notes'):
                lines.append(f"     ⚠️ 风险: {'; '.join(rec['risk_notes'])}")
            lines.append("")
    lines.append(f"{'='*60}")
    lines.append("⚠️ 声明：以上为AI模型分析结果，仅供参考，不构成投资建议。")
    lines.append(f"{'='*60}")
    return '\n'.join(lines)

def format_next_day_strategy(strategy: Dict) -> str:
    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  📋 次日策略 | {strategy['date']}")
    lines.append(f"{'='*60}")
    lines.append("")
    lines.append(f"🎯 整体策略: {strategy['overall_strategy']}")
    lines.append(f"📊 目标连板高度: {strategy['target_board_height']}")
    lines.append(f"🔥 关注概念: {', '.join(strategy['focus_concepts'])}")
    lines.append(f"⚠️ 风控要点: {strategy['risk_control']}")
    market_state = strategy.get('market_state', {})
    action_advice = market_state.get('action_advice', {})
    if action_advice:
        lines.append(f"\n💡 出击建议: {action_advice.get('advice_text', '')}")
    lines.append("")
    if strategy.get('top_picks'):
        lines.append("🏆 精选标的:")
        for i, p in enumerate(strategy['top_picks'], 1):
            conf = p.get('confidence_level', '?')
            hist_wr = p.get('historical_win_rate', 0)
            lines.append(f"  {i}. 【{conf}级】{p['name']}({p['code']}) "
                        f"评分{p['total_score']} 胜率{p.get('win_rate',0):.0%} "
                        f"(历史胜率{hist_wr:.0%}) "
                        f"→ {p['suggested_action']}")
    else:
        lines.append("\n🏆 精选标的: 当前无符合信心等级的标的，建议观望等待")
    lines.append("")
    lines.append(f"{'='*60}")
    return '\n'.join(lines)

# ─────────────────────────── 主程序 ───────────────────────────

def main():
    parser = argparse.ArgumentParser(description='智能个股推荐引擎')
    parser.add_argument('--date', type=str, default=None,
                        help='指定推荐日期 (YYYY-MM-DD)，默认使用最新交易日')
    parser.add_argument('--top', type=int, default=5,
                        help='推荐股票数量 (默认5)')
    parser.add_argument('--next-day', action='store_true',
                        help='生成次日策略')
    parser.add_argument('--db', type=str, default=DB_PATH,
                        help='数据库路径')
    args = parser.parse_args()
    db_path = args.db
    if args.date:
        date = args.date
    else:
        date = get_latest_date(db_path, 'xgt_limit_up_detail')
        if not date:
            logger.error("无法获取最新交易日，请检查数据库")
            sys.exit(1)
        logger.info(f"使用最新交易日: {date}")
    init_new_tables(db_path)
    if args.next_day:
        strategy = recommend_for_next_day(date, db_path)
        report = format_next_day_strategy(strategy)
        print(report)
    else:
        market_state = analyze_current_market(date, db_path)
        recommendations = generate_recommendations(date, top_n=args.top, db_path=db_path)
        report = format_recommendation_report(date, recommendations, market_state)
        print(report)

if __name__ == '__main__':
    main()