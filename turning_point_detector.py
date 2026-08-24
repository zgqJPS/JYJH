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


def _norm_turnover(v) -> float:
    """换手率归一化：数据库中可能以小数(0.05=5%)或百分数(5=5%)存储"""
    try:
        if v is None:
            return 0.0
        v = float(v)
        if v < 0 or v > 100:
            return 0.15  # 异常值用中性值
        if v > 1.0:
            v = v / 100.0  # 百分数→小数
        return v
    except Exception:
        return 0.15

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
    ]

    # 尝试加入进场确定性分析
    try:
        import os, sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        from entry_certainty_analyzer import EntryCertaintyAnalyzer
        analyzer = EntryCertaintyAnalyzer()
        ca = analyzer.analyze_stock(node['date'], d['code'])
        if 'error' not in ca:
            c = ca['composite']
            op = ca.get('operation', {})
            lines.append("## 🎯 进场确定性分析")
            lines.append(f"- 等级：**{c['certainty_grade']}** | 校准胜率：**{c.get('bayes_probability',0):.0%}** | 综合分：{c['score']}")
            dims = ca.get('dimensions', {})
            nxt = dims.get('next_day_certainty', {})
            for sig in nxt.get('signals', [])[:3]:
                lines.append(f"  - {sig}")
            if op:
                lines.append(f"- 建议操作：**{op.get('action_name','')}** | 仓位：**{op.get('position_pct',0)*100:.1f}%**")
                lines.append(f"- 时机：{op.get('timing','')}")
                if op.get('stop_loss'):
                    lines.append(f"- 止损：{op['stop_loss']:.2f}")
            lines.append("")
    except Exception as e:
        logging.getLogger(__name__).warning(f"龙头通知中确定性分析失败: {e}")

    lines.extend([
        f"## 🎯 操作建议",
        f"- 新龙头首日/加速期，关注次日开盘承接",
        f"- 若开盘溢价<3%且竞价封单充足，可考虑半路/打板",
        f"- 严格设置止损（跌破前一日涨停价-3%）",
        f"- 单只仓位不超过总仓位30%",
        "",
        f"> 风险提示：以上为系统量化信号，不构成投资建议，市场有风险，操作需谨慎。",
    ])
    content = "\n".join(lines)
    ok = notifier.send_notification(title, content)
    if ok:
        _record_notification(node["date"], "dragon_birth", title, content, "success")
    else:
        _record_notification(node["date"], "dragon_birth", title, content, "failed")
    return ok


# ============ 龙头即将诞生（次日预备金）预警 ============
def detect_dragon_imminent(date_str: str = None, db_path: str = None) -> Optional[Dict]:
    """
    检测最新交易日盘后，是否存在"次日有望冲击/确立总龙头"的候选股。
    目的：提前一天给用户发资金准备通知。

    候选条件（同时满足）：
      1. 当日真实连板≥3板（3板及以上才具备龙头基因）
      2. 当日封板质量好：seal_ratio≥1% 或 first_limit_up_time ≤ 10:00
      3. 当日炸板次数≤1
      4. 满足以下"龙头潜质"任一：
         a. 已是当日最高板
         b. 距最高板仅差1板且封板质量优于最高板股
         c. 板块龙（所属概念当日涨停≥3只且为该概念最高板）
      5. 排除：当日已被判定为 total_dragon（那个归诞生通知）

    返回 dict：{date, candidates:[{code,name,boards,concept,seal_ratio,reason,...}], market_ctx}
    """
    import sqlite3
    from config import DB_PATH as _DB
    db_path = db_path or _DB
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if date_str is None:
            row = conn.execute(
                "SELECT MAX(date) as d FROM xgt_limit_up_detail").fetchone()
            date_str = row["d"]
        if not date_str:
            return None

        # 获取当日所有涨停股
        rows = conn.execute("""
            SELECT code, name, limit_up_days, seal_ratio, break_times,
                   first_limit_up_time, turnover_rate, concept, reason,
                   flow_capital, volume_bias
            FROM xgt_limit_up_detail WHERE date = ?
        """, (date_str,)).fetchall()
        if not rows:
            return None

        # 用BoardCalculator获取真实连板
        real_boards = {}
        try:
            from board_calculator import BoardCalculator
            bc = BoardCalculator(conn)
            for r in rows:
                rb = bc.get_consecutive_boards(date_str, r["code"], conn)
                if rb > 0:
                    real_boards[r["code"]] = rb
        except Exception:
            pass

        # 概念统计
        concept_counts = {}
        concept_max_board = {}
        for r in rows:
            cb = real_boards.get(r["code"], r["limit_up_days"] or 1)
            c = r["concept"]
            if c:
                concept_counts[c] = concept_counts.get(c, 0) + 1
                if cb > concept_max_board.get(c, 0):
                    concept_max_board[c] = cb

        # 市场环境
        max_board_today = max(
            (real_boards.get(r["code"], r["limit_up_days"] or 1) for r in rows),
            default=0)

        # 砸盘系数
        sc_row = conn.execute(
            "SELECT smash_coefficient FROM smash_coefficients WHERE trade_date=?",
            (date_str,)).fetchone()
        sc = sc_row["smash_coefficient"] if sc_row else None

        # 当日是否已有total_dragon（从dragon_detections）
        total_dragon_code = None
        try:
            td_row = conn.execute("""
                SELECT code FROM dragon_detections
                WHERE detect_date=? AND dragon_type='total_dragon'
                ORDER BY total_score DESC LIMIT 1
            """, (date_str,)).fetchone()
            if td_row:
                total_dragon_code = td_row["code"]
        except Exception:
            pass

        candidates = []
        for r in rows:
            code = r["code"]
            boards = real_boards.get(code, r["limit_up_days"] or 1)
            if boards < 3:
                continue
            if code == total_dragon_code:
                continue  # 已是总龙头，归诞生通知

            seal_ratio = r["seal_ratio"] or 0
            break_times = r["break_times"] or 0
            raw_first = (r["first_limit_up_time"] or "").strip()
            # 标准化时间：支持 "09:25:00" / "09:25" / "092500" / "0930"
            if ":" in raw_first:
                first_time = raw_first[:5]
            elif len(raw_first) >= 6:
                first_time = raw_first[:2] + ":" + raw_first[2:4]
            elif len(raw_first) == 4:
                first_time = raw_first[:2] + ":" + raw_first[2:]
            else:
                first_time = raw_first
            concept = r["concept"] or ""

            # 封板质量门槛
            try:
                h, m = first_time.split(":")
                early_minutes = int(h) * 60 + int(m)
            except Exception:
                early_minutes = 999
            good_seal = seal_ratio >= 0.01 or early_minutes <= 10 * 60
            if not good_seal:
                continue
            if break_times > 1:
                continue

            # 龙头潜质判断
            reasons = []
            # a. 当日最高板（且尚无total_dragon或最高板多只并列）
            if boards == max_board_today:
                if not total_dragon_code:
                    reasons.append(f"当日最高板（{boards}板），尚未确立总龙头")
                else:
                    reasons.append(f"{boards}板并列最高梯队")
            # b. 距最高板仅差1板
            elif boards == max_board_today - 1 and max_board_today >= 4:
                reasons.append(f"{boards}板距最高板仅1板，具备卡位潜力")
            # c. 板块龙
            if concept and concept_counts.get(concept, 0) >= 3:
                if concept_max_board.get(concept, 0) == boards:
                    reasons.append(f"「{concept}」板块龙（板块{concept_counts[concept]}只涨停）")

            if not reasons:
                continue

            # 进场确定性（如果可用）
            certainty_info = None
            try:
                from entry_certainty_analyzer import EntryCertaintyAnalyzer
                analyzer = EntryCertaintyAnalyzer(db_path)
                ca = analyzer.analyze_stock(date_str, code)
                if "error" not in ca:
                    c = ca["composite"]
                    certainty_info = {
                        "grade": c.get("certainty_grade", "--"),
                        "score": c.get("score", 0),
                        "prob": c.get("bayes_probability", 0),
                    }
            except Exception:
                pass

            candidates.append({
                "code": code,
                "name": r["name"],
                "boards": boards,
                "concept": concept or "--",
                "seal_ratio": round(seal_ratio * 100, 2),
                "break_times": break_times,
                "first_time": first_time,
                "turnover": round(_norm_turnover(r["turnover_rate"]) * 100, 2),
                "reason": "；".join(reasons),
                "certainty": certainty_info,
            })

        if not candidates:
            return None

        # 按板数+封单排序，最多5只
        candidates.sort(key=lambda x: (-x["boards"], -x["seal_ratio"]))
        candidates = candidates[:5]

        return {
            "date": date_str,
            "candidates": candidates,
            "market_ctx": {
                "max_boards": max_board_today,
                "smash_coefficient": sc,
                "total_dragon_exists": total_dragon_code is not None,
            },
        }
    finally:
        conn.close()


def check_dragon_imminent_and_notify(notifier=None) -> Optional[Dict]:
    """
    供每日定时任务调用（盘后数据+龙头识别完成后）。
    若检测到"次日有望产生总龙头"的候选股，推送资金准备通知。
    同一日期只推一次。
    """
    info = detect_dragon_imminent()
    if not info or not info.get("candidates"):
        return None

    date_str = info["date"]
    notif_type = "dragon_imminent"
    if _already_notified(date_str, notif_type):
        logger.info(f"龙头即将诞生预警 {date_str} 已推送过，跳过")
        return {**info, "notified": True, "skipped": True}

    notified = False
    if notifier is not None:
        try:
            notified = _send_dragon_imminent_notification(notifier, info)
        except Exception as e:
            logger.error(f"发送龙头即将诞生通知失败: {e}")

    top = info["candidates"][0]
    top_name = top["name"]
    top_boards = top["boards"]
    n_cand = len(info["candidates"])
    max_b = info["market_ctx"]["max_boards"]
    _record_notification(
        date_str, notif_type,
        "🚀 龙头即将诞生？{}({}板)等{}只候选".format(top_name, top_boards, n_cand),
        "候选数{}，最高板{}".format(n_cand, max_b),
        "success" if notified else "failed")
    return {**info, "notified": notified}


def _send_dragon_imminent_notification(notifier, info: Dict) -> bool:
    """发送龙头即将诞生（资金准备）微信。"""
    ctx = info["market_ctx"]
    top = info["candidates"][0]
    title = f"🚀 龙头即将诞生？请准备资金 - {info['date']}"

    lines = [
        f"## 🚀 总龙头诞生前夜 · 资金准备预警",
        "",
        f"**日期**：{info['date']}（盘后）",
        f"**市场**：最高{ctx['max_boards']}板 | 砸盘系数{ctx.get('smash_coefficient','--')}",
        ("- ⚠️ 当日已有总龙头，以下为可能的切换/补涨候选"
         if ctx["total_dragon_exists"]
         else "- ✅ 当日尚未确立总龙头，以下为最强候选"),
        "",
        f"## 🎯 重点候选（次日重点跟踪）",
        "",
    ]

    for i, c in enumerate(info["candidates"], 1):
        cert_str = ""
        if c["certainty"]:
            ci = c["certainty"]
            cert_str = f" | 确定性 **{ci['grade']}**（校准胜率{ci['prob']:.0%}）"
        lines.append(f"### {i}. {c['name']}（{c['code']}）{c['boards']}板{cert_str}")
        lines.append(f"- 概念：{c['concept']}")
        lines.append(f"- 封单：{c['seal_ratio']}% | 炸板：{c['break_times']}次 | "
                     f"首封：{c['first_time']} | 换手：{c['turnover']}%")
        lines.append(f"- 潜质：{c['reason']}")
        lines.append("")

    lines.extend([
        f"## ⏰ 次日操作准备",
        f"1. **9:15-9:25 竞价**：重点观察上述候选竞价金额和封单，"
        f"竞价封单≥流通盘0.5%且高开3-7%为强势信号",
        f"2. **9:30 开盘**：若3-5分钟内快速上板且封单持续加大，可打板介入",
        f"3. **资金分配**：单只候选预留10-15%仓位，确认龙头后再加至目标仓位",
        f"4. **风控**：若开盘低开>3%或竞价封单薄弱，放弃当日介入",
        "",
        f"> 风险提示：以上为系统量化预判，总龙头能否次日确立存在不确定性，"
        f"不构成投资建议，市场有风险，操作需谨慎。",
    ])
    content = "\n".join(lines)
    return notifier.send_notification(title, content)


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
