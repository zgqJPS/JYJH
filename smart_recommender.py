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

# ─────────────────────────── 日志配置 ───────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('smart_recommender')

# 评分维度权重（初始值，可被 self_upgrader 动态调整）
DEFAULT_WEIGHTS = {
    'concept_heat':   0.15,
    'board_position': 0.25,
    'seal_quality':   0.40,
    'cap_fit':        0.10,
    'volume_price':   0.10,
}

# ─────────────────────────── 信心等级定义 ───────────────────────────
CONFIDENCE_LEVELS = {
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

CONFIDENCE_PRIORITY = ['S', 'A', 'B', 'C']

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
        return [dict(r) for r in rows]
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
    global _promotion_rate_cache
    if _promotion_rate_cache:
        return _promotion_rate_cache
    conn = get_conn(db_path)
    try:
        dates_rows = conn.execute("""
            SELECT DISTINCT date FROM xgt_limit_up_detail ORDER BY date
        """).fetchall()
        date_list = [r['date'] for r in dates_rows]
        if len(date_list) < 2:
            _promotion_rate_cache = dict(BOARD_PROMOTION_BASELINE)
            return _promotion_rate_cache
        date_to_next = {}
        for i in range(len(date_list) - 1):
            date_to_next[date_list[i]] = date_list[i + 1]
        all_data = conn.execute("""
            SELECT date, code, limit_up_days FROM xgt_limit_up_detail
        """).fetchall()
        data_map = {}
        for r in all_data:
            data_map[(r['date'], r['code'])] = r['limit_up_days']
        level_stats = defaultdict(lambda: {'total': 0, 'promoted': 0})
        for r in all_data:
            d, code, boards = r['date'], r['code'], r['limit_up_days']
            next_date = date_to_next.get(d)
            if not next_date:
                continue
            level_stats[boards]['total'] += 1
            next_boards = data_map.get((next_date, code), 0)
            if next_boards and next_boards > boards:
                level_stats[boards]['promoted'] += 1
        for level, stats in level_stats.items():
            if stats['total'] >= 2:
                _promotion_rate_cache[level] = stats['promoted'] / stats['total']
            else:
                _promotion_rate_cache[level] = BOARD_PROMOTION_BASELINE.get(level, 0.25)
        for level in BOARD_PROMOTION_BASELINE:
            if level not in _promotion_rate_cache:
                _promotion_rate_cache[level] = BOARD_PROMOTION_BASELINE[level]
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
    }
    result['action_advice'] = _get_action_advice(result)
    return result

def _get_action_advice(market_state: Dict) -> Dict[str, str]:
    smash = market_state.get('smash_coefficient')
    explosion_rate = market_state.get('explosion_rate', 0) or 0
    smash_val = smash if smash is not None else 3.5
    if smash_val > 6.0:
        max_level = 'S'
        advice_text = (f"当前砸盘系数{smash_val:.1f}（极高）+炸板率{explosion_rate:.0%}，"
                      f"市场风险极大，仅建议操作S级确定性极高的标的")
    elif explosion_rate > 0.40:
        max_level = 'A'
        advice_text = (f"当前炸板率{explosion_rate:.0%}（>40%，极高）+砸盘系数{smash_val:.1f}，"
                      f"市场分歧极大，建议操作A级及以上确定性标的")
    elif smash_val < 3.0:
        max_level = 'C'
        advice_text = (f"当前砸盘系数{smash_val:.1f}（偏低）+炸板率{explosion_rate:.0%}，"
                      f"市场状态温和，可操作至C级标的")
    else:
        max_level = 'B'
        advice_text = (f"当前砸盘系数{smash_val:.1f}+炸板率{explosion_rate:.0%}，"
                      f"建议操作B级及以上确定性标的")
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
    if 0.03 <= turnover <= 0.15:
        score += 15
        reason_parts.append(f"换手{turnover:.1%}(健康)")
    elif turnover > 0.15:
        score += 5
        reason_parts.append(f"换手{turnover:.1%}(偏高)")
    else:
        score += 8
        reason_parts.append(f"换手{turnover:.1%}(偏低)")
    if break_times == 0:
        score += 15
        reason_parts.append("零开板")
    elif break_times <= 2:
        score += 5
        reason_parts.append(f"开板{break_times}次")
    else:
        score -= 10
        reason_parts.append(f"开板{break_times}次(封板不稳)")
    if first_time:
        try:
            h, m, s = [int(x) for x in first_time.split(':')]
            minutes = h * 60 + m
            if minutes <= 30:
                score += 15
                reason_parts.append(f"{first_time}封板(极早)")
            elif minutes <= 45:
                score += 10
                reason_parts.append(f"{first_time}封板(早)")
            elif minutes <= 120:
                score += 5
                reason_parts.append(f"{first_time}封板(上午)")
            elif minutes <= 660:
                score += 0
                reason_parts.append(f"{first_time}封板(下午)")
            else:
                score -= 10
                reason_parts.append(f"{first_time}封板(尾盘)")
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

def score_stock(stock: Dict, market_state: Dict,
                weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    w = weights or DEFAULT_WEIGHTS
    c_score, c_reason = _score_concept_heat(stock, market_state)
    b_score, b_reason = _score_board_position(stock, market_state)
    s_score, s_reason = _score_seal_quality(stock, market_state)
    m_score, m_reason = _score_cap_fit(stock, market_state)
    v_score, v_reason = _score_volume_price(stock, market_state)
    total = (
        c_score * w['concept_heat'] +
        b_score * w['board_position'] +
        s_score * w['seal_quality'] +
        m_score * w['cap_fit'] +
        v_score * w['volume_price']
    )
    is_broken = stock.get('_from_break_pool', False)
    if is_broken:
        total *= 0.80
    sentiment = market_state.get('sentiment', 'neutral')
    if sentiment == 'bearish':
        total *= 0.90
    elif sentiment == 'bullish':
        total *= 1.05
    total = round(min(max(total, 0), 100), 1)
    action = _suggest_action(total, stock, market_state)
    risks = _generate_risks(stock, market_state, total)
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
        },
        'dimension_reasons': {
            'concept_heat': c_reason,
            'board_position': b_reason,
            'seal_quality': s_reason,
            'cap_fit': m_reason,
            'volume_price': v_reason,
        },
        'suggested_action': action,
        'risk_notes': risks,
        'concept': stock.get('concept', ''),
        'limit_up_days': stock.get('limit_up_days', 1),
        'seal_ratio': stock.get('seal_ratio', 0),
    }

def _suggest_action(score: float, stock: Dict, market_state: Dict) -> str:
    boards = stock.get('limit_up_days', 1) or 1
    is_broken = stock.get('_from_break_pool', False)
    phase = market_state.get('cycle_phase', '蓄力爬升期')
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

def _generate_risks(stock: Dict, market_state: Dict, score: float) -> List[str]:
    risks = []
    boards = stock.get('limit_up_days', 1) or 1
    break_times = stock.get('break_times', 0) or 0
    explosion_rate = market_state.get('explosion_rate', 0)
    phase = market_state.get('cycle_phase', '蓄力爬升期')
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
    score = stock_score['total_score']
    boards = stock_score.get('limit_up_days', 1)
    phase = market_state.get('cycle_phase', '蓄力爬升期')
    base_rate = 0.40
    base_rate += (score - 50) * 0.005
    promo = get_historical_promotion_rate(boards, db_path)
    base_rate = base_rate * 0.6 + promo * 0.4
    if phase in ('爆发高潮期', '蓄力爬升期'):
        base_rate *= 1.1
    elif phase in ('崩塌退潮期',):
        base_rate *= 0.8
    return round(max(0.15, min(base_rate, 0.85)), 2)

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
    if not all_candidates:
        logger.info(f"[{date}] 无候选股票，返回空推荐")
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
    final = []
    assigned_codes = set()
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
            stock = item['_stock_data']
            try:
                if needs_max_board:
                    is_match = filter_fn(stock, max_board)
                else:
                    is_match = filter_fn(stock)
            except TypeError:
                is_match = False
            if is_match:
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
        if not rows:
            return DEFAULT_WEIGHTS.copy()
        weights = DEFAULT_WEIGHTS.copy()
        for r in rows:
            dim = r['dimension']
            if dim in weights:
                weights[dim] = r['new_weight']
        return weights
    except Exception:
        return DEFAULT_WEIGHTS.copy()

def _build_reason_string(result: Dict) -> str:
    parts = []
    reasons = result.get('dimension_reasons', {})
    scores = result.get('dimension_scores', {})
    sorted_dims = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_dims = sorted_dims[:2]
    for dim, score in top_dims:
        if dim in reasons and score >= 50:
            parts.append(reasons[dim])
    if not parts:
        parts.append(reasons.get('concept_heat', ''))
    return '；'.join(parts)

def _save_recommendations(date: str, recommendations: List[Dict], db_path: str):
    conn = get_conn(db_path)
    try:
        for rec in recommendations:
            existing = conn.execute("""
                SELECT id FROM recommendation_log
                WHERE rec_date = ? AND code = ?
            """, (date, rec['code'])).fetchone()
            if existing:
                continue
            conn.execute("""
                INSERT INTO recommendation_log
                (rec_date, target_date, code, name, score, reason,
                 win_rate_estimate, suggested_action)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date,
                date,
                rec['code'],
                rec['name'],
                rec['total_score'],
                rec['reason'],
                rec.get('win_rate', 0),
                rec['suggested_action'],
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