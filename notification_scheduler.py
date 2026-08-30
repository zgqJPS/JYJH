# -*- coding: utf-8 -*-
"""
notification_scheduler.py - 微信通知节奏调度（盘前/盘中/盘后拆分）
=================================================================
将原"全部盘后长通知"拆分为三类，全部从数据库读数（与仪表盘/每日分析/智能推荐
三个页面同一日期锚点：xgt_limit_up_detail 最新交易日）：

  1. auction   09:25 竞价作战卡
     - 昨晚 operation_plans 中 action='operate' 的计划票，今日竞价状态逐一核对
       （一字封死=排到即接盘、开板回封=分歧转一致、未封板=按计划低吸/放弃）
     - 高位一字（≥4板竞价封死）风险警示：严禁竞价排板
  2. intraday  09:46 / 11:30 / 14:30 盘中速报
     - 实时高确定性买点：进场确定性 S+/S/A 且有仓位、非观望，标注分歧节奏状态
       （🟢分歧转一致回封为最佳买点；🔴一字仅总龙头小仓且排不到）
     - 风险提示：炸板≥3次的高标股（一致转分歧=卖点）
     - 砸盘≥6 / 炸板率≥35%：全场无买点闸门
     - 无买点且无风险时不推送（不骚扰）
 3. close     15:01 盘后专项报告
     - 市场全景 + 三项专项：
       ① 总龙头是否已确认（标的/板数/等级/生命周期；未确认则注明断档天数）
       ② 新龙头迹象（detect_dragon_imminent 候选 + 连板梯队完整度/断层）
       ③ 变盘风险预警（变盘节点信号 + 砸盘趋势 + 炸板率，红/黄/绿三级）
     - 次日策略 + 次日作战清单（operation_plans）+ 进场确定性 TOP3

去重：wechat_push_log 表，同一 (push_date, push_type, slot) 只推一次。
"""

import sqlite3
import json
import logging
from datetime import datetime, timedelta

try:
    from config import DB_PATH
except Exception:  # pragma: no cover
    DB_PATH = "market_data.db"

logger = logging.getLogger("notif_sched")


# ─────────────────────────── 基础工具 ───────────────────────────

def _conn(db_path=None):
    c = sqlite3.connect(db_path or DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_tables(db_path=None):
    c = _conn(db_path)
    try:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS wechat_push_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                push_date TEXT NOT NULL,
                push_type TEXT NOT NULL,
                slot TEXT,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(push_date, push_type, slot)
            );
        """)
        c.commit()
    finally:
        c.close()


def _already_pushed(push_date, push_type, slot, db_path=None):
    c = _conn(db_path)
    try:
        row = c.execute(
            "SELECT 1 FROM wechat_push_log WHERE push_date=? AND push_type=? AND slot=? LIMIT 1",
            (push_date, push_type, slot)).fetchone()
        return row is not None
    finally:
        c.close()


def _record_push(push_date, push_type, slot, title, db_path=None):
    c = _conn(db_path)
    try:
        c.execute(
            "INSERT OR IGNORE INTO wechat_push_log (push_date, push_type, slot, title) VALUES (?,?,?,?)",
            (push_date, push_type, slot, title))
        c.commit()
    finally:
        c.close()


def push_with_dedup(notifier, message, push_type, slot, db_path=None):
    """message: {'title':..., 'content':...} 或 None。返回 True=已发送"""
    if not message:
        logger.info(f"[{push_type}/{slot}] 无内容，跳过推送")
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    if _already_pushed(today, push_type, slot, db_path):
        logger.info(f"[{push_type}/{slot}] 今日已推送过，跳过")
        return False
    title, content = message.get("title", ""), message.get("content", "")
    ok = False
    if notifier is not None:
        try:
            ok = notifier.send_notification(title, content)
        except Exception as e:
            logger.error(f"[{push_type}/{slot}] 微信发送异常: {e}")
    _record_push(today, push_type, slot, title, db_path)
    logger.info(f"[{push_type}/{slot}] 推送完成 ok={ok} title={title[:30]}")
    return ok


def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _latest_data_date(conn):
    row = conn.execute("SELECT MAX(date) AS d FROM xgt_limit_up_detail").fetchone()
    return row["d"] if row and row["d"] else None


def _prev_trade_date(conn, date_str):
    row = conn.execute(
        "SELECT MAX(date) AS d FROM xgt_limit_up_detail WHERE date < ?",
        (date_str,)).fetchone()
    return row["d"] if row and row["d"] else None


def _next_workday(date_str):
    """简单推算下一工作日（跳过周末，节假日日历不在此处理）"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d.strftime("%Y-%m-%d")
    except Exception:
        return date_str


def _parse_time_minutes(raw):
    """'09:25:00'/'09:25'/'092500'/'0930' → 分钟数；失败返回 999"""
    try:
        s = (raw or "").strip()
        if not s:
            return 999
        if ":" in s:
            parts = s.split(":")
            return int(parts[0]) * 60 + int(parts[1])
        s = s.replace(":", "")
        if len(s) >= 4:
            return int(s[:2]) * 60 + int(s[2:4])
    except Exception:
        pass
    return 999


def _market_snapshot(conn, date_str):
    """三页同源的市场核心指标"""
    snap = {"smash": None, "lu_count": 0, "ld_count": 0,
            "explosion_rate": 0.0, "max_boards": 0}
    try:
        row = conn.execute(
            "SELECT smash_coefficient AS sc FROM smash_coefficients WHERE trade_date=?",
            (date_str,)).fetchone()
        if row and row["sc"] is not None:
            snap["smash"] = row["sc"]
        else:
            row = conn.execute(
                "SELECT smash_coefficient AS sc FROM smash_coefficients "
                "WHERE trade_date < ? AND smash_coefficient IS NOT NULL "
                "ORDER BY trade_date DESC LIMIT 1", (date_str,)).fetchone()
            if row:
                snap["smash"] = row["sc"]
    except Exception:
        pass
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c, MAX(limit_up_days) AS m FROM xgt_limit_up_detail WHERE date=?",
            (date_str,)).fetchone()
        snap["lu_count"] = row["c"] or 0
        snap["max_boards"] = row["m"] or 0
    except Exception:
        pass
    try:
        row = conn.execute(
            "SELECT * FROM xgt_daily_summary WHERE date=?", (date_str,)).fetchone()
        if row:
            d = dict(row)
            snap["ld_count"] = d.get("limit_down_count", 0) or 0
            er = d.get("explosion_rate", 0) or 0
            try:
                er = float(er)
                if er > 1:  # 百分数存储
                    er = er / 100.0
            except Exception:
                er = 0.0
            snap["explosion_rate"] = er
    except Exception:
        pass
    return snap


def _smash_tag(smash):
    if smash is None:
        return "⚪--"
    if smash < 3:
        return f"🟢{smash:.2f} 冰点(易反弹)"
    if smash <= 5:
        return f"🟡{smash:.2f} 博弈(可操作)"
    if smash < 6.5:
        return f"🔴{smash:.2f} 过热(防砸盘)"
    return f"⚫{smash:.2f} 极端分歧(无买点)"


def _divergence_of(conn, code, date_str, market_state):
    """返回 (is_yizi, state, label)；失败返回 (False, '', '')"""
    try:
        from smart_recommender import assess_divergence_state
        row = conn.execute(
            "SELECT * FROM xgt_limit_up_detail WHERE date=? AND code=?",
            (date_str, code)).fetchone()
        if not row:
            return (False, "", "")
        stock = dict(row)
        stock.setdefault("boards", stock.get("limit_up_days", 1))
        res = assess_divergence_state(stock, market_state) or {}
        return (bool(res.get("is_yizi")), res.get("state", ""), res.get("label", ""))
    except Exception as e:
        logger.debug(f"分歧状态计算失败 {code}: {e}")
        return (False, "", "")


# ─────────────────────────── 1) 09:25 竞价作战卡 ───────────────────────────

def build_auction_briefing(db_path=None):
    """基于昨晚(prev交易日)的 operation_plans，核对今日竞价状态，输出今日作战卡"""
    c = _conn(db_path)
    try:
        today = _today_str()
        has_today = c.execute(
            "SELECT COUNT(*) AS n FROM xgt_limit_up_detail WHERE date=?", (today,)
        ).fetchone()["n"]
        if not has_today:
            return None  # 今日实时数据尚未入库
        prev = _prev_trade_date(c, today)
        snap = _market_snapshot(c, today)

        today_pool = {}
        for r in c.execute("SELECT * FROM xgt_limit_up_detail WHERE date=?", (today,)):
            today_pool[r["code"]] = dict(r)

        lines = [f"## ⏰ 竞价作战卡（{today} 9:25）", ""]
        lines.append(f"**盘面**：涨停 {snap['lu_count']} / 跌停 {snap['ld_count']} | "
                     f"最高 {snap['max_boards']}板 | 砸盘 {_smash_tag(snap['smash'])}")
        lines.append("")

        # 高位一字风险警示（≥4板竞价封死）
        high_yizi = []
        for code, s in today_pool.items():
            bt = s.get("break_times") or 0
            boards = s.get("limit_up_days") or 1
            first_min = _parse_time_minutes(s.get("first_limit_up_time"))
            if bt == 0 and first_min <= 9 * 60 + 35 and boards >= 4:
                high_yizi.append(s)
        if high_yizi:
            lines.append("### 🔴 高位一字警示（严禁竞价排板，排到=接盘）")
            for s in high_yizi[:5]:
                lines.append(f"- {s.get('name','')}({s.get('code','')}) "
                             f"{s.get('limit_up_days',1)}板一字 | 等放量分歧、10:00前回封再考虑")
            lines.append("")

        plans = []
        if prev:
            for r in c.execute(
                "SELECT * FROM operation_plans WHERE plan_date=? AND action='operate' "
                "ORDER BY certainty_level, position_pct DESC", (prev,)):
                plans.append(dict(r))

        if not plans:
            lines.append("### 📋 今日计划")
            lines.append("- 昨晚无可操作计划标的：空仓等待，仅盯**分歧转一致回封**的半路/打板机会")
            lines.append("- 砸盘≥6 或炸板率≥35% 时全天不开新仓")
        else:
            lines.append(f"### 📋 今日计划（{prev} 晚制定，{len(plans)}只）")
            lines.append("")
            for i, p in enumerate(plans, 1):
                code, name = p["code"], p.get("name", "")
                t = today_pool.get(code)
                strat = {"board_hit": "打板", "half_way": "半路",
                         "low_buy": "低吸"}.get(p.get("buy_strategy", ""), p.get("buy_strategy", ""))
                head = f"**{i}. {name}({code})** {p.get('certainty_level','')}级 | {strat} | 仓位{p.get('position_pct',0)*100:.0f}%"
                lines.append(head)
                lo, hi = p.get("buy_price_low") or 0, p.get("buy_price_high") or 0
                if lo and hi:
                    lines.append(f"- 买入区间：{lo:.2f} ~ {hi:.2f}")
                sl = p.get("stop_loss_price") or 0
                if sl:
                    lines.append(f"- 🛑 止损价：{sl:.2f}（跌破无条件走）")
                if not t:
                    lines.append("- 竞价状态：**未封板** → 低吸票等支撑位；打板票放弃，不追高")
                else:
                    bt = t.get("break_times") or 0
                    boards = t.get("limit_up_days") or 1
                    first_min = _parse_time_minutes(t.get("first_limit_up_time"))
                    seal = t.get("seal_ratio") or 0
                    if bt == 0 and first_min <= 9 * 60 + 35 and boards >= 4:
                        lines.append(f"- 竞价状态：🔴**{boards}板一字封死** → 不挂竞价、不排板！"
                                     f"等开板分歧后10:00前回封（分歧转一致）再打，不回封放弃")
                    elif bt == 0:
                        lines.append(f"- 竞价状态：🟡 竞价封板/秒板（首封{t.get('first_limit_up_time','--')}，"
                                     f"封单{seal*100:.1f}%）→ 打板票仅回封确认后上，不排一字")
                    elif bt <= 2:
                        lines.append(f"- 竞价状态：🟢 **开板{bt}次后回封（分歧转一致）** → 最佳打板/半路点，"
                                     f"封单{seal*100:.1f}%站稳可上")
                    else:
                        lines.append(f"- 竞价状态：🔴 炸板{bt}次（分歧过大）→ 放弃，一致转分歧是卖点")
                lines.append("")

        lines.append("> ⚠️ 纪律：分歧转一致才买，一致转分歧就卖；一字不排、炸板不接。系统信号不构成投资建议。")
        return {"title": f"⏰{today[5:]} 竞价作战卡 {snap['lu_count']}涨停",
                "content": "\n".join(lines)}
    finally:
        c.close()


# ─────────────────────────── 2) 盘中速报 ───────────────────────────

def build_intraday_flash(slot, db_path=None):
    """盘中实时买点/风险速报。无买点无风险时返回 None（不骚扰）"""
    c = _conn(db_path)
    try:
        today = _today_str()
        has_today = c.execute(
            "SELECT COUNT(*) AS n FROM xgt_limit_up_detail WHERE date=?", (today,)
        ).fetchone()["n"]
        if not has_today:
            return None

        snap = _market_snapshot(c, today)
        smash, explosion = snap["smash"], snap["explosion_rate"]
        market_danger = (smash is not None and smash >= 6.0) or explosion >= 0.35

        # 进场确定性实时结果
        certainty_recs = []
        market_state = {}
        try:
            import smart_recommender as _sr
            market_state = _sr.analyze_current_market(today, db_path or DB_PATH)
        except Exception:
            market_state = {}
        try:
            from entry_certainty_analyzer import EntryCertaintyAnalyzer
            analyzer = EntryCertaintyAnalyzer(db_path or DB_PATH)
            certainty_recs = analyzer.analyze_date(today, top_n=10)
        except Exception as e:
            logger.warning(f"盘中速报: 进场确定性分析失败: {e}")

        buys = []
        for r in certainty_recs:
            comp = r.get("composite", {}) or {}
            op = r.get("operation", {}) or {}
            grade = comp.get("certainty_grade", "")
            pos = op.get("position_pct", 0) or 0
            action_name = op.get("action_name", "") or ""
            if grade not in ("S+", "S", "A"):
                continue
            if pos <= 0 or ("观望" in action_name) or ("wait" in action_name.lower()):
                continue
            # ── 量价闸门（首要依据）：量价 fail 直接不给买点；caution 标注降仓 ──
            vp = r.get("volume_price") or {}
            vp_grade = vp.get("grade")
            if vp_grade == "fail":
                continue
            is_yizi, state, label = _divergence_of(c, r.get("code", ""), today, market_state)
            buys.append({
                "code": r.get("code", ""), "name": r.get("name", ""),
                "boards": r.get("boards", 1), "grade": grade,
                "prob": comp.get("bayes_probability", 0) or 0,
                "action": action_name, "pos": pos,
                "timing": op.get("timing", ""), "sl": op.get("stop_loss", 0),
                "price": op.get("price_range", ""),
                "is_yizi": is_yizi, "state": state, "label": label,
                "vp_pattern": vp.get("pattern", ""),
                "vp_grade": vp_grade,
            })

        # 风险票：高标反复炸板（一致转分歧）
        risks = []
        try:
            for r in c.execute(
                "SELECT code, name, limit_up_days, break_times, seal_ratio, first_limit_up_time "
                "FROM xgt_limit_up_detail WHERE date=? AND break_times>=3 AND limit_up_days>=3 "
                "ORDER BY break_times DESC, limit_up_days DESC LIMIT 5", (today,)):
                risks.append(dict(r))
        except Exception:
            pass

        if not buys and not risks and not market_danger:
            return None  # 平静时段不打扰

        slot_name = {"09:46": "早盘", "11:30": "午盘", "14:30": "尾盘"}.get(slot, slot)
        lines = [f"## ⚡ 盘中速报·{slot_name}（{today} {slot}）", ""]
        lines.append(f"**盘面**：涨停 {snap['lu_count']} / 跌停 {snap['ld_count']} | "
                     f"最高 {snap['max_boards']}板 | 砸盘 {_smash_tag(smash)} | "
                     f"炸板率 {explosion:.0%}")
        lines.append("")

        if market_danger:
            lines.append("### 🔴 分歧闸门开启")
            lines.append("- 砸盘≥6 或炸板率≥35%：**除分歧转一致回封外全场无买点**，不开新仓、不排板")
            lines.append("")

        if buys:
            lines.append(f"### 🟢 实时可执行买点（{len(buys)}只）")
            for i, b in enumerate(buys[:5], 1):
                tag = ""
                if b["state"] == "divergence_to_consensus":
                    tag = " 🟢**分歧转一致回封（最佳买点）**"
                elif b["is_yizi"]:
                    tag = " 🔴一字（排不到，仅总龙头可小仓，等开板回封）"
                elif b["state"] == "consensus_to_divergence":
                    tag = " 🟠一致转分歧（回避）"
                lines.append(f"**{i}. {b['name']}({b['code']})** {b['boards']}板 "
                             f"{b['grade']}级 胜率{b['prob']:.0%}{tag}")
                if b.get("vp_pattern"):
                    vp_icon = "🟢" if b.get("vp_grade") == "pass" else "🟡"
                    lines.append(f"- 量价：{vp_icon}{b['vp_pattern']}（首要依据）")
                lines.append(f"- 动作：{b['action']} | 仓位 {b['pos']*100:.0f}%")
                if b.get("timing"):
                    lines.append(f"- 时机：{b['timing']}")
                if b.get("price"):
                    lines.append(f"- 价位：{b['price']}")
                if b.get("sl"):
                    lines.append(f"- 🛑 止损：{b['sl']}")
            lines.append("")
        elif not market_danger:
            lines.append("- 当前无 S/A 级且可成交的买点，继续等待分歧回封信号")
            lines.append("")

        if risks:
            lines.append("### 🔴 风险警示（一致转分歧=卖点）")
            for r in risks:
                lines.append(f"- {r['name']}({r['code']}) {r['limit_up_days']}板 "
                             f"炸板{r['break_times']}次 → 持仓逢高离场，未持仓不接")
            lines.append("")

        lines.append("> ⚠️ 系统实时信号，不构成投资建议；严格止损，排不到的一字不追。")
        return {"title": f"⚡{slot_name}速报: {len(buys)}买点/{len(risks)}风险",
                "content": "\n".join(lines)}
    finally:
        c.close()


# ─────────────────────────── 3) 15:01 盘后专项报告 ───────────────────────────

def _section_total_dragon(c, date_str):
    """① 总龙头确认状态"""
    lines = ["### 🐉 一、总龙头确认状态", ""]
    row = None
    try:
        row = c.execute(
            "SELECT * FROM dragon_detections WHERE detect_date=? AND dragon_type='total_dragon' "
            "ORDER BY total_score DESC LIMIT 1", (date_str,)).fetchone()
    except Exception as e:
        logger.warning(f"总龙头查询失败: {e}")
    if row:
        d = dict(row)
        lines.append(f"- ✅ **已确认：{d.get('name','')}({d.get('code','')})**")
        lines.append(f"- {d.get('limit_up_days',1)}板 | {d.get('certainty_level','')}级 | "
                     f"评分{d.get('total_score',0)} | 阶段：{d.get('lifecycle_stage','--')}")
        if d.get("concept"):
            lines.append(f"- 题材：{d['concept']}")
    else:
        lines.append("- ❌ **今日尚未确认市场总龙头**")
        try:
            prev_row = c.execute(
                "SELECT MAX(detect_date) AS d, name, code FROM dragon_detections "
                "WHERE dragon_type='total_dragon' AND detect_date < ?", (date_str,)).fetchone()
            if prev_row and prev_row["d"]:
                lines.append(f"- 上一任总龙头：{prev_row['name']}({prev_row['code']}) "
                             f"于 {prev_row['d']} 断档，龙头真空期注意补涨/切换风险")
            else:
                lines.append("- 近期无总龙头记录，属龙头真空期，高位接力谨慎")
        except Exception:
            pass
    lines.append("")
    return lines


def _section_new_dragon(c, date_str):
    """② 新龙头迹象：imminent 候选 + 梯队完整度"""
    lines = ["### 🚀 二、新龙头迹象", ""]
    # 梯队
    tiers = []
    try:
        for r in c.execute(
            "SELECT limit_up_days AS b, COUNT(*) AS n FROM xgt_limit_up_detail "
            "WHERE date=? GROUP BY limit_up_days ORDER BY b DESC", (date_str,)):
            tiers.append((r["b"] or 1, r["n"]))
    except Exception:
        pass
    if tiers:
        tier_str = " / ".join(f"{b}板{n}只" for b, n in tiers[:6])
        lines.append(f"- 连板梯队：{tier_str}")
        max_b = tiers[0][0]
        board_counts = {b: n for b, n in tiers}
        gaps = [b for b in range(2, max_b) if board_counts.get(b, 0) == 0]
        if gaps:
            lines.append(f"- ⚠️ 梯队断层：缺 {'/'.join(f'{g}板' for g in gaps)}，"
                         f"高位股孤军深入，接力风险大")
        elif max_b >= 3:
            lines.append("- ✅ 梯队完整，资金接力有序，利于新龙头走出")
    # imminent 候选
    try:
        from turning_point_detector import detect_dragon_imminent
        info = detect_dragon_imminent(date_str)
    except Exception as e:
        logger.warning(f"龙头迹象检测失败: {e}")
        info = None
    if info and info.get("candidates"):
        lines.append(f"- 🔍 **次日龙头候选 {len(info['candidates'])} 只**（准备资金，竞价核对）：")
        for cand in info["candidates"][:4]:
            cert = ""
            ci = cand.get("certainty")
            if ci:
                cert = f" | 确定性{ci.get('grade','--')}(胜率{ci.get('prob',0):.0%})"
            lines.append(f"  - **{cand['name']}({cand['code']})** {cand['boards']}板 "
                         f"[{cand.get('concept','--')}] 封单{cand.get('seal_ratio',0)}%"
                         f"{cert}")
            lines.append(f"    - 潜质：{cand.get('reason','')}")
    else:
        lines.append("- 暂无具备龙头潜质的高位候选（需≥3板+封板质量好+板块地位）")
    lines.append("")
    return lines


def _section_reversal_risk(c, date_str, snap):
    """③ 变盘风险预警"""
    lines = ["### ⚠️ 三、变盘风险预警", ""]
    level = "green"
    reasons = []

    smash = snap.get("smash")
    explosion = snap.get("explosion_rate", 0)

    # 砸盘趋势
    prev_smash = None
    try:
        rows = c.execute(
            "SELECT smash_coefficient AS sc FROM smash_coefficients "
            "WHERE trade_date <= ? AND smash_coefficient IS NOT NULL "
            "ORDER BY trade_date DESC LIMIT 3", (date_str,)).fetchall()
        vals = [r["sc"] for r in rows if r["sc"] is not None]
        if len(vals) >= 2:
            prev_smash = vals[1]
    except Exception:
        vals = []

    if smash is not None and smash >= 6.5:
        level = "red"
        reasons.append(f"砸盘系数 {smash:.2f} ≥6.5，极端分歧，崩塌风险")
    elif smash is not None and smash >= 5.5:
        level = "yellow" if level != "red" else level
        reasons.append(f"砸盘系数 {smash:.2f} 进入过热区（≥5.5）")
    if explosion >= 0.35:
        level = "red"
        reasons.append(f"炸板率 {explosion:.0%} ≥35%，封板资金溃败")
    if len(vals) >= 3 and vals[0] > vals[1] > vals[2] and (vals[0] - vals[2]) >= 1.0:
        if level == "green":
            level = "yellow"
        reasons.append(f"砸盘连续3日抬升（{vals[2]:.1f}→{vals[1]:.1f}→{vals[0]:.1f}），分歧持续加大")
    if smash is not None and smash < 3.0:
        reasons.append(f"砸盘 {smash:.2f} 处冰点区，若连板梯队回暖则为反弹拐点")

    # 变盘节点信号
    try:
        from turning_point_detector import detect_turning_points
        tp = detect_turning_points(days=10) or {}
        series = tp.get("series", []) or []
        if series:
            latest_day = series[-1]
            sigs = latest_day.get("signals", []) or []
            for s in sigs:
                name = s.get("name", "")
                sev = s.get("severity", "")
                stype = s.get("type", "")
                detail = s.get("detail", "")
                if stype == "top" or (sev == "strong" and stype not in ("bottom", "rebound", "breakout")):
                    level = "red"
                    reasons.append(f"变盘信号【{name}】{detail}")
                elif stype in ("bottom", "rebound", "breakout"):
                    reasons.append(f"✅ 向好信号【{name}】{detail}")
                elif sev:
                    if level == "green":
                        level = "yellow"
                    reasons.append(f"变盘信号【{name}】{detail}")
    except Exception as e:
        logger.warning(f"变盘节点检测失败: {e}")

    icon = {"red": "🔴 高风险（降仓/空仓，停止高位接力）",
            "yellow": "🟡 中风险（收缩仓位，只做最强分歧回封）",
            "green": "🟢 低风险（按计划正常操作）"}[level]
    lines.append(f"- 预警等级：**{icon}**")
    for r in reasons:
        lines.append(f"  - {r}")
    if not reasons:
        lines.append("  - 暂无明显变盘信号")
    lines.append("")
    return lines


def build_close_report(db_path=None):
    """15:01 盘后专项：市场全景 + 三项专项 + 次日策略 + 作战清单 + 确定性TOP"""
    c = _conn(db_path)
    try:
        date_str = _latest_data_date(c)
        if not date_str:
            return None
        target = _next_workday(date_str)
        snap = _market_snapshot(c, date_str)

        lines = [f"## 📊 盘后专项报告（{date_str}）", ""]

        # 市场全景
        lines.append("### 🌐 市场全景")
        lines.append(f"- 涨停/跌停：**{snap['lu_count']} / {snap['ld_count']}** | "
                     f"最高连板：**{snap['max_boards']}板** | 炸板率：{snap['explosion_rate']:.0%}")
        lines.append(f"- 砸盘系数：{_smash_tag(snap['smash'])}")
        lines.append("")

        # 三项专项
        lines.extend(_section_total_dragon(c, date_str))
        lines.extend(_section_new_dragon(c, date_str))
        lines.extend(_section_reversal_risk(c, date_str, snap))

        # 次日策略
        try:
            import smart_recommender as _sr
            nd = _sr.recommend_for_next_day(date_str, db_path or DB_PATH) or {}
            target = nd.get("target_date") or nd.get("date") or target
            lines.append("### 📅 次日策略")
            if nd.get("target_board_height"):
                lines.append(f"- 目标高度：{nd['target_board_height']}")
            focus = nd.get("focus_concepts", []) or []
            if focus:
                lines.append(f"- 关注题材：**{', '.join(focus[:5])}**")
            if nd.get("risk_control"):
                lines.append(f"- 风控：{nd['risk_control']}")
            if nd.get("overall_strategy"):
                lines.append(f"- 总策略：{nd['overall_strategy']}")
            lines.append("")
        except Exception as e:
            logger.warning(f"次日策略生成失败: {e}")

        # 次日作战清单
        lines.append(f"### 📋 {target} 作战清单")
        operate, wait_yizi = [], []
        try:
            for r in c.execute(
                "SELECT * FROM operation_plans WHERE plan_date=? ORDER BY position_pct DESC",
                (date_str,)):
                d = dict(r)
                if d.get("action") == "operate" and (d.get("position_pct") or 0) > 0:
                    operate.append(d)
                else:
                    wait_yizi.append(d)
        except Exception:
            pass
        if operate:
            for i, p in enumerate(operate[:6], 1):
                strat = {"board_hit": "打板", "half_way": "半路",
                         "low_buy": "低吸"}.get(p.get("buy_strategy", ""), p.get("buy_strategy", ""))
                sl = p.get("stop_loss_price") or 0
                lines.append(f"{i}. **{p.get('name','')}({p.get('code','')})** "
                             f"{p.get('certainty_level','')}级 {strat} "
                             f"仓位{p.get('position_pct',0)*100:.0f}%"
                             f"{f' 止损{sl:.2f}' if sl else ''}")
        else:
            lines.append("- 无可操作标的：空仓等待，分歧转一致信号出现前不出手")
        if wait_yizi:
            names = "、".join(f"{p.get('name','')}({p.get('code','')})"
                             for p in wait_yizi[:4])
            lines.append(f"- ⏸️ 观望标的（高位一字等分歧，不挂竞价不排板）：{names}")
        lines.append("")

        # 进场确定性 TOP3
        try:
            from entry_certainty_analyzer import EntryCertaintyAnalyzer
            analyzer = EntryCertaintyAnalyzer(db_path or DB_PATH)
            recs = analyzer.analyze_date(date_str, top_n=10)
            top = [r for r in recs
                   if (r.get("composite", {}) or {}).get("certainty_grade") in ("S+", "S", "A")][:3]
            if top:
                lines.append("### 🎯 进场确定性 TOP")
                for i, r in enumerate(top, 1):
                    comp = r.get("composite", {})
                    op = r.get("operation", {}) or {}
                    lines.append(f"{i}. **{r.get('name','')}({r.get('code','')})** "
                                 f"{r.get('boards',1)}板 {comp.get('certainty_grade','')}级 "
                                 f"胜率{comp.get('bayes_probability',0):.0%} → "
                                 f"{op.get('action_name','观望')} 仓位{op.get('position_pct',0)*100:.0f}%")
                lines.append("")
        except Exception as e:
            logger.warning(f"确定性TOP生成失败: {e}")

        lines.append("> ⚠️ 系统量化分析，不构成投资建议。节奏纪律：分歧转一致买，一致转分歧卖；"
                     "高位一字不排，砸盘≥6.5 不开仓。")
        return {"title": f"📊{date_str[5:]}盘后专项 龙头/变盘/次日策略",
                "content": "\n".join(lines)}
    finally:
        c.close()
