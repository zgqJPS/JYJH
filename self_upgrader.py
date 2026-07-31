"""
self_upgrader.py - 自适应升级系统
===================================
基于推荐跟踪结果，自动分析预测准确性、调整评分权重、
检测市场风格切换，实现推荐引擎的持续自我优化。

主要功能:
  - analyze_prediction_accuracy(db, days=30)  分析预测准确性
  - adjust_weights(db)                        自动调整权重
  - detect_regime_change(db)                  检测市场风格切换
  - auto_upgrade(db)                          自动升级入口

用法:
  python self_upgrader.py               # 运行自动升级
  python self_upgrader.py --check-only  # 仅检查不调整
  python self_upgrader.py --days 60     # 分析最近60天
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

# ─────────────────────────── 日志 ───────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('self_upgrader')

# ─────────────────────────── 常量 ───────────────────────────
def _auto_detect_db():
    """自动检测数据库路径"""
    import glob as _g
    _d = os.path.dirname(os.path.abspath(__file__))
    for f in _g.glob(os.path.join(_d, 'stock_data_*.db')):
        return f
    for f in _g.glob('stock_data_*.db'):
        return os.path.abspath(f)
    for f in _g.glob(os.path.join(_d, '..', 'stock_data_*.db')):
        return os.path.abspath(f)
    return 'stock_data.db'

DB_PATH = _auto_detect_db()

# 默认权重
DEFAULT_WEIGHTS = {
    'concept_heat':   0.25,
    'board_position': 0.20,
    'seal_quality':   0.25,
    'cap_fit':        0.15,
    'volume_price':   0.15,
}

# 调整步长
ADJUST_STEP = 0.03        # 每次调整的步长
MIN_WEIGHT = 0.05         # 权重下限
MAX_WEIGHT = 0.50         # 权重上限

# 市场风格定义
MARKET_REGIMES = {
    '连板接力': {'特征': '高连板活跃，3板以上股票多', '指标': 'high_board_active'},
    '首板轮动': {'特征': '首板为主，板块轮动快', '指标': 'first_board_rotation'},
    '大盘价值': {'特征': '大市值股涨停占比高', '指标': 'large_cap_dominant'},
    '小盘投机': {'特征': '小市值股涨停占比高', '指标': 'small_cap_dominant'},
    '趋势延续': {'特征': '连板股次日晋级率高', '指标': 'high_promotion_rate'},
    '打板套利': {'特征': '封板成功率低，炸板率高', '指标': 'high_explosion'},
}


# ─────────────────────────── 数据库工具 ───────────────────────────

def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables(db_path: str = DB_PATH):
    """确保所需表存在"""
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
    CREATE TABLE IF NOT EXISTS signal_tracking (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER NOT NULL,
        trigger_date TEXT NOT NULL,
        trigger_stocks TEXT,
        next_day_result TEXT,
        avg_return REAL,
        verified INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS regime_detection_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        detect_date TEXT NOT NULL,
        current_regime TEXT,
        prev_regime TEXT,
        regime_changed INTEGER DEFAULT 0,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS upgrade_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        upgrade_date TEXT NOT NULL,
        upgrade_type TEXT NOT NULL,
        details TEXT,
        status TEXT DEFAULT 'completed',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()
    logger.info("自适应升级系统表初始化完成")


# ─────────────────────────── 准确性分析 ───────────────────────────

def analyze_prediction_accuracy(days: int = 30, db_path: str = DB_PATH) -> Dict[str, Any]:
    """
    分析最近N天的推荐准确性
    
    分析维度：
    1. 总体胜率
    2. 各评分区间的胜率
    3. 各概念的历史胜率
    4. 各连板层级的胜率
    5. 各操作建议的胜率
    6. 胜率估计的校准度（预估胜率 vs 实际胜率）
    
    Returns:
        {
            'period': (start_date, end_date),
            'total_recommendations': int,
            'overall_win_rate': float,
            'by_score_range': Dict,
            'by_concept': Dict,
            'by_board_level': Dict,
            'by_action': Dict,
            'calibration': Dict,
            'dimension_correlation': Dict,
        }
    """
    conn = get_conn(db_path)

    try:
        # 获取最近N天的推荐记录
        rows = conn.execute("""
            SELECT * FROM recommendation_log
            WHERE is_correct IS NOT NULL
            ORDER BY rec_date DESC
            LIMIT 1000
        """).fetchall()

        if not rows:
            logger.warning("没有已验证的推荐记录")
            return {'error': 'no_data', 'total_recommendations': 0}

        # 过滤最近N天
        records = [dict(r) for r in rows]
        if records:
            latest = records[0]['rec_date']
            cutoff = (datetime.strptime(latest, '%Y-%m-%d') - timedelta(days=days)).strftime('%Y-%m-%d')
            records = [r for r in records if r['rec_date'] >= cutoff]

        if not records:
            return {'error': 'no_data_in_period', 'total_recommendations': 0}

        total = len(records)
        correct = sum(1 for r in records if r['is_correct'] == 1)
        overall_wr = correct / total if total > 0 else 0

        # --- 各评分区间胜率 ---
        score_ranges = {'高分(80+)': [], '中高分(65-80)': [], '中分(50-65)': [], '低分(<50)': []}
        for r in records:
            score = r.get('score', 0) or 0
            if score >= 80:
                score_ranges['高分(80+)'].append(r)
            elif score >= 65:
                score_ranges['中高分(65-80)'].append(r)
            elif score >= 50:
                score_ranges['中分(50-65)'].append(r)
            else:
                score_ranges['低分(<50)'].append(r)

        by_score = {}
        for range_name, recs in score_ranges.items():
            if recs:
                c = sum(1 for r in recs if r['is_correct'] == 1)
                by_score[range_name] = {
                    'total': len(recs),
                    'correct': c,
                    'win_rate': c / len(recs),
                }

        # --- 各概念胜率 ---
        # 从 reason 中提取概念信息
        concept_map = defaultdict(list)
        for r in records:
            reason = r.get('reason', '') or ''
            # 尝试从 reason 中提取概念（格式: 热门概念「XXX」）
            import re
            matches = re.findall(r'概念「(.+?)」', reason)
            for m in matches:
                concept_map[m].append(r)
            if not matches:
                concept_map['未知'].append(r)

        by_concept = {}
        for concept, recs in concept_map.items():
            if len(recs) >= 2:  # 至少2条记录
                c = sum(1 for r in recs if r['is_correct'] == 1)
                by_concept[concept] = {
                    'total': len(recs),
                    'correct': c,
                    'win_rate': c / len(recs),
                }

        # --- 各连板层级胜率 ---
        board_map = defaultdict(list)
        for r in records:
            # 从 reason 中提取连板信息
            reason = r.get('reason', '') or ''
            bmatch = re.search(r'(\d+)连板', reason)
            if bmatch:
                level = int(bmatch.group(1))
                if level >= 4:
                    board_map['高标(4+)'].append(r)
                elif level >= 2:
                    board_map[f'{level}板'].append(r)
                else:
                    board_map['首板'].append(r)
            else:
                board_map['首板'].append(r)

        by_board = {}
        for level, recs in board_map.items():
            if recs:
                c = sum(1 for r in recs if r['is_correct'] == 1)
                by_board[level] = {
                    'total': len(recs),
                    'correct': c,
                    'win_rate': c / len(recs),
                }

        # --- 各操作建议胜率 ---
        action_map = defaultdict(list)
        for r in records:
            action = r.get('suggested_action', '未知') or '未知'
            # 简化操作类型
            if '追涨' in action:
                action_map['追涨'].append(r)
            elif '打板' in action:
                action_map['打板'].append(r)
            elif '低吸' in action:
                action_map['低吸'].append(r)
            elif '观望' in action:
                action_map['观望'].append(r)
            elif '回避' in action:
                action_map['回避'].append(r)
            else:
                action_map['其他'].append(r)

        by_action = {}
        for action, recs in action_map.items():
            if recs:
                c = sum(1 for r in recs if r['is_correct'] == 1)
                by_action[action] = {
                    'total': len(recs),
                    'correct': c,
                    'win_rate': c / len(recs),
                }

        # --- 校准度分析 ---
        # 将预估胜率分桶，对比实际胜率
        calib_buckets = {'0-20%': [], '20-40%': [], '40-60%': [], '60-80%': [], '80-100%': []}
        for r in records:
            wr = r.get('win_rate_estimate', 0.5) or 0.5
            if wr < 0.2:
                calib_buckets['0-20%'].append(r)
            elif wr < 0.4:
                calib_buckets['20-40%'].append(r)
            elif wr < 0.6:
                calib_buckets['40-60%'].append(r)
            elif wr < 0.8:
                calib_buckets['60-80%'].append(r)
            else:
                calib_buckets['80-100%'].append(r)

        calibration = {}
        for bucket, recs in calib_buckets.items():
            if recs:
                actual_wr = sum(1 for r in recs if r['is_correct'] == 1) / len(recs)
                calibration[bucket] = {
                    'count': len(recs),
                    'actual_win_rate': actual_wr,
                }

        # --- 各评分维度的相关性（使用维度得分与实际结果的相关性）---
        # 从 reason 中解析维度得分比较困难，改用整体评分分段
        # 这里简化处理：比较各概念和层级的贡献
        dimension_corr = {}
        # 概念维度：计算最高胜率概念和最低胜率概念的差距
        if by_concept:
            rates = [v['win_rate'] for v in by_concept.values() if v['total'] >= 3]
            if rates:
                dimension_corr['concept_heat'] = max(rates) - min(rates)
        # 连板维度
        if by_board:
            rates = [v['win_rate'] for v in by_board.values() if v['total'] >= 3]
            if rates:
                dimension_corr['board_position'] = max(rates) - min(rates)
        # 操作维度
        if by_action:
            rates = [v['win_rate'] for v in by_action.values() if v['total'] >= 3]
            if rates:
                dimension_corr['action_accuracy'] = max(rates) - min(rates)

        period_start = records[-1]['rec_date']
        period_end = records[0]['rec_date']

        result = {
            'period': (period_start, period_end),
            'total_recommendations': total,
            'overall_win_rate': overall_wr,
            'by_score_range': by_score,
            'by_concept': by_concept,
            'by_board_level': by_board,
            'by_action': by_action,
            'calibration': calibration,
            'dimension_correlation': dimension_corr,
        }

        logger.info(f"准确性分析完成: {period_start}~{period_end}, "
                    f"共{total}条记录, 总胜率{overall_wr:.1%}")
        return result

    finally:
        conn.close()


# ─────────────────────────── 权重调整 ───────────────────────────

def _get_current_weights(db_path: str) -> Dict[str, float]:
    """获取当前权重（考虑历史调整）"""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("""
            SELECT dimension, new_weight FROM weight_adjustment_log
            ORDER BY adjust_date DESC
        """).fetchall()
        weights = DEFAULT_WEIGHTS.copy()
        latest = {}
        for r in rows:
            dim = r['dimension']
            if dim not in latest:
                latest[dim] = r['new_weight']
        for dim in weights:
            if dim in latest:
                weights[dim] = latest[dim]
        return weights
    except Exception:
        return DEFAULT_WEIGHTS.copy()
    finally:
        conn.close()


def _normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    """归一化权重使总和为1"""
    total = sum(weights.values())
    if total > 0:
        return {k: round(v / total, 4) for k, v in weights.items()}
    return weights


def adjust_weights(db_path: str = DB_PATH, check_only: bool = False) -> Dict[str, Any]:
    """
    根据准确性分析结果自动调整评分权重
    
    调整策略：
    1. 分析各维度（概念/连板/封板/市值/量价）对预测准确率的贡献
    2. 对准确率贡献大的维度 → 提高权重
    3. 对准确率贡献小的维度 → 降低权重
    4. 连续失败的信号/概念 → 降低其影响
    
    Args:
        db_path: 数据库路径
        check_only: 仅检查不实际调整
    
    Returns:
        {
            'old_weights': Dict,
            'new_weights': Dict,
            'adjustments': List[Dict],
            'reason': str,
        }
    """
    init_tables(db_path)

    # 1. 分析准确性
    accuracy = analyze_prediction_accuracy(days=30, db_path=db_path)
    if accuracy.get('error'):
        logger.warning(f"无法分析准确性: {accuracy.get('error')}")
        return {'error': accuracy.get('error'), 'adjustments': []}

    conn = get_conn(db_path)
    try:
        current_weights = _get_current_weights(db_path)
        new_weights = current_weights.copy()
        adjustments = []
        today = datetime.now().strftime('%Y-%m-%d')

        # 2. 基于评分区间分析调整
        # 如果高分段胜率高 → 提升整体评分模型的区分度 → 加大各维度权重差异
        # 如果高分段胜率低 → 降低权重差异，增加保守因子
        score_analysis = accuracy.get('by_score_range', {})
        high_wr = score_analysis.get('高分(80+)', {}).get('win_rate', 0)
        low_wr = score_analysis.get('低分(<50)', {}).get('win_rate', 0)

        discriminability = high_wr - low_wr if high_wr and low_wr else 0

        # 3. 基于概念维度调整
        concept_corr = accuracy.get('dimension_correlation', {}).get('concept_heat', 0)
        board_corr = accuracy.get('dimension_correlation', {}).get('board_position', 0)

        # 概念维度相关性强 → 增加概念权重
        if concept_corr > 0.20:
            delta = ADJUST_STEP
            old_w = new_weights['concept_heat']
            new_weights['concept_heat'] = min(old_w + delta, MAX_WEIGHT)
            adjustments.append({
                'dimension': 'concept_heat',
                'old_weight': old_w,
                'new_weight': new_weights['concept_heat'],
                'reason': f'概念维度区分度{concept_corr:.2f}(强), 提升概念权重',
            })
        elif concept_corr < 0.10:
            delta = ADJUST_STEP
            old_w = new_weights['concept_heat']
            new_weights['concept_heat'] = max(old_w - delta, MIN_WEIGHT)
            adjustments.append({
                'dimension': 'concept_heat',
                'old_weight': old_w,
                'new_weight': new_weights['concept_heat'],
                'reason': f'概念维度区分度{concept_corr:.2f}(弱), 降低概念权重',
            })

        # 连板维度相关性强 → 增加连板权重
        if board_corr > 0.20:
            delta = ADJUST_STEP
            old_w = new_weights['board_position']
            new_weights['board_position'] = min(old_w + delta, MAX_WEIGHT)
            adjustments.append({
                'dimension': 'board_position',
                'old_weight': old_w,
                'new_weight': new_weights['board_position'],
                'reason': f'连板维度区分度{board_corr:.2f}(强), 提升连板权重',
            })
        elif board_corr < 0.10:
            delta = ADJUST_STEP
            old_w = new_weights['board_position']
            new_weights['board_position'] = max(old_w - delta, MIN_WEIGHT)
            adjustments.append({
                'dimension': 'board_position',
                'old_weight': old_w,
                'new_weight': new_weights['board_position'],
                'reason': f'连板维度区分度{board_corr:.2f}(弱), 降低连板权重',
            })

        # 4. 基于概念胜率调整（连续失败的概念降低影响力）
        # 这通过 concept_heat 维度的间接调整实现
        by_concept = accuracy.get('by_concept', {})
        failing_concepts = [c for c, v in by_concept.items()
                          if v['total'] >= 3 and v['win_rate'] < 0.25]
        if failing_concepts:
            # 有连续失败的概念 → 略微降低概念权重
            delta = ADJUST_STEP * 0.5
            old_w = new_weights['concept_heat']
            new_weights['concept_heat'] = max(old_w - delta, MIN_WEIGHT)
            adj_record = {
                'dimension': 'concept_heat',
                'old_weight': old_w,
                'new_weight': new_weights['concept_heat'],
                'reason': f'低胜率概念({",".join(failing_concepts[:3])}), 降低概念权重',
            }
            # 避免重复调整
            if not any(a['dimension'] == 'concept_heat' for a in adjustments):
                adjustments.append(adj_record)

        # 5. 基于操作建议胜率调整
        by_action = accuracy.get('by_action', {})
        # 如果"追涨"胜率很低 → 可能市场不适合追涨 → 调整封板质量权重
        chase_wr = by_action.get('追涨', {}).get('win_rate', 0.5)
        if chase_wr < 0.20:
            delta = ADJUST_STEP
            old_w = new_weights['seal_quality']
            new_weights['seal_quality'] = min(old_w + delta, MAX_WEIGHT)
            adjustments.append({
                'dimension': 'seal_quality',
                'old_weight': old_w,
                'new_weight': new_weights['seal_quality'],
                'reason': f'追涨胜率仅{chase_wr:.0%}, 提升封板质量要求',
            })

        # 6. 归一化
        new_weights = _normalize_weights(new_weights)

        # 7. 写入数据库
        if not check_only and adjustments:
            for adj in adjustments:
                conn.execute("""
                    INSERT INTO weight_adjustment_log
                    (adjust_date, dimension, old_weight, new_weight, reason,
                     accuracy_before, accuracy_after)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    today,
                    adj['dimension'],
                    adj['old_weight'],
                    adj['new_weight'],
                    adj['reason'],
                    accuracy['overall_win_rate'],
                    None,  # accuracy_after 需后续跟踪
                ))
            conn.commit()
            logger.info(f"权重调整完成: {len(adjustments)}项调整已记录")
        elif check_only and adjustments:
            logger.info(f"[仅检查] 发现{len(adjustments)}项需要调整:")
            for adj in adjustments:
                logger.info(f"  {adj['dimension']}: {adj['old_weight']:.4f} → {adj['new_weight']:.4f}")
                logger.info(f"    原因: {adj['reason']}")

        # 8. 更新 signal_weights 表中的信号权重
        _update_signal_weights(accuracy, conn, today, check_only)

        return {
            'old_weights': current_weights,
            'new_weights': new_weights,
            'adjustments': adjustments,
            'overall_win_rate': accuracy['overall_win_rate'],
            'reason': f"基于{accuracy['total_recommendations']}条记录分析, "
                      f"胜率{accuracy['overall_win_rate']:.1%}, "
                      f"调整{len(adjustments)}项权重",
        }

    finally:
        conn.close()


def _update_signal_weights(accuracy: Dict, conn: sqlite3.Connection,
                           today: str, check_only: bool):
    """更新信号权重表中的连续成功/失败计数"""
    # 获取最近的推荐记录，按信号维度统计
    # 简化处理：使用操作类型作为信号代理
    by_action = accuracy.get('by_action', {})

    signal_action_map = {
        1: '追涨',  # 信号1对应追涨类
        2: '低吸',  # 信号2对应低吸类
        3: '打板',  # 信号3对应打板类
    }

    for sig_id, action_name in signal_action_map.items():
        action_stats = by_action.get(action_name, {})
        total = action_stats.get('total', 0)
        correct = action_stats.get('correct', 0)

        if total > 0:
            wr = correct / total
            # 更新 signal_weights
            if wr >= 0.5:
                conn.execute("""
                    UPDATE signal_weights
                    SET consecutive_success = consecutive_success + 1,
                        consecutive_failure = 0,
                        total_correct = total_correct + ?,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE signal_id = ?
                """, (correct, sig_id))
            else:
                conn.execute("""
                    UPDATE signal_weights
                    SET consecutive_failure = consecutive_failure + 1,
                        consecutive_success = 0,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE signal_id = ?
                """, (sig_id,))

    if not check_only:
        conn.commit()


# ─────────────────────────── 风格切换检测 ───────────────────────────

def detect_regime_change(db_path: str = DB_PATH, check_only: bool = False) -> Dict[str, Any]:
    """
    检测市场是否发生了风格切换
    
    检测维度：
    1. 连板高度趋势（连板接力 vs 首板轮动）
    2. 市值偏好变化（大盘 vs 小盘）
    3. 封板成功率变化（打板套利 vs 趋势延续）
    4. 概念集中度变化（主线明确 vs 散乱）
    
    Returns:
        {
            'current_regime': str,
            'prev_regime': str,
            'is_changed': bool,
            'evidence': List[str],
            'recommended_adjustments': Dict,
        }
    """
    init_tables(db_path)
    conn = get_conn(db_path)

    try:
        # 获取最近10个交易日的数据
        dates = conn.execute("""
            SELECT DISTINCT date FROM xgt_daily_summary
            ORDER BY date DESC LIMIT 10
        """).fetchall()
        if len(dates) < 5:
            return {'error': 'insufficient_data', 'is_changed': False}

        date_list = [r['date'] for r in dates]
        recent_5 = date_list[:5]
        prev_5 = date_list[5:10] if len(date_list) >= 10 else []

        # --- 分析维度1: 连板高度趋势 ---
        recent_boards = _get_avg_max_boards(conn, recent_5)
        prev_boards = _get_avg_max_boards(conn, prev_5) if prev_5 else recent_boards

        # --- 分析维度2: 市值偏好 ---
        recent_cap = _get_avg_flow_cap(conn, recent_5)
        prev_cap = _get_avg_flow_cap(conn, prev_5) if prev_5 else recent_cap

        # --- 分析维度3: 封板成功率 ---
        recent_explosion = _get_avg_explosion_rate(conn, recent_5)
        prev_explosion = _get_avg_explosion_rate(conn, prev_5) if prev_5 else recent_explosion

        # --- 分析维度4: 概念集中度 ---
        recent_concept_conc = _get_concept_concentration(conn, recent_5)
        prev_concept_conc = _get_concept_concentration(conn, prev_5) if prev_5 else recent_concept_conc

        # --- 综合判断当前风格 ---
        evidence = []
        current_regime = _classify_regime(
            recent_boards, prev_boards,
            recent_cap, prev_cap,
            recent_explosion, prev_explosion,
            recent_concept_conc, prev_concept_conc,
            evidence
        )

        # --- 获取上一次检测的风格 ---
        prev_regime_row = conn.execute("""
            SELECT current_regime FROM regime_detection_log
            ORDER BY detect_date DESC LIMIT 1
        """).fetchone()
        prev_regime = prev_regime_row['current_regime'] if prev_regime_row else None

        is_changed = prev_regime is not None and prev_regime != current_regime

        # --- 生成调整建议 ---
        adjustments = _get_regime_adjustments(current_regime, prev_regime, is_changed)

        # 保存检测结果
        if not check_only:
            today = datetime.now().strftime('%Y-%m-%d')
            conn.execute("""
                INSERT INTO regime_detection_log
                (detect_date, current_regime, prev_regime, regime_changed, details)
                VALUES (?, ?, ?, ?, ?)
            """, (
                today, current_regime, prev_regime,
                1 if is_changed else 0,
                json.dumps({
                    'evidence': evidence,
                    'recent_boards': recent_boards,
                    'prev_boards': prev_boards,
                    'recent_cap': recent_cap,
                    'prev_cap': prev_cap,
                    'recent_explosion': recent_explosion,
                    'prev_explosion': prev_explosion,
                    'recent_concept_conc': recent_concept_conc,
                    'prev_concept_conc': prev_concept_conc,
                }, ensure_ascii=False)
            ))
            conn.commit()

        result = {
            'current_regime': current_regime,
            'prev_regime': prev_regime,
            'is_changed': is_changed,
            'evidence': evidence,
            'metrics': {
                'avg_max_boards': (recent_boards, prev_boards),
                'avg_flow_cap': (recent_cap, prev_cap),
                'avg_explosion_rate': (recent_explosion, prev_explosion),
                'concept_concentration': (recent_concept_conc, prev_concept_conc),
            },
            'recommended_adjustments': adjustments,
        }

        if is_changed:
            logger.info(f"🔄 检测到风格切换: {prev_regime} → {current_regime}")
        else:
            logger.info(f"当前风格: {current_regime} (未变化)")

        return result

    finally:
        conn.close()


def _get_avg_max_boards(conn, dates: List[str]) -> float:
    """计算指定日期列表的平均最高连板"""
    if not dates:
        return 0
    placeholders = ','.join(['?' for _ in dates])
    rows = conn.execute(f"""
        SELECT AVG(max_continuous_boards) as avg_boards
        FROM xgt_daily_summary WHERE date IN ({placeholders})
    """, dates).fetchone()
    return rows['avg_boards'] if rows and rows['avg_boards'] else 0


def _get_avg_flow_cap(conn, dates: List[str]) -> float:
    """计算指定日期列表的平均流通市值"""
    if not dates:
        return 0
    placeholders = ','.join(['?' for _ in dates])
    rows = conn.execute(f"""
        SELECT AVG(flow_capital) as avg_cap
        FROM xgt_limit_up_detail WHERE date IN ({placeholders})
    """, dates).fetchone()
    return rows['avg_cap'] if rows and rows['avg_cap'] else 0


def _get_avg_explosion_rate(conn, dates: List[str]) -> float:
    """计算指定日期列表的平均炸板率"""
    if not dates:
        return 0
    placeholders = ','.join(['?' for _ in dates])
    rows = conn.execute(f"""
        SELECT AVG(explosion_rate) as avg_er
        FROM xgt_daily_summary WHERE date IN ({placeholders})
    """, dates).fetchone()
    return rows['avg_er'] if rows and rows['avg_er'] else 0


def _get_concept_concentration(conn, dates: List[str]) -> float:
    """计算概念集中度（TOP3概念占比）"""
    if not dates:
        return 0
    placeholders = ','.join(['?' for _ in dates])

    # 每日TOP3概念占总概念数的比例
    rows = conn.execute(f"""
        SELECT date,
            (SELECT SUM(count) FROM concept_statistics c2
             WHERE c2.date = c1.date
             ORDER BY c2.count DESC LIMIT 3) * 1.0 /
            (SELECT SUM(count) FROM concept_statistics c3
             WHERE c3.date = c1.date) as concentration
        FROM concept_statistics c1
        WHERE c1.date IN ({placeholders})
        GROUP BY date
    """, dates).fetchall()

    if not rows:
        return 0
    return sum(r['concentration'] or 0 for r in rows) / len(rows)


def _classify_regime(recent_boards, prev_boards,
                     recent_cap, prev_cap,
                     recent_explosion, prev_explosion,
                     recent_conc, prev_conc,
                     evidence: List[str]) -> str:
    """综合判断当前市场风格"""
    scores = {k: 0 for k in MARKET_REGIMES}

    # 连板高度判断
    if recent_boards >= 4:
        scores['连板接力'] += 3
        evidence.append(f"平均最高连板{recent_boards:.1f}(高位活跃)")
    elif recent_boards >= 2.5:
        scores['连板接力'] += 1
        scores['首板轮动'] += 1
    else:
        scores['首板轮动'] += 3
        evidence.append(f"平均最高连板{recent_boards:.1f}(首板为主)")

    # 市值判断
    if recent_cap >= 100:
        scores['大盘价值'] += 3
        evidence.append(f"平均流通市值{recent_cap:.0f}亿(大盘)")
    elif recent_cap >= 50:
        scores['大盘价值'] += 1
        scores['小盘投机'] += 1
    else:
        scores['小盘投机'] += 3
        evidence.append(f"平均流通市值{recent_cap:.0f}亿(小盘)")

    # 炸板率判断
    if recent_explosion < 0.15:
        scores['趋势延续'] += 3
        evidence.append(f"炸板率{recent_explosion:.0%}(封板稳定)")
    elif recent_explosion > 0.30:
        scores['打板套利'] += 3
        evidence.append(f"炸板率{recent_explosion:.0%}(封板不稳)")
    else:
        scores['趋势延续'] += 1
        scores['打板套利'] += 1

    # 概念集中度
    if recent_conc > 0.5:
        scores['连板接力'] += 2
        scores['趋势延续'] += 1
        evidence.append(f"概念集中度{recent_conc:.0%}(主线明确)")
    else:
        scores['首板轮动'] += 2
        scores['小盘投机'] += 1
        evidence.append(f"概念集中度{recent_conc:.0%}(较分散)")

    # 取最高分
    max_score = max(scores.values())
    regime = [k for k, v in scores.items() if v == max_score][0]
    return regime


def _get_regime_adjustments(current: str, prev: Optional[str],
                            is_changed: bool) -> Dict[str, float]:
    """根据风格给出权重调整建议"""
    if not is_changed:
        return {}

    # 不同风格对应的权重调整方向
    adjustments = {
        '连板接力': {
            'board_position': +0.05,
            'concept_heat': +0.03,
            'seal_quality': -0.03,
        },
        '首板轮动': {
            'concept_heat': +0.05,
            'board_position': -0.03,
            'volume_price': +0.03,
        },
        '大盘价值': {
            'cap_fit': +0.05,
            'seal_quality': +0.03,
        },
        '小盘投机': {
            'cap_fit': -0.03,
            'volume_price': +0.05,
        },
        '趋势延续': {
            'seal_quality': +0.05,
            'board_position': +0.03,
        },
        '打板套利': {
            'seal_quality': +0.05,
            'board_position': -0.03,
            'concept_heat': -0.02,
        },
    }
    return adjustments.get(current, {})


# ─────────────────────────── 自动升级入口 ───────────────────────────

def auto_upgrade(db_path: str = DB_PATH, check_only: bool = False) -> Dict[str, Any]:
    """
    自动升级入口 - 每日收盘后自动运行
    
    流程：
    1. 导入 live_tracker 并运行 track_daily
    2. 导入 live_tracker 并运行 evaluate_signals
    3. 检查是否需要调整权重（每5天一次）
    4. 检查是否需要检测风格切换（每10天一次）
    5. 生成升级日志
    """
    init_tables(db_path)
    today = datetime.now().strftime('%Y-%m-%d')
    results = {}

    # 获取最新交易日
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT MAX(date) as d FROM xgt_limit_up_detail").fetchone()
        latest_date = row['d'] if row and row['d'] else today
    finally:
        conn.close()

    logger.info(f"===== 自动升级开始 | {today} | 数据日期: {latest_date} =====")

    # 1. 运行每日跟踪
    try:
        # 延迟导入避免循环依赖
        import live_tracker
        tracking_result = live_tracker.track_daily(latest_date, db_path)
        results['tracking'] = {
            'status': 'success',
            'recommendations': tracking_result['recommendations_count'],
            'correct': tracking_result['correct_count'],
            'win_rate': tracking_result['win_rate'],
        }
    except Exception as e:
        logger.error(f"每日跟踪失败: {e}")
        results['tracking'] = {'status': 'error', 'error': str(e)}

    # 2. 运行信号验证
    try:
        import live_tracker
        signal_result = live_tracker.evaluate_signals(latest_date, db_path)
        results['signals'] = {
            'status': 'success',
            'triggered': len(signal_result['signals_triggered']),
            'verified': len(signal_result.get('newly_verified', [])),
        }
    except Exception as e:
        logger.error(f"信号验证失败: {e}")
        results['signals'] = {'status': 'error', 'error': str(e)}

    # 3. 检查是否需要调整权重（每5天一次）
    need_weight_adjust = _should_run_task('weight_adjust', 5, db_path)
    if need_weight_adjust:
        logger.info("执行权重调整...")
        try:
            adjust_result = adjust_weights(db_path, check_only=check_only)
            results['weight_adjust'] = {
                'status': 'success',
                'adjustments': len(adjust_result.get('adjustments', [])),
                'reason': adjust_result.get('reason', ''),
            }
        except Exception as e:
            logger.error(f"权重调整失败: {e}")
            results['weight_adjust'] = {'status': 'error', 'error': str(e)}
    else:
        results['weight_adjust'] = {'status': 'skipped', 'reason': '未到调整周期(每5天)'}

    # 4. 检查是否需要检测风格切换（每10天一次）
    need_regime_detect = _should_run_task('regime_detect', 10, db_path)
    if need_regime_detect:
        logger.info("执行风格切换检测...")
        try:
            regime_result = detect_regime_change(db_path, check_only=check_only)
            results['regime_detect'] = {
                'status': 'success',
                'current_regime': regime_result.get('current_regime', '未知'),
                'is_changed': regime_result.get('is_changed', False),
                'evidence': regime_result.get('evidence', []),
            }
        except Exception as e:
            logger.error(f"风格检测失败: {e}")
            results['regime_detect'] = {'status': 'error', 'error': str(e)}
    else:
        results['regime_detect'] = {'status': 'skipped', 'reason': '未到检测周期(每10天)'}

    # 5. 记录升级日志
    conn = get_conn(db_path)
    try:
        conn.execute("""
            INSERT INTO upgrade_log (upgrade_date, upgrade_type, details, status)
            VALUES (?, ?, ?, ?)
        """, (
            today,
            'auto_upgrade',
            json.dumps(results, ensure_ascii=False, default=str),
            'completed'
        ))
        conn.commit()
    finally:
        conn.close()

    logger.info(f"===== 自动升级完成 =====")
    return results


def _should_run_task(task_type: str, interval_days: int, db_path: str) -> bool:
    """检查某个任务是否到了运行周期"""
    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT MAX(upgrade_date) as last_run FROM upgrade_log
            WHERE upgrade_type = ?
        """, (task_type,)).fetchone()
        if not row or not row['last_run']:
            return True
        last_date = datetime.strptime(row['last_run'], '%Y-%m-%d')
        return (datetime.now() - last_date).days >= interval_days
    except Exception:
        return True
    finally:
        conn.close()


# ─────────────────────────── 输出格式化 ───────────────────────────

def format_upgrade_report(results: Dict) -> str:
    """格式化升级报告"""
    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  🔧 自适应升级报告 | {datetime.now().strftime('%Y-%m-%d')}")
    lines.append(f"{'='*60}")
    lines.append("")

    # 跟踪结果
    tracking = results.get('tracking', {})
    lines.append("📊 每日跟踪:")
    if tracking.get('status') == 'success':
        lines.append(f"  推荐: {tracking.get('recommendations', 0)}只")
        lines.append(f"  命中: {tracking.get('correct', 0)}只")
        lines.append(f"  胜率: {tracking.get('win_rate', 0):.1%}")
    else:
        lines.append(f"  状态: {tracking.get('status', '未知')}")
    lines.append("")

    # 信号结果
    signals = results.get('signals', {})
    lines.append("📡 信号验证:")
    if signals.get('status') == 'success':
        lines.append(f"  触发信号: {signals.get('triggered', 0)}个")
        lines.append(f"  新验证: {signals.get('verified', 0)}个")
    else:
        lines.append(f"  状态: {signals.get('status', '未知')}")
    lines.append("")

    # 权重调整
    weight_adj = results.get('weight_adjust', {})
    lines.append("⚖️ 权重调整:")
    if weight_adj.get('status') == 'success':
        lines.append(f"  调整项: {weight_adj.get('adjustments', 0)}项")
        lines.append(f"  说明: {weight_adj.get('reason', '')}")
    else:
        lines.append(f"  状态: {weight_adj.get('status', '未知')}")
    lines.append("")

    # 风格检测
    regime = results.get('regime_detect', {})
    lines.append("🔄 风格检测:")
    if regime.get('status') == 'success':
        lines.append(f"  当前风格: {regime.get('current_regime', '未知')}")
        if regime.get('is_changed'):
            lines.append(f"  ⚠️ 检测到风格切换!")
        if regime.get('evidence'):
            for e in regime['evidence']:
                lines.append(f"  · {e}")
    else:
        lines.append(f"  状态: {regime.get('status', '未知')}")
    lines.append("")

    lines.append(f"{'='*60}")
    return '\n'.join(lines)


def format_accuracy_report(accuracy: Dict) -> str:
    """格式化准确性分析报告"""
    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  📈 预测准确性分析报告")
    lines.append(f"{'='*60}")
    lines.append("")

    if accuracy.get('error'):
        lines.append(f"  错误: {accuracy['error']}")
        return '\n'.join(lines)

    period = accuracy.get('period', ('N/A', 'N/A'))
    lines.append(f"  分析区间: {period[0]} ~ {period[1]}")
    lines.append(f"  总推荐数: {accuracy['total_recommendations']}")
    lines.append(f"  总胜率:   {accuracy['overall_win_rate']:.1%}")
    lines.append("")

    # 分段胜率
    by_score = accuracy.get('by_score_range', {})
    if by_score:
        lines.append("  分段胜率:")
        for range_name, stats in by_score.items():
            lines.append(f"    {range_name}: {stats['correct']}/{stats['total']} "
                        f"({stats['win_rate']:.0%})")
        lines.append("")

    # 概念胜率
    by_concept = accuracy.get('by_concept', {})
    if by_concept:
        lines.append("  概念胜率:")
        sorted_concepts = sorted(by_concept.items(),
                                key=lambda x: x[1]['win_rate'], reverse=True)
        for concept, stats in sorted_concepts[:10]:
            lines.append(f"    {concept}: {stats['correct']}/{stats['total']} "
                        f"({stats['win_rate']:.0%})")
        lines.append("")

    # 连板层级胜率
    by_board = accuracy.get('by_board_level', {})
    if by_board:
        lines.append("  连板层级胜率:")
        for level, stats in by_board.items():
            lines.append(f"    {level}: {stats['correct']}/{stats['total']} "
                        f"({stats['win_rate']:.0%})")
        lines.append("")

    # 校准度
    calibration = accuracy.get('calibration', {})
    if calibration:
        lines.append("  胜率校准度:")
        for bucket, stats in calibration.items():
            lines.append(f"    预估{bucket}: "
                        f"实际{stats['actual_win_rate']:.0%} "
                        f"(样本{stats['count']})")
        lines.append("")

    # 维度相关性
    dim_corr = accuracy.get('dimension_correlation', {})
    if dim_corr:
        lines.append("  维度区分度:")
        for dim, corr in dim_corr.items():
            strength = '强' if corr > 0.20 else '中' if corr > 0.10 else '弱'
            lines.append(f"    {dim}: {corr:.2f} ({strength})")

    lines.append("")
    lines.append(f"{'='*60}")
    return '\n'.join(lines)


# ─────────────────────────── 主程序 ───────────────────────────

def main():
    parser = argparse.ArgumentParser(description='自适应升级系统')
    parser.add_argument('--check-only', action='store_true',
                        help='仅检查不调整')
    parser.add_argument('--analyze', action='store_true',
                        help='仅分析准确性')
    parser.add_argument('--adjust', action='store_true',
                        help='仅调整权重')
    parser.add_argument('--regime', action='store_true',
                        help='仅检测风格切换')
    parser.add_argument('--days', type=int, default=30,
                        help='分析天数 (默认30)')
    parser.add_argument('--db', type=str, default=DB_PATH,
                        help='数据库路径')
    args = parser.parse_args()

    db_path = args.db
    init_tables(db_path)

    if args.analyze:
        # 仅分析模式
        accuracy = analyze_prediction_accuracy(days=args.days, db_path=db_path)
        report = format_accuracy_report(accuracy)
        print(report)

    elif args.adjust:
        # 仅调整权重
        result = adjust_weights(db_path, check_only=args.check_only)
        if result.get('adjustments'):
            print(f"\n⚖️ 权重调整结果:")
            print(f"  总胜率: {result.get('overall_win_rate', 0):.1%}")
            for adj in result['adjustments']:
                print(f"  {adj['dimension']}: "
                      f"{adj['old_weight']:.4f} → {adj['new_weight']:.4f}")
                print(f"    原因: {adj['reason']}")
            print(f"\n  新权重: {result['new_weights']}")
        else:
            print("无需调整")

    elif args.regime:
        # 仅风格检测
        result = detect_regime_change(db_path, check_only=args.check_only)
        print(f"\n🔄 风格检测结果:")
        print(f"  当前风格: {result.get('current_regime', '未知')}")
        print(f"  上一风格: {result.get('prev_regime', '未知')}")
        print(f"  是否切换: {'是' if result.get('is_changed') else '否'}")
        if result.get('evidence'):
            print(f"  证据:")
            for e in result['evidence']:
                print(f"    · {e}")

    else:
        # 完整自动升级
        results = auto_upgrade(db_path, check_only=args.check_only)
        report = format_upgrade_report(results)
        print(report)


if __name__ == '__main__':
    main()
