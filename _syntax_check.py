# -*- coding: utf-8 -*-
"""临时语法检查脚本：编译核心文件并把结果写入 _syntax_result.txt（自诊断版）"""
import sys
import os
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "_syntax_result.txt")


def main():
    lines = []
    lines.append("python: " + sys.version.replace("\n", " "))
    lines.append("cwd: " + os.getcwd())
    lines.append("base: " + BASE)
    import py_compile
    files = ["smart_recommender.py", "entry_certainty_analyzer.py",
             "app.py", "main.py", "turning_point_detector.py"]
    ok_all = True
    for fn in files:
        path = os.path.join(BASE, fn)
        if not os.path.exists(path):
            lines.append(f"[MISSING] {fn}")
            continue
        try:
            py_compile.compile(path, doraise=True)
            lines.append(f"[OK] {fn}")
        except py_compile.PyCompileError as e:
            ok_all = False
            lines.append(f"[ERROR] {fn}")
            lines.append(str(e)[:4000])
        except Exception as e:
            ok_all = False
            lines.append(f"[ERROR] {fn}: {type(e).__name__}: {e}")
    lines.append("ALL_OK" if ok_all else "HAS_ERROR")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("CHECK_DONE")


try:
    main()
except Exception:
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("FATAL:\n" + traceback.format_exc())
    except Exception:
        pass
    print("CHECK_FATAL")
