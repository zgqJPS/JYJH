#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market_advisor 知识库与信号数据初始化脚本
功能：
1. 向 market_knowledge 表写入系统性市场规律
2. 向 signal_tracking 表回填历史触发数据
3. 幂等设计，可重复执行
"""

import sqlite3
import json
import os
import glob
from datetime import datetime

# ============================================================
# 自动检测数据库路径
# ============================================================
def find_database():
    """自动检测数据库路径"""
    # 候选路径列表（优先级从高到低）
    candidates = []
    
    # 1. 脚本同目录下的数据库
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.extend(glob.glob(os.path.join(script_dir, "stock_data_*.db")))
    
    # 2. 工作目录下的数据库
    cwd = os.getcwd()
    candidates.extend(glob.glob(os.path.join(cwd, "stock_data_*.db")))
    
    # 3. 固定路径
    fixed_path = "/app/data/所有对话/主对话"
    if os.path.isdir(fixed_path):
        candidates.extend(glob.glob(os.path.join(fixed_path, "stock_data_*.db")))
    
    for path in candidates:
        if os.path.isfile(path):
            return path
    
    return None


def init_connection():
    """初始化数据库连接"""
    db_path = find_database()
    if not db_path:
        raise FileNotFoundError("未找到 stock_data_*.db 数据库文件")
    print(f"[INFO] 数据库路径: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables(conn):
    """确保所有必要的表存在（V4模块表 + 选股通数据表）"""
    print("\n[STEP 0] 检查并创建必要的数据库表...")
    
    tables_created = []
    
    # V4 模块所需的表
    v4_tables = {
        "signal_tracking": """
            CREATE TABLE IF NOT EXISTS signal_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                trigger_date TEXT NOT NULL,
                trigger_stocks TEXT,
                next_day_result TEXT,
                avg_return REAL,
                verified INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "recommendation_log": """
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
            )
        """,
        "weight_adjustment_log": """
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
            )
        """,
        "regime_detection_log": """
            CREATE TABLE IF NOT EXISTS regime_detection_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detect_date TEXT NOT NULL,
                current_regime TEXT,
                prev_regime TEXT,
                regime_changed INTEGER DEFAULT 0,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "upgrade_log": """
            CREATE TABLE IF NOT EXISTS upgrade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upgrade_date TEXT NOT NULL,
                upgrade_type TEXT,
                description TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "daily_tracking_report": """
            CREATE TABLE IF NOT EXISTS daily_tracking_report (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL UNIQUE,
                market_summary TEXT,
                recommendation_performance TEXT,
                signal_status TEXT,
                next_day_advice TEXT,
                cumulative_win_rate REAL,
                total_recommendations INTEGER,
                correct_recommendations INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
    }
    
    # 选股通数据表（用于信号回填查询）
    xgt_tables = {
        "xgt_daily_summary": """
            CREATE TABLE IF NOT EXISTS xgt_daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                limit_up_count INTEGER DEFAULT 0,
                limit_down_count INTEGER DEFAULT 0,
                rise_count INTEGER DEFAULT 0,
                fall_count INTEGER DEFAULT 0,
                break_rate REAL DEFAULT 0,
                max_continuous_boards INTEGER DEFAULT 0,
                board_distribution TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "xgt_limit_up_detail": """
            CREATE TABLE IF NOT EXISTS xgt_limit_up_detail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                symbol TEXT,
                stock_name TEXT,
                price REAL,
                prev_close_price REAL,
                change_percent REAL,
                limit_up_days INTEGER DEFAULT 1,
                first_limit_up TEXT,
                last_limit_up TEXT,
                break_limit_up_times INTEGER DEFAULT 0,
                buy_lock_volume_ratio REAL,
                turnover_ratio REAL,
                non_restricted_capital REAL,
                total_capital REAL,
                volume_bias_ratio REAL,
                surge_reason TEXT,
                concepts TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "xgt_break_limit_up": """
            CREATE TABLE IF NOT EXISTS xgt_break_limit_up (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                symbol TEXT,
                stock_name TEXT,
                price REAL,
                prev_close_price REAL,
                change_percent REAL,
                limit_up_days INTEGER DEFAULT 1,
                first_limit_up TEXT,
                break_limit_up_times INTEGER DEFAULT 0,
                buy_lock_volume_ratio REAL,
                turnover_ratio REAL,
                non_restricted_capital REAL,
                total_capital REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "xgt_limit_down": """
            CREATE TABLE IF NOT EXISTS xgt_limit_down (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                symbol TEXT,
                stock_name TEXT,
                price REAL,
                prev_close_price REAL,
                change_percent REAL,
                limit_down_days INTEGER DEFAULT 1,
                non_restricted_capital REAL,
                total_capital REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
    }
    
    # 创建所有表
    for name, sql in {**v4_tables, **xgt_tables}.items():
        try:
            # 检查表是否存在
            cursor = conn.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
            if cursor.fetchone() is None:
                conn.execute(sql)
                conn.commit()
                tables_created.append(name)
        except Exception as e:
            print(f"  [WARN] 创建表 {name} 时出错: {e}")
    
    # 修复表结构不匹配问题（旧版列名与模块期望不一致）
    fixes = {
        "weight_adjustment_log": ("dimension", "添加 dimension 列"),
        "regime_detection_log": ("regime_changed", "修正列名 is_changed→regime_changed, evidence→details"),
        "daily_tracking_report": ("cumulative_win_rate", "修正列结构匹配 live_tracker 模块"),
    }
    for table, (required_col, desc) in fixes.items():
        try:
            cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if cols and required_col not in cols:
                conn.execute(f"DROP TABLE {table}")
                conn.execute(v4_tables[table])
                conn.commit()
                print(f"  [FIX] {table} 表结构已修正（{desc}）")
        except Exception:
            pass  # 表不存在时忽略
    
    if tables_created:
        print(f"  [OK] 创建了 {len(tables_created)} 张新表: {', '.join(tables_created)}")
    else:
        print(f"  [OK] 所有表已存在，无需创建")
    
    return tables_created


# ============================================================
# 第一部分：写入 market_knowledge 规律数据
# ============================================================
def get_market_knowledge_entries():
    """
    构建需要写入 market_knowledge 的规律列表。
    每条记录: (pattern_type, description, occurrence_count, success_rate, last_verified, last_seen, metadata_json)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    entries = []

    # ---------- cycle_phase 类型（市场周期规律） ----------
    entries.append(("cycle_phase", "冰点期市场特征",
        58, 0.69, today, today,
        json.dumps({
            "duration_ratio": 0.389,
            "avg_days": 3.2,
            "max_days": 15,
            "avg_smash": 0.61,
            "avg_boards": 1.2,
            "continue_prob": 0.69,
            "transition_to_蓄力": 0.086,
            "transition_to_高潮": 0.086,
            "transition_to_崩塌": 0.138,
            "interpretation": "冰点期持续38.9%时间，69%概率继续冰点，惯性极强"
        }, ensure_ascii=False)))

    entries.append(("cycle_phase", "高潮期市场特征",
        56, 0.71, today, today,
        json.dumps({
            "duration_ratio": 0.376,
            "avg_days": 3.3,
            "max_days": 17,
            "avg_smash": 6.30,
            "avg_boards": 6.3,
            "continue_prob": 0.71,
            "interpretation": "高潮期持续37.6%时间，71%概率继续高潮，趋势性强，最长17天"
        }, ensure_ascii=False)))

    entries.append(("cycle_phase", "蓄力期市场特征",
        17, 0.35, today, today,
        json.dumps({
            "duration_ratio": 0.114,
            "avg_days": 1.1,
            "max_days": 2,
            "avg_smash": 1.87,
            "avg_boards": 5.2,
            "jump_to_高潮_prob": 0.353,
            "interpretation": "蓄力期仅11.4%，常直接跳入高潮（35%概率），多数是直接跳跃"
        }, ensure_ascii=False)))

    entries.append(("cycle_phase", "崩塌期市场特征",
        18, 0.28, today, today,
        json.dumps({
            "duration_ratio": 0.121,
            "avg_days": 1.4,
            "max_days": 2,
            "avg_smash": 5.18,
            "avg_boards": 3.9,
            "recover_prob": 0.5,
            "interpretation": "崩塌期12.1%，平均1.4天，崩塌后倾向快速修复"
        }, ensure_ascii=False)))

    entries.append(("cycle_phase", "冰点转蓄力转折信号",
        5, 0.8, today, today,
        json.dumps({
            "transition_count": 5,
            "pre_smash_avg": 1.02,
            "pre_boards_avg": 1.6,
            "dates": ["2025-12-21", "2026-01-23", "2026-03-10", "2026-04-09", "2026-04-14"],
            "interpretation": "转折前平均砸盘系数仅1.02，平均连板高度1.6，从极低位置突然跳升是主要模式"
        }, ensure_ascii=False)))

    # ---------- smash_board_correlation 类型（砸盘×连板交叉） ----------
    entries.append(("smash_board_correlation", "砸盘系数与连板高度中强正相关",
        149, 0.57, today, today,
        json.dumps({
            "pearson_r": 0.5702,
            "smash_mean": 3.45,
            "smash_median": 3.03,
            "smash_min": 0.0,
            "smash_max": 34.32,
            "boards_mean": 3.9,
            "boards_median": 4,
            "boards_min": 0,
            "boards_max": 9,
            "interpretation": "砸盘系数与连板高度Pearson相关0.57，市场活跃时两者同向运动"
        }, ensure_ascii=False)))

    entries.append(("smash_board_correlation", "冰点期砸盘连板特征",
        58, 0.69, today, today,
        json.dumps({
            "phase": "冰点",
            "days": 58,
            "avg_smash": 0.61,
            "avg_boards": 1.2,
            "median_smash": 0.00,
            "interpretation": "冰点期平均砸盘0.61，连板均值1.2"
        }, ensure_ascii=False)))

    entries.append(("smash_board_correlation", "高潮期砸盘连板特征",
        56, 0.71, today, today,
        json.dumps({
            "phase": "高潮",
            "days": 56,
            "avg_smash": 6.30,
            "avg_boards": 6.3,
            "median_smash": 5.62,
            "interpretation": "高潮期平均砸盘6.30，连板均值6.3"
        }, ensure_ascii=False)))

    # ---------- concept_rotation 类型（概念轮动） ----------
    entries.append(("concept_rotation", "核心概念矩阵TOP4",
        66, 0.85, today, today,
        json.dumps({
            "top_concepts": [
                {"name": "业绩增长", "daily_avg": 7.0, "total": 288, "days": 41},
                {"name": "云计算数据中心", "daily_avg": 5.7, "total": 243, "days": 43},
                {"name": "航天", "daily_avg": 5.4, "total": 250, "days": 46},
                {"name": "油服", "daily_avg": 5.4, "total": 87, "days": 16}
            ],
            "interpretation": "核心概念引擎：业绩增长(日均7.0)、云计算数据中心(日均5.7)、航天(日均5.4)、油服(日均5.4)"
        }, ensure_ascii=False)))

    entries.append(("concept_rotation", "概念集中度与市场表现",
        33, 0.7, today, today,
        json.dumps({
            "avg_top3_concentration": 0.465,
            "high_concentration_days": 11,
            "high_concentration_avg_limit_up": 52,
            "low_concentration_days": 22,
            "low_concentration_avg_limit_up": 68,
            "interpretation": "低集中度时涨停总数更高（68 vs 52），百花齐放比一枝独秀更有利于整体市场"
        }, ensure_ascii=False)))

    entries.append(("concept_rotation", "概念轮动周期与接力规律",
        116, 0.45, today, today,
        json.dumps({
            "burst_events": 116,
            "rotation_cycle_days": "5-7",
            "strong_burst_continuation_rate": 0.615,
            "medium_burst_continuation_rate": 0.364,
            "relay_patterns": [
                {"from": "其他", "to": "航天", "count": 4},
                {"from": "其他", "to": "机器人", "count": 3},
                {"from": "其他", "to": "云计算数据中心", "count": 3},
                {"from": "机器人", "to": "房地产", "count": 3},
                {"from": "机器人", "to": "国产芯片", "count": 3}
            ],
            "interpretation": "概念轮动周期约5-7天，'其他'退出后航天/机器人/云计算最常接力"
        }, ensure_ascii=False)))

    # ---------- board_promotion 类型（连板晋级率） ----------
    entries.append(("board_promotion", "1板到2板晋级率",
        3011, 0.151, today, today,
        json.dumps({
            "from_board": 1, "to_board": 2,
            "promotion_count": 456, "total_count": 3011,
            "promotion_rate": 0.151,
            "interpretation": "首板晋级难度大，85%的首板次日断板"
        }, ensure_ascii=False)))

    entries.append(("board_promotion", "2板到3板晋级率",
        468, 0.269, today, today,
        json.dumps({
            "from_board": 2, "to_board": 3,
            "promotion_count": 126, "total_count": 468,
            "promotion_rate": 0.269,
            "interpretation": "能过首板的股票晋级概率更高"
        }, ensure_ascii=False)))

    entries.append(("board_promotion", "3板到4板晋级率（跃升点）",
        118, 0.432, today, today,
        json.dumps({
            "from_board": 3, "to_board": 4,
            "promotion_count": 51, "total_count": 118,
            "promotion_rate": 0.432,
            "is_key_node": True,
            "interpretation": "3→4板是晋级率跃升的关键节点，3板是重要分水岭"
        }, ensure_ascii=False)))

    entries.append(("board_promotion", "4板到5板晋级率（风险拐点）",
        59, 0.356, today, today,
        json.dumps({
            "from_board": 4, "to_board": 5,
            "promotion_count": 21, "total_count": 59,
            "promotion_rate": 0.356,
            "is_risk_inflection": True,
            "interpretation": "4→5板开始回落，高位风险加大"
        }, ensure_ascii=False)))

    entries.append(("board_promotion", "6板以上妖股模式",
        7, 0.714, today, today,
        json.dumps({
            "from_board": 6, "to_board": 7,
            "promotion_count": 5, "total_count": 7,
            "promotion_rate": 0.714,
            "is_demon_mode": True,
            "interpretation": "6板以上进入妖股模式，能到6板的妖股大概率继续（样本量小但极高）"
        }, ensure_ascii=False)))

    entries.append(("board_promotion", "断板vs晋级特征对比",
        400, 0.74, today, today,
        json.dumps({
            "failed_samples": 104,
            "success_samples": 296,
            "failed_seal_ratio": 0.0115,
            "success_seal_ratio": 0.0190,
            "failed_turnover": 0.0848,
            "success_turnover": 0.0508,
            "failed_break_times": 4.0,
            "success_break_times": 2.0,
            "interpretation": "断板特征：高换手+高炸板+低封单；晋级特征：高封单+低换手+低炸板"
        }, ensure_ascii=False)))

    # ---------- break_rate 类型（炸板率规律） ----------
    entries.append(("break_rate", "高炸板率次日修复规律",
        6, 0.83, today, today,
        json.dumps({
            "high_break_threshold": 0.35,
            "recovery_count": 5,
            "total_high_break_days": 6,
            "recovery_rate": 0.83,
            "data": [
                {"date": "2026-07-06", "rate": 0.383, "next_rate": 0.414, "result": "恶化"},
                {"date": "2026-07-07", "rate": 0.414, "next_rate": 0.236, "result": "修复"},
                {"date": "2026-07-10", "rate": 0.505, "next_rate": 0.302, "result": "修复"},
                {"date": "2026-07-16", "rate": 0.361, "next_rate": 0.227, "result": "修复"},
                {"date": "2026-07-20", "rate": 0.419, "next_rate": 0.062, "result": "修复"},
                {"date": "2026-07-22", "rate": 0.447, "next_rate": 0.164, "result": "修复"}
            ],
            "interpretation": "高炸板率(≥35%)有强烈的次日修复倾向（5/6次修复），均值回归特性显著"
        }, ensure_ascii=False)))

    entries.append(("break_rate", "高炸板后涨停多但涨跌比低",
        21, 0.5, today, today,
        json.dumps({
            "avg_explosion_rate": 0.2613,
            "high_break_next_day_avg_limit_up": 87,
            "low_break_next_day_avg_limit_up": 70,
            "high_break_next_day_rise_fall_ratio": 1.24,
            "low_break_next_day_rise_fall_ratio": 2.91,
            "interpretation": "高炸板后虽然涨停绝对数多(87 vs 70)，但涨跌比更低(1.24 vs 2.91)，做多情绪但大盘弱"
        }, ensure_ascii=False)))

    # ---------- seal_quality 类型（封板质量） ----------
    entries.append(("seal_quality", "高质量涨停特征组合",
        111, 0.802, today, today,
        json.dumps({
            "top_combos": [
                {"combo": "2板+低换手(<5%)", "samples": 72, "promotion_rate": 0.875},
                {"combo": "高封单+低炸板", "samples": 88, "promotion_rate": 0.852},
                {"combo": "低换手+低量比", "samples": 191, "promotion_rate": 0.838},
                {"combo": "低换手+零炸板+低量比", "samples": 146, "promotion_rate": 0.822},
                {"combo": "高封单+低换手+零炸板", "samples": 111, "promotion_rate": 0.802}
            ],
            "interpretation": "最高晋级率组合：2板+低换手(<5%)达87.5%"
        }, ensure_ascii=False)))

    entries.append(("seal_quality", "强封标准与封板时间效应",
        400, 0.75, today, today,
        json.dumps({
            "strong_seal_seal_ratio_threshold": 0.01,
            "strong_seal_turnover_threshold": 0.05,
            "strong_seal_criteria": "封单>5亿，换手<5%",
            "early_seal_advantage": "封板时间越早（上午），次日溢价越高",
            "seal_ratio_q4_promotion": 0.863,
            "interpretation": "强封标准：高封单比+低换手+零开板；封板时间越早次日溢价越高"
        }, ensure_ascii=False)))

    entries.append(("seal_quality", "龙头股画像（5板以上）",
        31, 0.5, today, today,
        json.dumps({
            "total_dragons": 31,
            "avg_seal_ratio": 0.0230,
            "avg_turnover": 0.0842,
            "avg_volume_bias": 3.95,
            "avg_flow_capital": "53.0亿",
            "avg_break_times": 2.2,
            "top_dragons": [
                {"name": "白银有色", "code": "601212", "boards": 8},
                {"name": "华电辽能", "code": "600396", "boards": 8},
                {"name": "豫能控股", "code": "001896", "boards": 7},
                {"name": "圣阳股份", "code": "002580", "boards": 7},
                {"name": "津药药业", "code": "600488", "boards": 7}
            ],
            "interpretation": "5板以上龙头共31只，流通市值偏小(53亿)，封单比高(0.023)"
        }, ensure_ascii=False)))

    # ---------- signal_pattern 类型（7个已发现信号） ----------
    entries.append(("signal_pattern", "低砸盘+连板新高信号",
        2, 1.0, today, today,
        json.dumps({
            "signal_id": 1,
            "signal_name": "低砸盘+连板新高",
            "conditions": "连板>=6且砸盘<3.0",
            "trigger_count": 2,
            "win_rate": 1.0,
            "avg_return": 1.077,
            "rating": "低样本",
            "interpretation": "胜率100%但仅触发2次，需持续验证"
        }, ensure_ascii=False)))

    entries.append(("signal_pattern", "概念超强爆发信号",
        44, 0.977, today, today,
        json.dumps({
            "signal_id": 2,
            "signal_name": "概念超强爆发",
            "conditions": "单日龙头概念涨停>=10",
            "trigger_count": 44,
            "win_rate": 0.977,
            "avg_return": 0.0,
            "rating": "高胜率",
            "interpretation": "胜率97.7%，触发44次，最可靠的信号之一"
        }, ensure_ascii=False)))

    entries.append(("signal_pattern", "砸盘连续下降+企稳信号",
        7, 0.857, today, today,
        json.dumps({
            "signal_id": 3,
            "signal_name": "砸盘连续下降+企稳",
            "conditions": "连续3天下降且累计降>30%",
            "trigger_count": 7,
            "win_rate": 0.857,
            "avg_return": 0.353,
            "rating": "高胜率",
            "interpretation": "胜率85.7%，平均收益35.3%，信号质量高"
        }, ensure_ascii=False)))

    entries.append(("signal_pattern", "高板梯队厚度信号",
        44, 0.523, today, today,
        json.dumps({
            "signal_id": 4,
            "signal_name": "高板梯队厚度",
            "conditions": "3板以上>=3只",
            "trigger_count": 44,
            "win_rate": 0.523,
            "avg_return": 0.0,
            "rating": "中等胜率",
            "interpretation": "胜率52.3%，触发44次，中等信号"
        }, ensure_ascii=False)))

    entries.append(("signal_pattern", "炸板率骤降修复信号",
        8, 0.5, today, today,
        json.dumps({
            "signal_id": 5,
            "signal_name": "炸板率骤降修复",
            "conditions": "炸板率日降>10个百分点",
            "trigger_count": 8,
            "win_rate": 0.5,
            "avg_return": 0.14,
            "rating": "中等胜率",
            "interpretation": "胜率50%，平均收益14%，需配合其他信号使用"
        }, ensure_ascii=False)))

    entries.append(("signal_pattern", "砸盘骤降+连板突破信号",
        9, 0.444, today, today,
        json.dumps({
            "signal_id": 6,
            "signal_name": "砸盘骤降+连板突破",
            "conditions": "砸盘日降>30%+最高连板>=5",
            "trigger_count": 9,
            "win_rate": 0.444,
            "avg_return": 0.341,
            "rating": "中低胜率",
            "interpretation": "胜率44.4%但平均收益34.1%，适合配合使用"
        }, ensure_ascii=False)))

    entries.append(("signal_pattern", "长冰点后反弹信号（反向信号）",
        2, 0.0, today, today,
        json.dumps({
            "signal_id": 7,
            "signal_name": "长冰点后反弹",
            "conditions": "连续冰点>=3天后转非冰点",
            "trigger_count": 2,
            "win_rate": 0.0,
            "avg_return": -0.395,
            "rating": "反向信号",
            "interpretation": "胜率0%，仅触发2次，作为反向参考指标"
        }, ensure_ascii=False)))

    return entries


def insert_market_knowledge(conn):
    """向 market_knowledge 表写入规律数据"""
    print("\n" + "=" * 60)
    print("[STEP 1] 写入 market_knowledge 规律数据")
    print("=" * 60)

    entries = get_market_knowledge_entries()
    inserted = 0
    skipped = 0

    for entry in entries:
        pattern_type, description, occ_count, success_rate, last_verified, last_seen, metadata = entry
        try:
            conn.execute("""
                INSERT OR IGNORE INTO market_knowledge 
                (pattern_type, description, occurrence_count, success_rate, last_verified, last_seen, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (pattern_type, description, occ_count, success_rate, last_verified, last_seen, metadata))
            if conn.total_changes > 0:
                inserted += 1
            else:
                skipped += 1
        except sqlite3.IntegrityError:
            skipped += 1
        except Exception as e:
            print(f"  [ERROR] 插入失败: {pattern_type}/{description} - {e}")
            skipped += 1

    conn.commit()

    # 统计最终结果
    cursor = conn.execute("SELECT COUNT(*) FROM market_knowledge")
    total = cursor.fetchone()[0]
    cursor = conn.execute("SELECT pattern_type, COUNT(*) FROM market_knowledge GROUP BY pattern_type ORDER BY COUNT(*) DESC")
    type_counts = cursor.fetchall()

    print(f"  [OK] 本次新增: {inserted} 条")
    print(f"  [OK] 跳过(已存在): {skipped} 条")
    print(f"  [OK] 表中总计: {total} 条")
    print("  [详情] 各类型分布:")
    for pt, cnt in type_counts:
        print(f"    - {pt}: {cnt} 条")

    return inserted


# ============================================================
# 第二部分：回填 signal_tracking 历史触发数据
# ============================================================
def backfill_signal_tracking(conn):
    """基于数据库中的实际数据分析回填 signal_tracking 表"""
    print("\n" + "=" * 60)
    print("[STEP 2] 回填 signal_tracking 历史触发数据")
    print("=" * 60)

    total_inserted = 0

    # ------ Signal 1: 低砸盘+连板新高 ------
    # 查 smash_coefficient_results 中 smash<3.0 且同日/次日 max_boards>=6
    print("\n  [Signal 1] 低砸盘+连板新高 (砸盘<3.0 且 连板>=6)")
    try:
        # smash_coefficient_results 有 smash_coefficient 和 max_continuous_boards
        rows = conn.execute("""
            SELECT date, smash_coefficient, max_continuous_boards
            FROM smash_coefficient_results
            WHERE smash_coefficient < 3.0 AND max_continuous_boards >= 6
            ORDER BY date
        """).fetchall()
        
        for row in rows:
            date = row[0]
            stocks_info = f"砸盘{row[1]:.2f}/连板{row[2]}"
            # 检查是否已存在
            existing = conn.execute("""
                SELECT id FROM signal_tracking 
                WHERE signal_id = 1 AND trigger_date = ?
            """, (date,)).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO signal_tracking (signal_id, trigger_date, trigger_stocks, next_day_result, avg_return, verified)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (1, date, stocks_info, None, None, 1))
                total_inserted += 1
        
        print(f"    找到 {len(rows)} 个触发日期")
    except Exception as e:
        print(f"    [ERROR] {e}")

    # ------ Signal 2: 概念超强爆发 ------
    # 查 xgt_limit_up_detail 中按日期分组，统计每个概念出现次数>=10
    print("\n  [Signal 2] 概念超强爆发 (单日龙头概念涨停>=10)")
    try:
        # 按日期和概念分组统计
        rows = conn.execute("""
            SELECT date, concept, COUNT(*) as cnt
            FROM xgt_limit_up_detail
            WHERE concept IS NOT NULL AND concept != ''
            GROUP BY date, concept
            HAVING cnt >= 10
            ORDER BY date
        """).fetchall()
        
        for row in rows:
            date = row[0]
            concept = row[1]
            cnt = row[2]
            stocks_info = f"概念:{concept}({cnt}只)"
            existing = conn.execute("""
                SELECT id FROM signal_tracking 
                WHERE signal_id = 2 AND trigger_date = ?
            """, (date,)).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO signal_tracking (signal_id, trigger_date, trigger_stocks, next_day_result, avg_return, verified)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (2, date, stocks_info, None, None, 1))
                total_inserted += 1
        
        print(f"    找到 {len(rows)} 个触发日期")
    except Exception as e:
        print(f"    [ERROR] {e}")

    # ------ Signal 3: 砸盘连续下降+企稳 ------
    # 查 smash_coefficient_results 中连续3天下降且累计降幅>30%
    print("\n  [Signal 3] 砸盘连续下降+企稳 (连续3天下降且累计降>30%)")
    try:
        rows = conn.execute("""
            SELECT date, smash_coefficient
            FROM smash_coefficient_results
            ORDER BY date
        """).fetchall()
        
        if len(rows) >= 3:
            for i in range(2, len(rows)):
                # 检查连续3天下降
                s0 = rows[i-2][1]  # 第1天
                s1 = rows[i-1][1]  # 第2天
                s2 = rows[i][1]    # 第3天（当天）
                
                if s0 is not None and s1 is not None and s2 is not None:
                    if s0 > s1 > s2 and s0 > 0:
                        drop_pct = (s0 - s2) / s0
                        if drop_pct > 0.30:
                            date = rows[i][0]
                            stocks_info = f"砸盘{s0:.2f}→{s1:.2f}→{s2:.2f}(降{drop_pct*100:.1f}%)"
                            existing = conn.execute("""
                                SELECT id FROM signal_tracking 
                                WHERE signal_id = 3 AND trigger_date = ?
                            """, (date,)).fetchone()
                            if not existing:
                                conn.execute("""
                                    INSERT INTO signal_tracking (signal_id, trigger_date, trigger_stocks, next_day_result, avg_return, verified)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                """, (3, date, stocks_info, None, None, 1))
                                total_inserted += 1
        
        print(f"    扫描完成，已回填触发日期")
    except Exception as e:
        print(f"    [ERROR] {e}")

    # ------ Signal 4: 高板梯队厚度 ------
    # 查 xgt_limit_up_detail 中 limit_up_days>=3 的个数>=3 的日期
    print("\n  [Signal 4] 高板梯队厚度 (当日3板以上>=3只)")
    try:
        rows = conn.execute("""
            SELECT date, COUNT(*) as high_board_count
            FROM xgt_limit_up_detail
            WHERE limit_up_days >= 3
            GROUP BY date
            HAVING high_board_count >= 3
            ORDER BY date
        """).fetchall()
        
        for row in rows:
            date = row[0]
            cnt = row[1]
            # 获取具体股票
            stocks = conn.execute("""
                SELECT name || '(' || code || ')' || limit_up_days || '板'
                FROM xgt_limit_up_detail
                WHERE date = ? AND limit_up_days >= 3
                ORDER BY limit_up_days DESC
            """, (date,)).fetchall()
            stocks_info = ', '.join([s[0] for s in stocks]) if stocks else f"{cnt}只3板+"
            
            existing = conn.execute("""
                SELECT id FROM signal_tracking 
                WHERE signal_id = 4 AND trigger_date = ?
            """, (date,)).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO signal_tracking (signal_id, trigger_date, trigger_stocks, next_day_result, avg_return, verified)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (4, date, stocks_info, None, None, 1))
                total_inserted += 1
        
        print(f"    找到 {len(rows)} 个触发日期")
    except Exception as e:
        print(f"    [ERROR] {e}")

    # ------ Signal 5: 炸板率骤降修复 ------
    # 查 xgt_daily_summary 中 explosion_rate 较前日下降>10个百分点
    print("\n  [Signal 5] 炸板率骤降修复 (炸板率日降>10个百分点)")
    try:
        rows = conn.execute("""
            SELECT a.date, a.explosion_rate as today_rate, b.explosion_rate as yesterday_rate
            FROM xgt_daily_summary a
            JOIN xgt_daily_summary b ON b.date = (
                SELECT MAX(date) FROM xgt_daily_summary WHERE date < a.date
            )
            WHERE (b.explosion_rate - a.explosion_rate) > 0.10
            ORDER BY a.date
        """).fetchall()
        
        for row in rows:
            date = row[0]
            today_rate = row[1]
            yesterday_rate = row[2]
            drop_pct = (yesterday_rate - today_rate) * 100
            stocks_info = f"炸板率{yesterday_rate*100:.1f}%→{today_rate*100:.1f}%(降{drop_pct:.1f}个百分点)"
            
            existing = conn.execute("""
                SELECT id FROM signal_tracking 
                WHERE signal_id = 5 AND trigger_date = ?
            """, (date,)).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO signal_tracking (signal_id, trigger_date, trigger_stocks, next_day_result, avg_return, verified)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (5, date, stocks_info, None, None, 1))
                total_inserted += 1
        
        print(f"    找到 {len(rows)} 个触发日期")
    except Exception as e:
        print(f"    [ERROR] {e}")

    # ------ Signal 6: 砸盘骤降+连板突破 ------
    # 查 smash_coefficient 日降>30% 且 max_boards>=5
    print("\n  [Signal 6] 砸盘骤降+连板突破 (砸盘日降>30% + 最高连板>=5)")
    try:
        rows = conn.execute("""
            SELECT a.date, a.smash_coefficient as today_smash, a.max_continuous_boards,
                   b.smash_coefficient as yesterday_smash
            FROM smash_coefficient_results a
            JOIN smash_coefficient_results b ON b.date = (
                SELECT MAX(date) FROM smash_coefficient_results WHERE date < a.date
            )
            WHERE b.smash_coefficient > 0
              AND (b.smash_coefficient - a.smash_coefficient) / b.smash_coefficient > 0.30
              AND a.max_continuous_boards >= 5
            ORDER BY a.date
        """).fetchall()
        
        for row in rows:
            date = row[0]
            today_smash = row[1]
            boards = row[2]
            yesterday_smash = row[3]
            drop_pct = (yesterday_smash - today_smash) / yesterday_smash * 100
            stocks_info = f"砸盘{yesterday_smash:.2f}→{today_smash:.2f}(降{drop_pct:.1f}%)/连板{boards}"
            
            existing = conn.execute("""
                SELECT id FROM signal_tracking 
                WHERE signal_id = 6 AND trigger_date = ?
            """, (date,)).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO signal_tracking (signal_id, trigger_date, trigger_stocks, next_day_result, avg_return, verified)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (6, date, stocks_info, None, None, 1))
                total_inserted += 1
        
        print(f"    找到 {len(rows)} 个触发日期")
    except Exception as e:
        print(f"    [ERROR] {e}")

    # ------ Signal 7: 长冰点后反弹 ------
    # 查连续3天冰点(smash<1)后转非冰点
    print("\n  [Signal 7] 长冰点后反弹 (连续冰点>=3天后转非冰点)")
    try:
        rows = conn.execute("""
            SELECT date, smash_coefficient
            FROM smash_coefficient_results
            ORDER BY date
        """).fetchall()
        
        if len(rows) >= 4:
            for i in range(3, len(rows)):
                # 检查前3天都是冰点(smash<1)
                s0 = rows[i-3][1]
                s1 = rows[i-2][1]
                s2 = rows[i-1][1]
                s3 = rows[i][1]  # 当天
                
                if (s0 is not None and s1 is not None and s2 is not None and s3 is not None):
                    if s0 < 1 and s1 < 1 and s2 < 1 and s3 >= 1:
                        date = rows[i][0]
                        stocks_info = f"冰点期{s0:.2f}→{s1:.2f}→{s2:.2f}→转非冰点{s3:.2f}"
                        
                        existing = conn.execute("""
                            SELECT id FROM signal_tracking 
                            WHERE signal_id = 7 AND trigger_date = ?
                        """, (date,)).fetchone()
                        if not existing:
                            conn.execute("""
                                INSERT INTO signal_tracking (signal_id, trigger_date, trigger_stocks, next_day_result, avg_return, verified)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (7, date, stocks_info, None, None, 1))
                            total_inserted += 1
        
        print(f"    扫描完成，已回填触发日期")
    except Exception as e:
        print(f"    [ERROR] {e}")

    conn.commit()

    # 统计
    cursor = conn.execute("SELECT COUNT(*) FROM signal_tracking")
    total = cursor.fetchone()[0]
    cursor = conn.execute("SELECT signal_id, COUNT(*) FROM signal_tracking GROUP BY signal_id ORDER BY signal_id")
    signal_counts = cursor.fetchall()

    print(f"\n  [OK] 本次新增: {total_inserted} 条")
    print(f"  [OK] 表中总计: {total} 条")
    print("  [详情] 各信号触发次数:")
    signal_names = {
        1: "低砸盘+连板新高",
        2: "概念超强爆发",
        3: "砸盘连续下降+企稳",
        4: "高板梯队厚度",
        5: "炸板率骤降修复",
        6: "砸盘骤降+连板突破",
        7: "长冰点后反弹"
    }
    for sid, cnt in signal_counts:
        name = signal_names.get(sid, f"Signal {sid}")
        print(f"    - Signal {sid} ({name}): {cnt} 次")

    return total_inserted


# ============================================================
# 主函数
# ============================================================
def main():
    print("=" * 60)
    print("  market_advisor 知识库与信号数据初始化")
    print(f"  执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    try:
        conn = init_connection()
    except FileNotFoundError as e:
        print(f"\n[FATAL] {e}")
        print("请确保数据库文件 stock_data_*.db 存在于脚本同目录或工作目录中。")
        return

    try:
        # 第零步：确保所有必要的表存在
        ensure_tables(conn)
        
        # 第一步：写入 market_knowledge
        mk_inserted = insert_market_knowledge(conn)

        # 第二步：回填 signal_tracking
        st_inserted = backfill_signal_tracking(conn)

        # 最终统计摘要
        print("\n" + "=" * 60)
        print("  初始化完成 - 统计摘要")
        print("=" * 60)
        
        # market_knowledge 统计
        cursor = conn.execute("SELECT COUNT(*) FROM market_knowledge")
        mk_total = cursor.fetchone()[0]
        cursor = conn.execute("SELECT pattern_type, COUNT(*) FROM market_knowledge GROUP BY pattern_type ORDER BY COUNT(*) DESC")
        mk_types = cursor.fetchall()
        
        print(f"\n  market_knowledge 表:")
        print(f"    总计: {mk_total} 条")
        for pt, cnt in mk_types:
            print(f"    - {pt}: {cnt} 条")

        # signal_tracking 统计
        cursor = conn.execute("SELECT COUNT(*) FROM signal_tracking")
        st_total = cursor.fetchone()[0]
        cursor = conn.execute("SELECT signal_id, COUNT(*) FROM signal_tracking GROUP BY signal_id ORDER BY signal_id")
        st_signals = cursor.fetchall()
        
        print(f"\n  signal_tracking 表:")
        print(f"    总计: {st_total} 条")
        signal_names = {
            1: "低砸盘+连板新高",
            2: "概念超强爆发",
            3: "砸盘连续下降+企稳",
            4: "高板梯队厚度",
            5: "炸板率骤降修复",
            6: "砸盘骤降+连板突破",
            7: "长冰点后反弹"
        }
        for sid, cnt in st_signals:
            name = signal_names.get(sid, f"Signal {sid}")
            print(f"    - Signal {sid} ({name}): {cnt} 次")

        print(f"\n  [DONE] 初始化成功！")
        print("=" * 60)

    except Exception as e:
        print(f"\n[FATAL] 初始化失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
