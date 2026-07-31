#!/usr/bin/env python3
"""
hotfix5.py - 修复 hotfix4 引入的问题
1. config.py 被 hotfix4 的正则搞坏（嵌套括号匹配错误导致 unmatched ')'）
2. hotfix4 的V2别名逻辑不适用于原始版本文件（本来就不需要修）

hotfix5 的策略：
- 先从备份恢复 config.py，再用安全方式修复 DB_PATH
- 不修改 predictor.py / knowledge_base.py / smash_coefficient.py（它们本来就没问题）
"""
import os
import sys
import shutil
import glob
import re
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def log(msg):
    print(f"[hotfix5] {msg}")

def find_backup_config():
    """找到 hotfix4 备份的 config.py"""
    backup_dirs = sorted(glob.glob(os.path.join(SCRIPT_DIR, "backup_*")), reverse=True)
    for d in backup_dirs:
        config_backup = os.path.join(d, "config.py")
        if os.path.exists(config_backup):
            return config_backup
    return None

def restore_from_backup():
    """从 hotfix4 的备份恢复 config.py"""
    backup_path = find_backup_config()
    if not backup_path:
        log("[警告] 未找到 hotfix4 的备份文件")
        return False
    
    config_path = os.path.join(SCRIPT_DIR, "config.py")
    
    # 备份当前损坏的版本
    backup_current = os.path.join(SCRIPT_DIR, f"broken_config_{datetime.now().strftime('%H%M%S')}.py")
    if os.path.exists(config_path):
        shutil.copy2(config_path, backup_current)
        log(f"  当前损坏版本已备份: {os.path.basename(backup_current)}")
    
    # 恢复
    shutil.copy2(backup_path, config_path)
    log(f"  已从备份恢复 config.py (来源: {os.path.basename(os.path.dirname(backup_path))})")
    return True

def fix_config_db_path():
    """安全修复 DB_PATH — 不使用正则，直接字符串替换"""
    config_path = os.path.join(SCRIPT_DIR, "config.py")
    
    with open(config_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    # 已经是修复版本则跳过
    full_text = "".join(lines)
    if '_db_candidates' in full_text:
        log("  [跳过] config.py 已经是修复版本")
        return True
    
    # 找到 DB_PATH 定义的行（可能是单行或多行 os.path.join）
    new_block = [
        "# 数据库路径 - 自动检测（兼容多环境）\n",
        "_db_candidates = [\n",
        "    os.path.join(os.path.dirname(os.path.abspath(__file__)), \"stock_data_1784791326780_0_09ym.db\"),\n",
        "    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), \"stock_data_1784791326780_0_09ym.db\"),\n",
        "]\n",
        "DB_PATH = None\n",
        "for _p in _db_candidates:\n",
        "    if os.path.exists(_p):\n",
        "        DB_PATH = _p\n",
        "        break\n",
        "if DB_PATH is None:\n",
        "    DB_PATH = _db_candidates[0]\n",
    ]
    
    # 逐行扫描，找到 DB_PATH 开头的位置，然后判断跨几行
    new_lines = []
    i = 0
    found = False
    while i < len(lines):
        line = lines[i]
        
        # 检测 DB_PATH = os.path.join(...) 模式
        if re.match(r'^DB_PATH\s*=\s*os\.path\.join\(', line):
            found = True
            # 计算括号平衡，找到完整的 os.path.join 结束位置
            paren_count = 0
            j = i
            while j < len(lines):
                for ch in lines[j]:
                    if ch == '(':
                        paren_count += 1
                    elif ch == ')':
                        paren_count -= 1
                if paren_count <= 0:
                    break
                j += 1
            
            # 插入新的 DB_PATH 块
            new_lines.extend(new_block)
            # 跳过原来的多行定义（从 i 到 j）
            i = j + 1
            log("  [匹配] os.path.join 多行格式，已安全替换")
            break
        elif 'DB_PATH = "/app/data' in line or "DB_PATH = '/app/data" in line:
            found = True
            new_lines.extend(new_block)
            i += 1
            log("  [匹配] 硬编码云端路径，已替换")
            break
        else:
            new_lines.append(line)
            i += 1
    
    if not found:
        log("  [警告] 未找到 DB_PATH 定义行")
        return False
    
    # 把剩余的行追加
    new_lines.extend(lines[i:])
    
    # 写回
    with open(config_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    
    log("  [完成] config.py DB_PATH 已修复为相对路径自动检测")
    return True

def verify():
    """验证修复结果"""
    log("\n========== 验证修复 ==========")
    ok = True
    
    # 确保在项目目录
    sys.path.insert(0, SCRIPT_DIR)
    
    # 清除缓存模块
    for mod in list(sys.modules.keys()):
        if mod in ('config', 'predictor', 'knowledge_base', 'smash_coefficient', 'db', 'main'):
            del sys.modules[mod]
    
    # 1. 验证 config.py
    try:
        from config import DB_PATH
        log(f"  config.py ✓  DB_PATH = {DB_PATH}")
        if '/app/data' in str(DB_PATH):
            log("  [失败] DB_PATH 仍为云端路径！")
            ok = False
        else:
            log("  [通过] DB_PATH 路径正确")
    except SyntaxError as e:
        log(f"  [失败] config.py 语法错误: {e}")
        ok = False
    except Exception as e:
        log(f"  [失败] config.py 导入异常: {e}")
        ok = False
    
    # 2. 验证 predictor.py（不需要修，直接导入）
    try:
        from predictor import Predictor
        log(f"  predictor.py ✓  class Predictor")
    except Exception as e:
        log(f"  [失败] predictor.py: {e}")
        ok = False
    
    # 3. 验证 knowledge_base.py
    try:
        from knowledge_base import KnowledgeBase
        log(f"  knowledge_base.py ✓  class KnowledgeBase")
    except Exception as e:
        log(f"  [失败] knowledge_base.py: {e}")
        ok = False
    
    # 4. 验证 smash_coefficient.py
    try:
        from smash_coefficient import SmashCoefficientCalculator
        log(f"  smash_coefficient.py ✓  class SmashCoefficientCalculator")
    except Exception as e:
        log(f"  [失败] smash_coefficient.py: {e}")
        ok = False
    
    # 5. 验证 main.py 导入链
    try:
        # 清除所有相关模块缓存
        for mod in list(sys.modules.keys()):
            if mod in ('config', 'predictor', 'knowledge_base', 'smash_coefficient', 
                       'db', 'main', 'data_fetcher', 'market_analyzer', 
                       'pattern_recognizer', 'prediction_tracker', 'self_corrector', 'reporter'):
                del sys.modules[mod]
        
        from config import DB_PATH as dp
        from predictor import Predictor
        from knowledge_base import KnowledgeBase
        from smash_coefficient import SmashCoefficientCalculator
        log(f"  main.py 导入链 ✓")
    except Exception as e:
        log(f"  [失败] main.py 导入链: {e}")
        ok = False
    
    return ok

def main():
    log("=" * 50)
    log("hotfix5 - 修复 hotfix4 的问题")
    log("=" * 50)
    
    # 步骤1: 从备份恢复 config.py
    log("\n[1/2] 恢复 config.py ...")
    if not restore_from_backup():
        log("[错误] 无法恢复 config.py，请手动从 backup_ 目录复制")
        return
    
    # 步骤2: 安全修复 DB_PATH
    log("\n[2/2] 修复 DB_PATH ...")
    if not fix_config_db_path():
        log("[错误] DB_PATH 修复失败")
        return
    
    # 验证
    if verify():
        log("\n✓ 所有验证通过！可以正常运行 python app.py 了")
    else:
        log("\n⚠️ 部分验证未通过，请检查上方日志")

if __name__ == "__main__":
    main()
