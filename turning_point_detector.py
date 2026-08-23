"""
turning_point_detector.py - 大盘变盘节点与总龙头诞生节点识别
（已修复数据库路径，从 config 导入 DB_PATH）
"""

from __future__ import annotations

import sqlite3
import logging
from typing import Dict, List, Optional

from config import DB_PATH

logger = logging.getLogger("turning_point")


def _get_conn(db_path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ============ 信号阈值 ============
SMASH_LOW = 3.0
SMASH_HIGH = 5.5
SMASH_EXTREME = 7.0
SMASH_DROP_BIG = 3.0
SMASH_DROP_MED = 1.5
BOARD_BOTTOM = 3
BOARD_BREAKOUT = 6
BOARD_TOP = 7


def _load_smash_series(conn: sqlite3.Connection, days: int = 60) -> List[Dict]:
    rows = conn.execute(
        """
        SELECT trade_date as date, smash_coefficient as sc,
               max_continuous_days as max_boards, limit_up_count as lu
        FROM smash_coefficients
        WHERE smash_coefficient IS NOT NULL
        ORDER BY trade_date DESC LIMIT ?
        """, (days,)
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def _load_dragon_series(conn: sqlite3.Connection, days: int = 60) -> Dict[str, Dict]:
    try:
        rows = conn.execute(
            """
            SELECT detect_date as date, code, name, certainty_level as level,
                   total_score as score, lifecycle_stage as lifecycle,
                   limit_up_days as boards, concept
            FROM dragon_detections
            WHERE dragon_type = 'total_dragon'
            ORDER BY detect_date DESC LIMIT ?
            """, (days,)
        ).fetchall()
        return {r["date"]: dict(r) for r in rows}
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            logger.warning("dragon_detections 表不存在，龙头诞生节点将不可用，但变盘节点仍可正常显示")
            return {}
        raise


def _detect_signals(series: List[Dict], idx: int) -> List[Dict]:
    cur = series[idx]
    if idx == 0:
        return []

    prev = series[idx - 1]
    sc_now = cur.get("sc") or 0.0
    sc_prev = prev.get("sc") or 0.0
    mb_now = cur.get("max_boards") or 0
    mb_prev = prev.get("max_boards") or 0
    sc_delta = sc_now - sc_prev

    signals: List[Dict] = []

    if sc_delta < -SMASH_DROP_BIG and mb_now <= BOARD_BOTTOM + 1 and sc_now < SMASH_LOW:
        signals.append({
            "type": "bottom",
            "severity": "strong",
            "name": "冰点见底",
            "detail": f"砸盘{sc_prev:.2f}→{sc_now:.2f}（骤降{abs(sc_delta):.2f}），最高{mb_now}板，抛压释放"
        })
    elif sc_delta < -SMASH_DROP_MED and mb_now <= BOARD_BOTTOM and sc_now < SMASH_LOW:
        signals.append({
            "type": "bottom",
            "severity": "medium",
            "name": "分歧收敛",
            "detail": f"砸盘{sc_prev:.2f}→{sc_now:.2f}（降{abs(sc_delta):.2f}），最高{mb_now}板，情绪筑底"
        })

    if idx >= 2:
        prev2 = series[idx - 2]
        if (sc_prev < SMASH_LOW and sc_now < SMASH_LOW
                and mb_now <= BOARD_BOTTOM and (prev2.get("sc") or 0) < SMASH_LOW + 0.5):
            if not any(s["type"] == "bottom" for s in signals):
                signals.append({
                    "type": "bottom",
                    "severity": "medium",
                    "name": "底部确认",
                    "detail": f"连续低分歧（砸盘{sc_prev:.2f}/{sc_now:.2f}）+ 最高{mb_now}板，能量蓄势"
                })

    if mb_prev == 5 and mb_now >= BOARD_BREAKOUT and sc_delta < 0:
        signals.append({
            "type": "breakout",
            "severity": "strong",
            "name": "突破加速",
            "detail": f"连板{mb_prev}→{mb_now}突破生死线，砸盘反降{abs(sc_delta):.2f}（{sc_prev:.2f}→{sc_now:.2f}），多头一致"
        })
    elif mb_now >= BOARD_BREAKOUT and sc_now < SMASH_HIGH and sc_delta <= 0:
        signals.append({
            "type": "breakout",
            "severity": "medium",
            "name": "高位加速",
            "detail": f"最高{mb_now}板+砸盘{sc_now:.2f}（分歧未扩大），关注能否冲7板"
        })

    if mb_now >= BOARD_TOP and sc_now > SMASH_HIGH:
        sev = "strong" if sc_now > SMASH_EXTREME or mb_now >= 8 else "medium"
        signals.append({
            "type": "top",
            "severity": sev,
            "name": "高潮见顶",
            "detail": f"最高{mb_now}板+砸盘{sc_now:.2f}（过热），1-2日内大概率崩塌"
        })

    if sc_delta < -SMASH_DROP_BIG and mb_prev >= 5 and mb_now <= mb_prev - 2:
        signals.append({
            "type": "top",
            "severity": "strong",
            "name": "断裂崩塌",
            "detail": f"砸盘{sc_prev:.2f}→{sc_now:.2f}，连板{mb_prev}→{mb_now}，高潮结束"
        })

    return signals


def _classify_phase(sc: float, mb: int) -> str:
    if sc < 2.0 and mb <= 3:
        return "冰点酝酿"
    if sc < 3.5 and mb <= 4:
        return "蓄力爬升"
    if 3.5 <= sc < 5.5 and 4 <= mb <= 5:
        return "上升博弈"
    if sc >= 5.5 and mb >= 6:
        return "爆发高潮"
    if sc < 3.5 and mb <= 3:
        return "冰点酝酿"
    if sc >= 5.5 and mb <= 4:
        return "崩塌退潮"
    return "震荡分化"


def _judge_dragon_birth(series: List[Dict], idx: int,
                        dragons: Dict[str, Dict]) -> Optional[Dict]:
    cur = series[idx]
    date = cur["date"]
    dragon = dragons.get(date)
    if not dragon:
        return None
    score = dragon.get("score") or 0
    lifecycle = (dragon.get("lifecycle") or "").lower()
    if score < 70 or lifecycle in ("decline", "climax"):
        return None

    prev_dragon_missing_or_decline = False
    new_face = True
    for back in range(1, 6):
        if idx - back < 0:
            continue
        prev_date = series[idx - back]["date"]
        prev_d = dragons.get(prev_date)
        if back <= 2:
            if (not prev_d) or (prev_d.get("lifecycle") or "").lower() == "decline":
                prev_dragon_missing_or_decline = True
        if prev_d and prev_d.get("code") == dragon.get("code"):
            new_face = False
            break

    if new_face and idx >= 1:
        prev_date = series[idx - 1]["date"]
        prev_d = dragons.get(prev_date)
        if not prev_d or (prev_d.get("code") != dragon.get("code")
                          and (prev_d.get("score") or 0) < score):
            prev_dragon_missing_or_decline = True

    recent_signal = None
    for back in (0, 1, 2):
        if idx - back < 0:
            continue
        sigs = _detect_signals(series, idx - back) if (idx - back) > 0 else []
        for s in sigs:
            if s["type"] in ("bottom", "breakout"):
                recent_signal = s
                break
        if recent_signal:
            break

    cold_then_birth = False
    if idx >= 1:
        prev = series[idx - 1]
        if ((prev.get("sc") or 0) < 2.5 and (prev.get("max_boards") or 0) <= 4
                and not dragons.get(prev["date"])):
            cold_then_birth = True

    if not (prev_dragon_missing_or_decline or recent_signal or cold_then_birth):
        return None

    trigger = (
        f"{recent_signal['name']}后接棒" if recent_signal
        else ("冰点后首个龙头" if cold_then_birth else "旧龙头衰退后接棒")
    )
    return {
        "date": date,
        "dragon": dragon,
        "trigger": trigger,
        "sc": cur.get("sc"),
        "max_boards": cur.get("max_boards"),
        "phase": _classify_phase(cur.get("sc") or 0, cur.get("max_boards") or 0),
    }


def detect_turning_points(days: int = 30, db_path: str = None) -> Dict:
    conn = _get_conn(db_path)
    try:
        smash = _load_smash_series(conn, days=days + 10)
        dragons = _load_dragon_series(conn, days=days + 10)

        if not smash:
            return {"series": [], "turning_points": [], "dragon_birth_nodes": [],
                    "summary": {"error": "no smash data"}}

        smash = smash[-days:]

        series_out: List[Dict] = []
        turning_points: List[Dict] = []
        dragon_birth_nodes: List[Dict] = []

        for i, day in enumerate(smash):
            date = day["date"]
            sc = day.get("sc") or 0.0
            mb = day.get("max_boards") or 0
            signals = _detect_signals(smash, i) if i > 0 else []
            phase = _classify_phase(sc, mb)
            dragon = dragons.get(date)

            point = {
                "date": date,
                "sc": round(sc, 2),
                "max_boards": mb,
                "limit_up_count": day.get("lu"),
                "phase": phase,
                "signals": signals,
                "dragon": dragon,
            }
            series_out.append(point)

            for sig in signals:
                turning_points.append({
                    "date": date,
                    **sig,
                    "dragon_name": dragon["name"] if dragon else None,
                    "dragon_level": dragon["level"] if dragon else None,
                })

            if dragons:
                birth = _judge_dragon_birth(smash, i, dragons)
                if birth:
                    dragon_birth_nodes.append(birth)

        summary = {
            "days": len(series_out),
            "start_date": series_out[0]["date"],
            "end_date": series_out[-1]["date"],
            "turning_point_count": len(turning_points),
            "dragon_birth_count": len(dragon_birth_nodes),
            "current_phase": series_out[-1]["phase"],
            "latest_sc": series_out[-1]["sc"],
            "latest_max_boards": series_out[-1]["max_boards"],
        }

        seen_codes = set()
        deduped_births = []
        for node in dragon_birth_nodes:
            code = node["dragon"].get("code")
            if code in seen_codes:
                continue
            seen_codes.add(code)
            deduped_births.append(node)
        dragon_birth_nodes = deduped_births
        summary["dragon_birth_count"] = len(dragon_birth_nodes)

        return {
            "series": series_out,
            "turning_points": turning_points,
            "dragon_birth_nodes": dragon_birth_nodes,
            "summary": summary,
        }
    finally:
        conn.close()


# ============ 定时任务调用接口 ============
def check_latest_and_notify(notifier=None) -> Optional[Dict]:
    result = detect_turning_points(days=10)
    nodes = result.get("dragon_birth_nodes", [])
    if not nodes:
        return None

    latest_node = nodes[-1]
    latest_date = result["summary"]["end_date"]

    if latest_node["date"] != latest_date:
        return None

    notified = False
    if notifier is not None:
        try:
            notified = _send_dragon_birth_notification(notifier, latest_node, result)
        except Exception as e:
            logger.error(f"发送龙头诞生通知失败: {e}")

    latest_node["notified"] = notified
    return latest_node


def _send_dragon_birth_notification(notifier, node: Dict, full_result: Dict) -> bool:
    d = node["dragon"]
    title = f"🐉 新总龙头诞生 - {d['name']}({d['code']})"

    lines = [
        f"## 🐉 新总龙头诞生预警",
        "",
        f"**日期**：{node['date']}",
        f"**龙头**：{d['name']}（{d['code']}）",
        f"**等级**：{d['level']}级 | **评分**：{d['score']}",
        f"**连板**：{d['boards']}板 | **阶段**：{d['lifecycle']}",
        f"**概念**：{d.get('concept') or '--'}",
        "",
        f"## 📊 大盘环境",
        f"- 周期：{node['phase']}",
        f"- 砸盘系数：{node['sc']}",
        f"- 最高连板：{node['max_boards']}板",
        f"- 触发依据：{node['trigger']}",
        "",
        f"## 🎯 操作建议",
        f"- 新龙头首日/加速期，关注次日开盘承接",
        f"- 若开盘溢价<3%且竞价封单充足，可考虑半路/打板",
        f"- 严格设置止损（跌破前一日涨停价-3%）",
        f"- 单只仓位不超过总仓位30%",
        "",
        f"> 风险提示：以上为系统量化信号，不构成投资建议，市场有风险，操作需谨慎。",
    ]
    content = "\n".join(lines)
    return notifier.send_notification(title, content)


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    r = detect_turning_points(days=30)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))