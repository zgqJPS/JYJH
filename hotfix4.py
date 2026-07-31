#!/usr/bin/env python3
"""
hotfix4.py - 修复 hotfix3 引入的两个兼容性问题
1. config.py 中 DB_PATH 写死了云端Linux路径 → 改为相对路径自动检测
2. predictor/knowledge_base/smash_coefficient 类名加了V2后缀 → 添加兼容别名
"""
import os
import sys
import shutil
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(SCRIPT_DIR, "backup_" + datetime.now().strftime("%Y%m%d_%H%M%S"))

def log(msg):
    print(f"[hotfix4] {msg}")

def backup_file(filepath):
    """备份文件"""
    if os.path.exists(filepath):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dst = os.path.join(BACKUP_DIR, os.path.basename(filepath))
        shutil.copy2(filepath, dst)
        log(f"  备份: {os.path.basename(filepath)} → backup/")
        return True
    return False

def fix_config():
    """修复 config.py 的 DB_PATH"""
    config_path = os.path.join(SCRIPT_DIR, "config.py")
    if not os.path.exists(config_path):
        log("  [跳过] config.py 不存在")
        return False
    
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # 检查是否已经是修复后的版本
    if '_db_candidates' in content:
        log("  [跳过] config.py 已经是修复版本")
        return True
    
    backup_file(config_path)
    
    # 精确替换 DB_PATH 行（处理V2硬编码路径和原始相对路径两种情况）
    new_db_block = '''# 数据库路径 - 自动检测（兼容多环境）
_db_candidates = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_data_1784791326780_0_09ym.db"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stock_data_1784791326780_0_09ym.db"),
]
DB_PATH = None
for _p in _db_candidates:
    if os.path.exists(_p):
        DB_PATH = _p
        break
if DB_PATH is None:
    DB_PATH = _db_candidates[0]'''
    
    # 情况1: V2硬编码路径
    old_line = 'DB_PATH = "/app/data/所有对话/主对话/stock_data_1784791326780_0_09ym.db"'
    if old_line in content:
        content = content.replace(old_line, new_db_block)
        log("  [匹配] V2硬编码路径，已替换")
    else:
        # 情况2: 原始多行 os.path.join 格式
        import re
        pattern = r'DB_PATH\s*=\s*os\.path\.join\([^)]+\)'
        if re.search(pattern, content, re.DOTALL):
            content = re.sub(pattern, new_db_block, content, flags=re.DOTALL)
            log("  [匹配] 原始os.path.join格式，已替换")
        else:
            log("  [警告] 未匹配到已知DB_PATH格式，手动检查config.py")
            return False
    
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    log("  [完成] config.py DB_PATH 已修复为相对路径自动检测")
    return True

def fix_predictor():
    """修复 predictor.py - 添加 Predictor = PredictorV2 别名"""
    filepath = os.path.join(SCRIPT_DIR, "predictor.py")
    if not os.path.exists(filepath):
        log("  [跳过] predictor.py 不存在")
        return False
    
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    
    if 'Predictor = PredictorV2' in content:
        log("  [跳过] predictor.py 已有兼容别名")
        return True
    
    if 'class PredictorV2' not in content:
        log("  [跳过] predictor.py 不是V2版本")
        return True
    
    backup_file(filepath)
    
    # 在文件末尾添加兼容别名
    alias_code = '''

# ===== 向后兼容别名 =====
Predictor = PredictorV2
'''
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(alias_code)
    
    log("  [完成] predictor.py 添加 Predictor = PredictorV2")
    return True

def fix_knowledge_base():
    """修复 knowledge_base.py - 添加 KnowledgeBase = KnowledgeBaseV2 别名"""
    filepath = os.path.join(SCRIPT_DIR, "knowledge_base.py")
    if not os.path.exists(filepath):
        log("  [跳过] knowledge_base.py 不存在")
        return False
    
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    
    if 'KnowledgeBase = KnowledgeBaseV2' in content:
        log("  [跳过] knowledge_base.py 已有兼容别名")
        return True
    
    if 'class KnowledgeBaseV2' not in content:
        log("  [跳过] knowledge_base.py 不是V2版本")
        return True
    
    backup_file(filepath)
    
    alias_code = '''

# ===== 向后兼容别名 =====
KnowledgeBase = KnowledgeBaseV2
'''
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(alias_code)
    
    log("  [完成] knowledge_base.py 添加 KnowledgeBase = KnowledgeBaseV2")
    return True

def fix_smash_coefficient():
    """修复 smash_coefficient.py - 添加 SmashCoefficientCalculator = SmashCoefficientCalculatorV2 别名"""
    filepath = os.path.join(SCRIPT_DIR, "smash_coefficient.py")
    if not os.path.exists(filepath):
        log("  [跳过] smash_coefficient.py 不存在")
        return False
    
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    
    if 'SmashCoefficientCalculator = SmashCoefficientCalculatorV2' in content:
        log("  [跳过] smash_coefficient.py 已有兼容别名")
        return True
    
    if 'class SmashCoefficientCalculatorV2' not in content:
        log("  [跳过] smash_coefficient.py 不是V2版本")
        return True
    
    backup_file(filepath)
    
    alias_code = '''

# ===== 向后兼容别名 =====
SmashCoefficientCalculator = SmashCoefficientCalculatorV2
'''
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(alias_code)
    
    log("  [完成] smash_coefficient.py 添加 SmashCoefficientCalculator = SmashCoefficientCalculatorV2")
    return True

def verify():
    """验证修复结果"""
    log("\n========== 验证修复 ==========")
    ok = True
    
    # 验证1: config.py DB_PATH
    try:
        # 重新加载config模块
        import importlib
        if 'config' in sys.modules:
            del sys.modules['config']
        sys.path.insert(0, SCRIPT_DIR)
        from config import DB_PATH
        log(f"  DB_PATH = {DB_PATH}")
        if '/app/data' in str(DB_PATH):
            log("  [失败] DB_PATH 仍为云端路径！")
            ok = False
        else:
            log("  [通过] DB_PATH 路径正确")
    except Exception as e:
        log(f"  [失败] config 导入异常: {e}")
        ok = False
    
    # 验证2: predictor 导入
    try:
        if 'predictor' in sys.modules:
            del sys.modules['predictor']
        from predictor import Predictor, PredictorV2
        assert Predictor is PredictorV2, "Predictor 不是 PredictorV2 的别名"
        log("  [通过] from predictor import Predictor ✓")
    except Exception as e:
        log(f"  [失败] predictor 导入: {e}")
        ok = False
    
    # 验证3: knowledge_base 导入
    try:
        if 'knowledge_base' in sys.modules:
            del sys.modules['knowledge_base']
        from knowledge_base import KnowledgeBase, KnowledgeBaseV2
        assert KnowledgeBase is KnowledgeBaseV2
        log("  [通过] from knowledge_base import KnowledgeBase ✓")
    except Exception as e:
        log(f"  [失败] knowledge_base 导入: {e}")
        ok = False
    
    # 验证4: smash_coefficient 导入
    try:
        if 'smash_coefficient' in sys.modules:
            del sys.modules['smash_coefficient']
        from smash_coefficient import SmashCoefficientCalculator, SmashCoefficientCalculatorV2
        assert SmashCoefficientCalculator is SmashCoefficientCalculatorV2
        log("  [通过] from smash_coefficient import SmashCoefficientCalculator ✓")
    except Exception as e:
        log(f"  [失败] smash_coefficient 导入: {e}")
        ok = False
    
    # 验证5: main.py 可以导入
    try:
        if 'main' in sys.modules:
            del sys.modules['main']
        from main import run_daily
        log("  [通过] from main import run_daily ✓")
    except Exception as e:
        log(f"  [失败] main 导入: {e}")
        ok = False
    
    return ok

def main():
    print("=" * 50)
    print("hotfix4.py - Market Advisor 兼容性修复")
    print("=" * 50)
    print(f"工作目录: {SCRIPT_DIR}")
    print()
    
    # 修复
    log("\n[1/4] 修复 config.py DB_PATH...")
    fix_config()
    
    log("\n[2/4] 修复 predictor.py 兼容别名...")
    fix_predictor()
    
    log("\n[3/4] 修复 knowledge_base.py 兼容别名...")
    fix_knowledge_base()
    
    log("\n[4/4] 修复 smash_coefficient.py 兼容别名...")
    fix_smash_coefficient()
    
    # 验证
    ok = verify()
    
    print()
    if ok:
        print("=" * 50)
        print("✅ 所有修复通过！请重新启动 app.py")
        print("=" * 50)
    else:
        print("=" * 50)
        print("⚠️ 部分修复未通过，请检查上方日志")
        print("=" * 50)
    
    if BACKUP_DIR and os.path.exists(BACKUP_DIR):
        print(f"\n备份目录: {BACKUP_DIR}")

if __name__ == "__main__":
    main()
