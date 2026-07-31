"""
数据库诊断脚本 - 用于排查 akshare_limit_up 表创建问题
运行方式: python check_db.py
"""
import sqlite3
import os
import sys

# 数据库路径（和 config.py 保持一致）
DB_PATH = r"C:\Users\15624\Desktop\软件\stock_data_1784791326780_0_09ym.db"

print("=" * 60)
print("数据库诊断工具")
print("=" * 60)

# 1. 检查文件是否存在
print(f"\n[1] 数据库文件路径: {DB_PATH}")
print(f"    文件存在: {os.path.exists(DB_PATH)}")
if os.path.exists(DB_PATH):
    print(f"    文件大小: {os.path.getsize(DB_PATH) / 1024:.1f} KB")
    print(f"    可写权限: {os.access(DB_PATH, os.W_OK)}")

# 2. 连接数据库并列出所有表
print(f"\n[2] 连接数据库...")
try:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    print(f"    当前共有 {len(tables)} 张表:")
    for t in tables:
        cursor.execute(f"PRAGMA table_info({t})")
        cols = cursor.fetchall()
        print(f"      - {t} ({len(cols)}列)")
    
    # 3. 检查关键表
    print(f"\n[3] 检查 market_advisor 所需的关键表:")
    required = ['akshare_limit_up', 'prediction_records', 'model_weights',
                'smash_coefficient_results', 'xgb_limit_up_detail', 
                'concept_statistics', 'daily_snapshot']
    for t in required:
        if t in tables:
            cursor.execute(f"SELECT COUNT(*) FROM {t}")
            count = cursor.fetchone()[0]
            print(f"      ✅ {t} (数据量: {count}条)")
        else:
            print(f"      ❌ {t} - 不存在!")
    
    # 4. 尝试创建 akshare_limit_up 表
    print(f"\n[4] 尝试创建 akshare_limit_up 表...")
    try:
        cursor.execute("""CREATE TABLE IF NOT EXISTS akshare_limit_up (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            continuous_boards INTEGER DEFAULT 1,
            seal_amount REAL DEFAULT 0,
            seal_style TEXT DEFAULT '',
            turnover_rate REAL DEFAULT 0,
            latest_price REAL DEFAULT 0,
            change_percent REAL DEFAULT 0,
            UNIQUE(date, code)
        )""")
        conn.commit()
        print(f"    ✅ 表创建/验证成功!")
        
        # 验证
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='akshare_limit_up'")
        result = cursor.fetchone()
        if result:
            print(f"    ✅ 验证通过: akshare_limit_up 表确实存在")
        else:
            print(f"    ❌ 验证失败: 表创建后立即查询不到!")
    except Exception as e:
        print(f"    ❌ 创建失败: {e}")
    
    # 5. 测试数据插入
    print(f"\n[5] 测试数据插入...")
    try:
        cursor.execute("""INSERT OR REPLACE INTO akshare_limit_up 
            (date, code, name, continuous_boards, seal_amount, turnover_rate, latest_price, change_percent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-07-23", "000001", "测试股票", 1, 1.0, 5.0, 10.0, 10.0))
        conn.commit()
        cursor.execute("SELECT COUNT(*) FROM akshare_limit_up WHERE date='2026-07-23'")
        count = cursor.fetchone()[0]
        print(f"    ✅ 插入测试数据成功 (查询到{count}条)")
        
        # 清除测试数据
        cursor.execute("DELETE FROM akshare_limit_up WHERE date='2026-07-23' AND code='000001'")
        conn.commit()
        print(f"    ✅ 测试数据已清除")
    except Exception as e:
        print(f"    ❌ 插入失败: {e}")
    
    conn.close()
    print(f"\n[完成] 数据库连接已关闭")
    
except Exception as e:
    print(f"    ❌ 连接失败: {e}")

print("\n" + "=" * 60)
print("诊断完成")
print("=" * 60)
