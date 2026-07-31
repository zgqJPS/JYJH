#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立数据库修复脚本
直接操作SQLite数据库文件，绕过所有模块，确保market_advisor所需的表全部存在
"""

import sqlite3
import os
import sys

# 数据库路径：market_advisor的上一级目录
DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "stock_data_1784791326780_0_09ym.db"
)

def fix():
    print(f"=" * 60)
    print(f"数据库修复脚本 v1.0")
    print(f"=" * 60)
    print(f"数据库路径: {DB_PATH}")
    print(f"文件存在: {os.path.exists(DB_PATH)}")

    if not os.path.exists(DB_PATH):
        print(f"[错误] 数据库文件不存在！")
        print(f"请确认文件路径是否正确。")
        return False

    file_size = os.path.getsize(DB_PATH)
    print(f"文件大小: {file_size / 1024 / 1024:.2f} MB")
    print()

    # 步骤1：查看当前所有表
    print("-" * 60)
    print("[步骤1] 查看当前数据库中的所有表...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    existing_tables = [row[0] for row in cursor.fetchall()]
    print(f"当前共有 {len(existing_tables)} 张表:")
    for t in existing_tables:
        print(f"  - {t}")
    print()

    # 步骤2：定义market_advisor需要的所有表
    required_tables = {
        "akshare_limit_up": """
            CREATE TABLE IF NOT EXISTS akshare_limit_up (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                continuous_boards INTEGER DEFAULT 1,
                seal_amount REAL DEFAULT 0,
                seal_style TEXT,
                turnover_rate REAL DEFAULT 0,
                latest_price REAL DEFAULT 0,
                change_percent REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, code)
            )
        """,
        "xgb_limit_up_detail": """
            CREATE TABLE IF NOT EXISTS xgb_limit_up_detail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                concept TEXT,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, code)
            )
        """,
        "concept_statistics": """
            CREATE TABLE IF NOT EXISTS concept_statistics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                concept TEXT NOT NULL,
                count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, concept)
            )
        """,
        "smash_coefficient_results": """
            CREATE TABLE IF NOT EXISTS smash_coefficient_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                smash_coefficient REAL DEFAULT 0,
                max_continuous_boards INTEGER DEFAULT 0,
                limit_up_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "prediction_records": """
            CREATE TABLE IF NOT EXISTS prediction_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                prediction_type TEXT NOT NULL,
                content TEXT,
                confidence REAL DEFAULT 0,
                actual_result TEXT,
                accuracy_score REAL DEFAULT 0,
                verified INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "model_weights": """
            CREATE TABLE IF NOT EXISTS model_weights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factor_name TEXT NOT NULL UNIQUE,
                weight REAL DEFAULT 0,
                history TEXT,
                consecutive_misses INTEGER DEFAULT 0,
                credibility REAL DEFAULT 1.0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "market_knowledge": """
            CREATE TABLE IF NOT EXISTS market_knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT NOT NULL,
                description TEXT,
                occurrence_count INTEGER DEFAULT 0,
                success_rate REAL DEFAULT 0,
                last_occurrence TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "correction_log": """
            CREATE TABLE IF NOT EXISTS correction_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                trigger TEXT,
                factor_name TEXT,
                old_weight REAL DEFAULT 0,
                new_weight REAL DEFAULT 0,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "daily_snapshot": """
            CREATE TABLE IF NOT EXISTS daily_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                limit_up_count INTEGER DEFAULT 0,
                max_continuous_boards INTEGER DEFAULT 0,
                avg_seal_amount REAL DEFAULT 0,
                smash_coefficient REAL DEFAULT 0,
                market_sentiment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """,
        "backtest_records": """
            CREATE TABLE IF NOT EXISTS backtest_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backtest_date TEXT NOT NULL,
                target_date TEXT NOT NULL,
                prediction_type TEXT,
                predicted_value TEXT,
                actual_value TEXT,
                accuracy REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
    }

    # 步骤3：创建缺失的表
    print("-" * 60)
    print("[步骤2] 检查并创建缺失的表...")
    created = 0
    already_exist = 0

    for table_name, create_sql in required_tables.items():
        if table_name in existing_tables:
            print(f"  [已存在] {table_name}")
            already_exist += 1
        else:
            try:
                cursor.execute(create_sql)
                conn.commit()
                print(f"  [已创建] {table_name} ✓")
                created += 1
            except Exception as e:
                print(f"  [创建失败] {table_name}: {e}")

    print(f"\n结果: {already_exist} 张表已存在, {created} 张表新创建")
    print()

    # 步骤4：验证所有表是否真的存在
    print("-" * 60)
    print("[步骤3] 最终验证 - 查询sqlite_master确认所有表...")
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    final_tables = [row[0] for row in cursor.fetchall()]
    print(f"当前共有 {len(final_tables)} 张表")

    all_ok = True
    for table_name in required_tables:
        if table_name in final_tables:
            print(f"  [✓] {table_name}")
        else:
            print(f"  [✗] {table_name} - 缺失！")
            all_ok = False
    print()

    # 步骤5：测试akshare_limit_up表是否可以读写
    print("-" * 60)
    print("[步骤4] 测试akshare_limit_up表的读写能力...")
    try:
        # 测试查询
        cursor.execute("SELECT COUNT(*) FROM akshare_limit_up")
        count = cursor.fetchone()[0]
        print(f"  当前数据量: {count} 条")

        # 测试插入
        cursor.execute("""
            INSERT OR IGNORE INTO akshare_limit_up (date, code, name, continuous_boards, seal_amount)
            VALUES ('_test_', '000000', '测试', 1, 0)
        """)
        conn.commit()

        # 测试读取插入的数据
        cursor.execute("SELECT COUNT(*) FROM akshare_limit_up WHERE date='_test_'")
        test_count = cursor.fetchone()[0]
        if test_count > 0:
            print(f"  写入测试: 成功 ✓")
            # 清除测试数据
            cursor.execute("DELETE FROM akshare_limit_up WHERE date='_test_'")
            conn.commit()
            print(f"  清理测试数据: 成功 ✓")
        else:
            print(f"  写入测试: 失败 ✗ (插入后读不到)")
            all_ok = False

        # 测试查询日期
        cursor.execute("SELECT DISTINCT date FROM akshare_limit_up ORDER BY date LIMIT 5")
        dates = [row[0] for row in cursor.fetchall()]
        print(f"  可用日期 (前5个): {dates}")

    except Exception as e:
        print(f"  读写测试失败: {e}")
        all_ok = False

    print()

    # 步骤6：查看表结构
    print("-" * 60)
    print("[步骤5] akshare_limit_up 表结构:")
    try:
        cursor.execute("PRAGMA table_info(akshare_limit_up)")
        columns = cursor.fetchall()
        for col in columns:
            print(f"  {col[1]:25s} {col[2]:15s} {'NOT NULL' if col[3] else 'NULL':10s} {'PK' if col[5] else ''}")
    except Exception as e:
        print(f"  获取表结构失败: {e}")

    conn.close()

    # 最终结果
    print()
    print("=" * 60)
    if all_ok:
        print("[成功] 所有表已就绪！数据库修复完成。")
        print("现在可以重启 market_advisor 服务并运行分析了。")
    else:
        print("[失败] 仍有问题，请检查以上错误信息。")
    print("=" * 60)

    return all_ok


if __name__ == "__main__":
    success = fix()
    sys.exit(0 if success else 1)
