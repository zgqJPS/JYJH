"""
turning_point_detector.py - 大盘变盘节点与总龙头诞生节点识别

核心逻辑（基于"砸盘系数 × 连板高度"深度分析报告的5个量化信号）：
  1. 变盘节点 detection：
     - 冰点拐点：砸盘骤降>3点（或连续2日<3 + 连板≤3）
     - 5→6突破 + 砸盘下降：最强看涨信号
     - 7板+砸盘>6：见顶崩塌信号
  2. 总龙头诞生预判：
     在"变盘节点"出现后1-2日内，结合 dragon_detections：
     - 若前日 total_dragon 处于 decline/缺失（龙头断档）
     - 且当日出现新的 total_dragon（lifecycle=launch/acceleration，评分≥70）
     → 判定为"新龙头诞生节点"

输出：
  - detect_recent(days=30)：返回最近N日的节点列表，用于仪表盘叠加
  - check_latest_and_notify()：供定时任务调用，若当日命中新龙头诞生节点则微信通知
"""

from __future__ import annotations

import os
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("turning_point")

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "stock_data.db"
)


def _get_conn(db_path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ============ 信号阈值（来自深度分析报告的实证规律） ============
SMASH_LOW = 3.0           # 砸盘低于此为"低分歧"
SMASH_HIGH = 5.5          # 砸盘高于此为"高分歧"
SMASH_EXTREME = 7.0       # 砸盘高于此为"极端分歧"
SMASH_DROP_BIG = 3.0      # 砸盘骤降阈值
SMASH_DROP_MED = 1.5      # 砸盘明显下降阈值
BOARD_BOTTOM = 3          # 底部连板高度
BOARD_BREAKOUT = 6        # 突破6板
BOARD_TOP = 7             # 顶部连板高度


def _load_smash_series(conn: sqlite3.Connection, days: int = 60) -> List[Dict]:
    """读取砸盘序列（按日期升序）。"""
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
    """读取每日总龙头（按日期索引）。"""
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


# ============ 单节点信号判定 ============
def _detect_signals(series: List[Dict], idx: int) -> List[Dict]:
    """
    对第 idx 日判定其变盘信号。返回信号列表（可能多个并存）。
    每个信号：{type, severity, name, detail}
      type: 'bottom'/'breakout'/'top'/'rebound'
      severity: 'strong'/'medium'/'weak'
    """
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

    # —— 信号①：砸盘骤降 + 低位连板 = 见底反弹（strong） ——
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

    # —— 信号②：连续2日低砸盘 + 连板≤3 = 底部确认 ——
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

    # —— 信号③：5→6突破 + 砸盘下降 = 最强看涨 ——
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

    # —— 信号④：7板+砸盘>6 = 见顶崩塌 ——
    if mb_now >= BOARD_TOP and sc_now > SMASH_HIGH:
        sev = "strong" if sc_now > SMASH_EXTREME or mb_now >= 8 else "medium"
        signals.append({
            "type": "top",
            "severity": sev,
            "name": "高潮见顶",
            "detail": f"最高{mb_now}板+砸盘{sc_now:.2f}（过热），1-2日内大概率崩塌"
        })

    # —— 信号⑤：砸盘骤降 + 高板位 = 高位崩塌（断裂式下跌） ——
    if sc_delta < -SMASH_DROP_BIG and mb_prev >= 5 and mb_now <= mb_prev - 2:
        signals.append({
            "type": "top",
            "severity": "strong",
            "name": "断裂崩塌",
            "detail": f"砸盘{sc_prev:.2f}→{sc_now:.2f}，连板{mb_prev}→{mb_now}，高潮结束"
        })

    return signals


def _classify_phase(sc: float, mb: int) -> str:
    """根据砸盘+连板判断周期阶段。"""
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


# ============ 主接口：检测最近N日的变盘节点 ============
def detect_turning_points(days: int = 30, db_path: str = None) -> Dict:
    """
    返回：
    {
      "series": [{date, sc, max_boards, phase, signals:[]}, ...],
      "turning_points": [{date, type, severity, name, detail, dragon}],
      "dragon_birth_nodes": [{date, dragon, trigger_signal, ...}],
      "summary": {...}
    }
    """
    conn = _get_conn(db_path)
    try:
        smash = _load_smash_series(conn, days=days + 10)
        dragons = _load_dragon_series(conn, days=days + 10)

        if not smash:
            return {"series": [], "turning_points": [], "dragon_birth_nodes": [],
                    "summary": {"error": "no smash data"}}

        # 截取最近 days 个交易日
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

            # —— 龙头诞生判定 ——
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

        # 同一只龙头只保留其首次被识别为"诞生"的那一天
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


def _judge_dragon_birth(series: List[Dict], idx: int,
                        dragons: Dict[str, Dict]) -> Optional[Dict]:
    """
    判定某日是否为"新总龙头诞生节点"。
    条件：
      1. 当日存在 total_dragon（评分≥70，lifecycle ∈ launch/acceleration）
      2. 满足下列任一前置背景：
         a. 前1-2日 total_dragon 缺失或处于 decline/断板
         b. 近1-2日出现过 bottom / breakout 信号
         c. 砸盘处于冰点（<2.5）且连板≤4后第一次出现明确龙头
    """
    cur = series[idx]
    date = cur["date"]
    dragon = dragons.get(date)
    if not dragon:
        return None
    score = dragon.get("score") or 0
    lifecycle = (dragon.get("lifecycle") or "").lower()
    if score < 70 or lifecycle in ("decline", "climax"):
        return None

    # 查前2日的龙头情况：
    # - 若前2日无龙头/衰退 → 新龙头诞生
    # - 若是只全新的票（近5日都没当过total_dragon） → 新龙头诞生
    prev_dragon_missing_or_decline = False
    new_face = True  # 假设是新面孔，除非近5日当过龙头
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

    # 新面孔首日成为龙头，且评分明显高于前日其他票，也视作诞生
    if new_face and idx >= 1:
        prev_date = series[idx - 1]["date"]
        prev_d = dragons.get(prev_date)
        if not prev_d or (prev_d.get("code") != dragon.get("code")
                          and (prev_d.get("score") or 0) < score):
            prev_dragon_missing_or_decline = True

    # 近2日是否出现变盘信号
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

    # 冰点后第一龙头
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


# ============ 定时任务调用：检查最新日是否命中龙头诞生 ============
def check_latest_and_notify(notifier=None) -> Optional[Dict]:
    """
    供每日定时任务调用（在数据+龙头识别完成后）。
    若最新一日命中"新总龙头诞生节点"，通过 notifier 发送微信。
    返回节点 dict（含 notified=True/False），未命中返回 None。
    """
    result = detect_turning_points(days=10)
    nodes = result.get("dragon_birth_nodes", [])
    if not nodes:
        return None

    latest_node = nodes[-1]
    latest_date = result["summary"]["end_date"]

    # 只关心"最新日"命中的诞生节点
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


def check_latest_risk_and_notify(notifier=None) -> Optional[Dict]:
    """
    供每日定时任务调用：检测最新日是否出现"大盘变盘空仓信号"。
    空仓信号定义（强见顶类）：
      - 高潮见顶（type=top, severity=strong）：7板+砸盘>5.5（极端过热）
      - 断裂崩塌（type=top, severity=strong）：高位连板崩塌+砸盘骤降
    同一信号日只推送一次（查 notification_log）。
    返回推送的信号 dict（含 notified=True/False），未命中返回 None。
    """
    result = detect_turning_points(days=10)
    latest_date = result["summary"]["end_date"]
    series = result.get("series", [])
    if not series:
        return None

    latest_day = series[-1]
    if latest_day["date"] != latest_date:
        return None

    # 命中：当日最新的强 top 信号
    risk_signal = None
    for sig in latest_day.get("signals", []):
        if sig.get("type") == "top" and sig.get("severity") == "strong":
            risk_signal = sig
            break
    if not risk_signal:
        return None

    # 同类型同日期去重：查 notification_log 中是否已推过
    notif_type = f"turning_point_empty_{risk_signal['name']}"
    if not _already_notified(latest_date, notif_type):
        notified = False
        if notifier is not None:
            try:
                notified = _send_empty_position_notification(
                    notifier, latest_day, risk_signal, result)
            except Exception as e:
                logger.error(f"发送空仓信号通知失败: {e}")
        # 写入通知日志
        _record_notification(latest_date, notif_type,
                             f"⚠️ 大盘变盘空仓信号 - {risk_signal['name']}",
                             risk_signal.get("detail", ""),
                             "success" if notified else "failed")
        return {"signal": risk_signal, "date": latest_date, "notified": notified}
    else:
        logger.info(f"空仓信号 {latest_date}/{risk_signal['name']} 已推送过，跳过")
        return {"signal": risk_signal, "date": latest_date, "notified": True, "skipped": True}


def _already_notified(date_str: str, notif_type: str) -> bool:
    """检查 notification_log 中是否已存在同一日期+类型的成功通知。"""
    try:
        conn = _get_conn()
        try:
            row = conn.execute(
                """SELECT 1 FROM notification_log
                   WHERE date = ? AND notification_type = ? AND status = 'success'
                   LIMIT 1""",
                (date_str, notif_type)
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"查询通知去重失败: {e}")
        return False


def _record_notification(date_str: str, notif_type: str, title: str,
                         content: str, status: str = "success"):
    """写入 notification_log（若表存在）。"""
    try:
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO notification_log
                   (date, notification_type, title, content, status, send_time)
                   VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))""",
                (date_str, notif_type, title[:200], content, status)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"写入通知日志失败: {e}")


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
    ok = notifier.send_notification(title, content)
    if ok:
        _record_notification(node["date"], "dragon_birth", title, content, "success")
    else:
        _record_notification(node["date"], "dragon_birth", title, content, "failed")
    return ok


def _send_empty_position_notification(notifier, day: Dict, signal: Dict,
                                      full_result: Dict) -> bool:
    """发送大盘变盘空仓信号微信。"""
    s = full_result.get("summary", {})
    title = f"⚠️ 大盘空仓信号 - {signal['name']}（{day['date']}）"
    lines = [
        f"## ⚠️ 大盘变盘空仓预警",
        "",
        f"**日期**：{day['date']}",
        f"**信号**：{signal['name']}（强度：{signal['severity']}）",
        f"**细节**：{signal['detail']}",
        "",
        f"## 📊 大盘状态",
        f"- 周期阶段：{day.get('phase') or '--'}",
        f"- 砸盘系数：{day.get('sc')}",
        f"- 最高连板：{day.get('max_boards')}板",
        f"- 涨停数：{day.get('limit_up_count') or '--'}",
        f"- 近30日变盘节点：{s.get('turning_point_count', '--')} 个",
        "",
        f"## 🛡️ 操作建议",
        f"- **降仓/空仓为主**，停止新开仓",
        f"- 高位连板股（≥6板）坚决止盈/止损",
        f"- 总仓位建议降至 3 成以下",
        f"- 等待砸盘系数回落至 3 以下、新冰点见底信号出现后再考虑进场",
        f"- 若持有龙头，参考其生命周期：climax/decline 阶段应果断离场",
        "",
        f"> 风险提示：以上为系统量化信号，不构成投资建议，市场有风险，操作需谨慎。",
    ]
    return notifier.send_notification(title, "\n".join(lines))


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    r = detect_turning_points(days=30)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
