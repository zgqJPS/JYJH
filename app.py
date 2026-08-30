"""
app.py - Web 应用入口（V6 增强部署版）
移除 ngrok，添加健康检查，适配 Railway 部署
"""

import sys
import os
import json
import uuid
import threading
import logging
import mimetypes
import requests
import time
import zoneinfo
import schedule
import re
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta

tz = zoneinfo.ZoneInfo('Asia/Shanghai')
now = datetime.now(tz)

# 确保项目路径在 sys.path
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

# 统一从 config 导入 DB_PATH
from config import DB_PATH, KNOWLEDGE_DIR
from db import Database
from main import run_fetch

# ============ Server酱微信通知 ============
class ServerChanNotifier:
    def __init__(self, sckey: str):
        self.sckey = sckey
        self.base_url = f"https://sctapi.ftqq.com/{sckey}.send"

    def send_notification(self, title: str, content: str) -> bool:
        if not self.sckey:
            return False
        try:
            payload = {'title': title[:32], 'desp': content, 'channel': 9}
            response = requests.post(self.base_url, data=payload, timeout=30)
            if response.status_code == 200:
                result = response.json()
                return result.get('code') == 0
            return False
        except Exception as e:
            logging.error(f"发送微信通知失败: {e}")
            return False

SERVER_CHAN_SCKEY = os.environ.get("SERVER_CHAN_SCKEY", "SCT302469TzkdqbtA9rEWoHctOuDgRg9K3")
if not SERVER_CHAN_SCKEY:
    logging.warning("未设置 SERVER_CHAN_SCKEY，微信通知禁用")
    notifier = None
else:
    notifier = ServerChanNotifier(SERVER_CHAN_SCKEY)

# ============ 模块导入（try-except 保护） ============
_smart_recommender = None
_live_tracker = None
_self_upgrader = None
_exit_strategy = None
_simulator = None
_new_modules_status = {}

try:
    import smart_recommender as _smart_recommender
    _new_modules_status['smart_recommender'] = 'loaded'
except Exception as e:
    _new_modules_status['smart_recommender'] = f'error: {e}'

try:
    import live_tracker as _live_tracker
    _new_modules_status['live_tracker'] = 'loaded'
except Exception as e:
    _new_modules_status['live_tracker'] = f'error: {e}'

try:
    import self_upgrader as _self_upgrader
    _new_modules_status['self_upgrader'] = 'loaded'
except Exception as e:
    _new_modules_status['self_upgrader'] = f'error: {e}'

try:
    import exit_strategy as _exit_strategy
    _new_modules_status['exit_strategy'] = 'loaded'
except Exception as e:
    _new_modules_status['exit_strategy'] = f'error: {e}'

try:
    import simulator as _simulator
    _new_modules_status['simulator'] = 'loaded'
except Exception as e:
    _new_modules_status['simulator'] = f'error: {e}'

# ============ 任务状态 ============
tasks = {}
tasks_lock = threading.Lock()

STATIC_DIR = os.path.join(PROJECT_DIR, 'static')
TEMPLATE_DIR = os.path.join(PROJECT_DIR, 'templates')

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("web")


# ============ ★ 修改：微信通知函数，增加 daily_result 参数 ============
def send_recommend_notification(date_str, recommendations, market_state, next_day, daily_result=None):
    if not notifier:
        return False
    title = f"📊 {date_str} 盘后策略"
    lines = []

    # ── 1. 市场全景 ──
    lines.append("## 🎯 市场全景\n")
    cycle = market_state.get('cycle_phase', '未知')
    sc = market_state.get('smash_coefficient')
    sc_str = f"{sc:.2f}" if isinstance(sc, (int, float)) else '--'
    if isinstance(sc, (int, float)):
        if sc < 3:
            sc_tag = "🟢冰点（易反弹）"
        elif sc <= 5:
            sc_tag = "🟡博弈（可操作）"
        else:
            sc_tag = "🔴过热（防砸盘）"
    else:
        sc_tag = ""
    lines.append(f"- 周期: **{cycle}** {sc_tag}")
    lines.append(f"- 砸盘系数: **{sc_str}**")
    lines.append(f"- 涨停/跌停: **{market_state.get('limit_up_count', 0)} / {market_state.get('limit_down_count', 0)}**")
    lines.append(f"- 最高连板: **{market_state.get('max_boards', 0)}板**")
    lines.append(f"- 炸板率: {market_state.get('explosion_rate', 0):.0%}")
    sentiment = market_state.get('sentiment', '')
    if sentiment:
        lines.append(f"- 情绪: {sentiment}")
    lines.append("")

    # ── 2. 次日操作策略 ──
    if next_day:
        lines.append("## 📅 次日策略\n")
        target_h = next_day.get('target_board_height', '')
        if target_h:
            lines.append(f"- 目标高度: {target_h}")
        focus = next_day.get('focus_concepts', [])
        if focus:
            lines.append(f"- 关注题材: **{', '.join(focus[:5])}**")
        risk = next_day.get('risk_control', '')
        if risk:
            lines.append(f"- 风控: {risk}")
        strategy = next_day.get('overall_strategy', '')
        if strategy:
            lines.append(f"- 总策略: {strategy}")
        lines.append("")

    # ── 3. ★ 新增：资金流分析报告摘要 ──
    if daily_result:
        cf_report = daily_result.get("capital_flow_report", "")
        if cf_report:
            lines.append("## 💰 资金流快报\n")
            # 提取关键行
            cf_lines = cf_report.strip().split('\n')
            summary_lines = []
            for line in cf_lines:
                if '【综合评估】' in line or '仓位系数' in line or '进攻力度' in line or '持续能力' in line or '轮动模式' in line:
                    # 清理多余空格
                    clean_line = line.strip()
                    if clean_line:
                        summary_lines.append(clean_line)
                if len(summary_lines) >= 6:
                    break
            if summary_lines:
                for sl in summary_lines:
                    lines.append(f"- {sl}")
            lines.append("")

        # ── 4. ★ 新增：龙头识别摘要 ──
        dragon_report = daily_result.get("dragon_report", "")
        if dragon_report:
            # 提取总龙头
            match = re.search(r'【(\w+)级】(.+?)\((\d+)\)', dragon_report)
            if match:
                level, name, code = match.groups()
                lines.append(f"## 🐉 总龙头：{name}({code}) {level}级\n")
            # 提取更多龙头信息（前3只）
            dragon_matches = re.findall(r'【(\w+)级】(.+?)\((\d+)\)', dragon_report)
            if len(dragon_matches) > 1:
                lines.append("其他龙头：")
                for i, (lvl, nm, cd) in enumerate(dragon_matches[1:4], 1):
                    lines.append(f"  {i}. 【{lvl}级】{nm}({cd})")
            lines.append("")

        # ── 5. ★ 新增：操作计划摘要 ──
        plan_report = daily_result.get("plan_report", "")
        if plan_report:
            # 统计可操作标的数量
            operate_matches = re.findall(r'【(\w+)级】', plan_report)
            if operate_matches:
                lines.append(f"## 📋 操作计划：{len(operate_matches)}只可操作标的\n")
                # 提取操作建议摘要
                action_matches = re.findall(r'│ (?:🎯|🛤️|💰) (.*?):', plan_report)
                if action_matches:
                    lines.append("操作策略：")
                    for am in action_matches[:3]:
                        lines.append(f"  - {am.strip()}")
            lines.append("")

    # ── 6. 进场确定性分析（核心） ──
    certainty_recs = []
    try:
        from entry_certainty_analyzer import EntryCertaintyAnalyzer
        analyzer = EntryCertaintyAnalyzer(DB_PATH)
        certainty_recs = analyzer.analyze_date(date_str, top_n=10)
    except Exception as e:
        logging.warning(f"进场确定性分析失败: {e}")

    if certainty_recs:
        actionable = [r for r in certainty_recs
                      if r['composite']['certainty_grade'] in ('S+', 'S', 'A')]
        if actionable:
            lines.append("## 🎯 进场确定性TOP（核心）\n")
            for i, r in enumerate(actionable[:5], 1):
                c = r['composite']
                op = r.get('operation', {})
                grade = c['certainty_grade']
                bp = c.get('bayes_probability', 0)
                grade_icon = {'S+': '🔥', 'S': '⭐', 'A': '✅'}.get(grade, '')
                lines.append(f"### {i}. {grade_icon} {r['name']}({r['code']}) {grade}级")
                lines.append(f"- 板数: {r['boards']}板 | 校准胜率: **{bp:.0%}** | 综合分: {c['score']}")
                if r.get('concept'):
                    lines.append(f"- 题材: {r['concept']}")

                dims = r.get('dimensions', {})
                dim_parts = []
                for dk, dn in [('seal_quality','封板'), ('positioning','卡位'),
                               ('theme_strength','题材'), ('turnover_structure','换手'),
                               ('auction_proxy','竞价'), ('next_day_certainty','推演')]:
                    dv = dims.get(dk, {})
                    if dv:
                        dim_parts.append(f"{dn}{dv.get('grade','')}{dv.get('score',0):.0f}")
                if dim_parts:
                    lines.append(f"- 六维: {' | '.join(dim_parts)}")

                sigs = []
                for dk in ['next_day_certainty', 'seal_quality', 'auction_proxy']:
                    for s in dims.get(dk, {}).get('signals', [])[:2]:
                        sigs.append(s)
                if sigs:
                    lines.append(f"- 信号: {'; '.join(sigs[:3])}")

                if op:
                    lines.append(f"- 操作: **{op.get('action_name','')}** | 仓位: **{op.get('position_pct',0)*100:.1f}%**")
                    lines.append(f"- 时机: {op.get('timing','')}")
                    price_r = op.get('price_range', '')
                    if price_r:
                        lines.append(f"- 价格: {price_r}")
                    sl = op.get('stop_loss', 0)
                    tp1 = op.get('take_profit_1', 0)
                    if sl and tp1:
                        lines.append(f"- 止损/止盈: {sl:.2f} / {tp1:.2f}")
                lines.append("")

    # ── 7. 智能推荐（补充） ──
    if recommendations:
        shown_codes = set(r['code'] for r in (certainty_recs or [])[:5])
        extra = [r for r in recommendations if r.get('code') not in shown_codes]
        if extra:
            lines.append("## 📋 其他关注\n")
            for i, r in enumerate(extra[:3], 1):
                wr = r.get('win_rate', 0) or r.get('historical_win_rate', 0)
                lines.append(f"{i}. **{r.get('name','')}**({r.get('code','')}) "
                           f"得分{r.get('total_score',0)} 胜率{wr:.0%}")
                reason = r.get('reason', '')
                if reason:
                    lines.append(f"   - {reason[:80]}")
                risks = r.get('risk_notes', [])
                if risks:
                    lines.append(f"   - ⚠️ {'; '.join(risks[:2])}")
            lines.append("")

    # ── 8. 风险提示 ──
    lines.append("---\n")
    lines.append("> ⚠️ 以上为系统量化分析，不构成投资建议。"
                 "校准胜率基于2026年7-8月真实数据回测，不代表未来表现。"
                 "严格止损，仓位控制。")

    content = "\n".join(lines)
    return notifier.send_notification(title, content)


class TradingDayChecker:
    @staticmethod
    def is_trading_day(date_str: str = None) -> tuple:
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if dt.weekday() >= 5:
            return False, f"{date_str} 是周末"
        return True, f"{date_str} 是交易日"


def get_next_trading_day(date_str: str) -> str:
    try:
        db = Database(DB_PATH)
        all_dates = db.get_all_dates()
        if date_str not in all_dates:
            for d in all_dates:
                if d > date_str:
                    return d
            return None
        idx = all_dates.index(date_str)
        if idx + 1 < len(all_dates):
            return all_dates[idx + 1]
        return None
    except Exception as e:
        logger.error(f"获取下一交易日失败: {e}")
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
            return dt.strftime("%Y-%m-%d")
        except:
            return None


def _determine_analysis_date(preferred_date=None):
    today = datetime.now().strftime("%Y-%m-%d")
    data_date = preferred_date
    if not data_date:
        try:
            db = Database(DB_PATH)
            conn = db.conn
            cur = conn.execute("SELECT COUNT(*) FROM xgt_limit_up_detail WHERE date = ?", (today,))
            if cur.fetchone()[0] > 0:
                data_date = today
            else:
                cur = conn.execute("SELECT MAX(date) FROM xgt_limit_up_detail")
                row = cur.fetchone()
                data_date = row[0] if row else None
            db.close()
        except:
            data_date = None
    if not data_date:
        return None, None
    target_date = get_next_trading_day(data_date)
    return data_date, target_date


def run_task(task_id, task_type, params=None):
    with tasks_lock:
        tasks[task_id] = {
            "id": task_id,
            "type": task_type,
            "status": "running",
            "progress": 0,
            "message": "启动中...",
            "result": None,
            "created_at": datetime.now().isoformat()
        }
    try:
        if task_type == "daily":
            _run_daily_task(task_id)
        elif task_type == "fetch":
            _run_fetch_task(task_id, params or {})
        elif task_type == "backtest":
            _run_backtest_task(task_id, params or {})
        elif task_type == "recommend":
            _run_recommend_task(task_id, params or {})
        elif task_type == "track":
            _run_track_task(task_id, params or {})
        elif task_type == "auto_upgrade":
            _run_auto_upgrade_task(task_id, params or {})
        elif task_type == "simulate":
            _run_simulate_task(task_id, params or {})
    except Exception as e:
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["message"] = str(e)
        logger.error(f"任务 {task_id} 异常: {e}", exc_info=True)


def _run_fetch_task(task_id, params):
    from main import run_fetch
    date_str = params.get("date")
    with tasks_lock:
        tasks[task_id]["message"] = f"获取数据{'('+date_str+')' if date_str else ''}..."
        tasks[task_id]["progress"] = 10
    result = run_fetch(date_str=date_str)
    with tasks_lock:
        if result and result > 0:
            tasks[task_id]["progress"] = 70
            tasks[task_id]["result"] = {"fetched_count": result}
        else:
            tasks[task_id]["progress"] = 100
            tasks[task_id]["status"] = "completed"
            tasks[task_id]["message"] = "未获取到新数据（可能非交易日）"
            tasks[task_id]["result"] = {"fetched_count": 0}
            return

    if result and result > 0 and _smart_recommender:
        try:
            with tasks_lock:
                tasks[task_id]["message"] = "数据获取成功，正在生成明日预测..."
                tasks[task_id]["progress"] = 75
            data_date, target_date = _determine_analysis_date(date_str)
            if data_date:
                if _live_tracker:
                    try:
                        _live_tracker.evaluate_signals(data_date, DB_PATH)
                    except:
                        pass
                market_state = _smart_recommender.analyze_current_market(data_date, DB_PATH)
                recs = _smart_recommender.generate_recommendations(data_date, top_n=5, db_path=DB_PATH)
                next_day = _smart_recommender.recommend_for_next_day(data_date, DB_PATH)
                rec_serialized = []
                for r in recs:
                    rec_serialized.append({
                        "code": r.get("code", ""),
                        "name": r.get("name", ""),
                        "total_score": r.get("total_score", 0),
                        "win_rate": r.get("win_rate", 0),
                        "grade": r.get("grade", ""),
                        "reason": r.get("reason", ""),
                        "risk_notes": r.get("risk_notes", []),
                        "suggested_action": r.get("suggested_action", ""),
                        "concept": r.get("concept", ""),
                        "limit_up_days": r.get("limit_up_days", 1),
                        "dimension_scores": r.get("dimension_scores", {}),
                        "dimension_reasons": r.get("dimension_reasons", {}),
                        "confidence_level": r.get("confidence_level", "C"),
                        "confidence_name": r.get("confidence_name", "C级·中等"),
                        "historical_win_rate": r.get("historical_win_rate", 0.50),
                        "condition_match": r.get("condition_match", ""),
                    })
                with tasks_lock:
                    tasks[task_id]["result"]["recommendations"] = rec_serialized
                    tasks[task_id]["result"]["date"] = data_date
                    tasks[task_id]["result"]["data_date"] = data_date
                    tasks[task_id]["result"]["target_date"] = target_date
                    tasks[task_id]["result"]["market_state"] = {
                        "cycle_phase": market_state.get("cycle_phase", ""),
                        "smash_coefficient": market_state.get("smash_coefficient"),
                        "smash_trend": market_state.get("smash_trend", ""),
                        "explosion_rate": market_state.get("explosion_rate", 0),
                        "hot_concepts_top5": market_state.get("hot_concepts_top5", []),
                        "max_boards": market_state.get("max_boards", 0),
                        "limit_up_count": market_state.get("limit_up_count", 0),
                        "sentiment": market_state.get("sentiment", ""),
                        "cap_preference": market_state.get("cap_preference", ""),
                    }
                    tasks[task_id]["result"]["next_day_strategy"] = {
                        "target_date": next_day.get("target_date", target_date),
                        "target_board_height": next_day.get("target_board_height", ""),
                        "focus_concepts": next_day.get("focus_concepts", []),
                        "risk_control": next_day.get("risk_control", ""),
                        "overall_strategy": next_day.get("overall_strategy", ""),
                    }
                    tasks[task_id]["progress"] = 100
                    tasks[task_id]["status"] = "completed"
                    predict_msg = f"（预测{target_date}）" if target_date else ""
                    tasks[task_id]["message"] = f"数据获取成功，基于{data_date}生成{len(rec_serialized)}只个股预测{predict_msg}"
            else:
                with tasks_lock:
                    tasks[task_id]["progress"] = 100
                    tasks[task_id]["status"] = "completed"
                    tasks[task_id]["message"] = "数据获取成功，但无法确定最新交易日（数据库无数据）"
        except Exception as e:
            logger.error(f"自动推荐失败: {e}", exc_info=True)
            with tasks_lock:
                tasks[task_id]["progress"] = 100
                tasks[task_id]["status"] = "completed"
                tasks[task_id]["message"] = f"数据获取成功，但自动推荐失败: {e}"
    else:
        with tasks_lock:
            tasks[task_id]["progress"] = 100
            tasks[task_id]["status"] = "completed"
            tasks[task_id]["message"] = f"数据获取成功: {result} 条（智能推荐模块未加载）"


# ============ ★ 修改：_run_daily_task 存储报告字段 ============
def _run_daily_task(task_id):
    from main import run_daily
    with tasks_lock:
        tasks[task_id]["message"] = "执行每日分析..."
        tasks[task_id]["progress"] = 10
    result = run_daily()
    with tasks_lock:
        tasks[task_id]["progress"] = 100
        tasks[task_id]["status"] = "completed"
        tasks[task_id]["message"] = "每日分析完成"
        if result:
            tasks[task_id]["result"] = {
                "date": result.get("date"),
                "analysis": _serialize_analysis(result.get("analysis", {})),
                "predictions": _serialize_predictions(result.get("predictions", [])),
                "patterns": result.get("patterns", {}),
                # ★ 新增报告字段
                "capital_flow_report": result.get("capital_flow_report", ""),
                "dragon_report": result.get("dragon_report", ""),
                "plan_report": result.get("plan_report", ""),
            }
        else:
            tasks[task_id]["result"] = {"error": "分析无结果"}


def _run_backtest_task(task_id, params):
    from main import run_backtest
    max_days = params.get("max_days", 30)
    with tasks_lock:
        tasks[task_id]["message"] = f"执行回测 (最多{max_days}天)..."
        tasks[task_id]["progress"] = 10
    result = run_backtest(max_days=max_days)
    with tasks_lock:
        tasks[task_id]["progress"] = 100
        tasks[task_id]["status"] = "completed"
        tasks[task_id]["message"] = "回测完成"
        if result:
            tasks[task_id]["result"] = {
                "total_days": result.get("total_days"),
                "total_predictions": result.get("total_predictions"),
                "total_verifications": result.get("total_verifications"),
                "report": result.get("report", ""),
                "results": _serialize_backtest_results(result.get("results", [])),
            }
        else:
            tasks[task_id]["result"] = {"error": "回测无结果"}


# ============ ★ 修改：_run_recommend_task 快速推荐（不跑完整run_daily） ============
def _run_recommend_task(task_id, params):
    if not _smart_recommender:
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["message"] = "智能推荐模块未加载"
        return
    date_str = params.get("date")
    top_n = params.get("top_n", 10)
    with tasks_lock:
        tasks[task_id]["message"] = "生成智能推荐..."
        tasks[task_id]["progress"] = 20
    try:
        data_date, target_date = _determine_analysis_date(date_str)
        if not data_date:
            with tasks_lock:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["message"] = "无法获取最新交易日"
            return

        # 快速推荐：直接调用smart_recommender（读取已有DB数据，不跑完整run_daily）
        market_state = _smart_recommender.analyze_current_market(data_date, DB_PATH)
        recs = _smart_recommender.generate_recommendations(data_date, top_n=top_n, db_path=DB_PATH)
        next_day = _smart_recommender.recommend_for_next_day(data_date, DB_PATH)
        actual_target = next_day.get("target_date", target_date)
        rec_serialized = []
        for r in recs:
            rec_serialized.append({
                "code": r.get("code", ""),
                "name": r.get("name", ""),
                "total_score": r.get("total_score", 0),
                "win_rate": r.get("win_rate", 0),
                "grade": r.get("grade", ""),
                "reason": r.get("reason", ""),
                "risk_notes": r.get("risk_notes", []),
                "suggested_action": r.get("suggested_action", ""),
                "concept": r.get("concept", ""),
                "limit_up_days": r.get("limit_up_days", 1),
                "dimension_scores": r.get("dimension_scores", {}),
                "dimension_reasons": r.get("dimension_reasons", {}),
                "confidence_level": r.get("confidence_level", "C"),
                "confidence_name": r.get("confidence_name", "C级·中等"),
                "historical_win_rate": r.get("historical_win_rate", 0.50),
                "condition_match": r.get("condition_match", ""),
                "dragon_info": r.get("dragon_info"),
                "is_yizi": r.get("is_yizi", False),
                "divergence_state": r.get("divergence_state", ""),
                "divergence_label": r.get("divergence_label", ""),
                "seal_ratio": r.get("seal_ratio", 0),
                "vp_grade": r.get("vp_grade"),
                "vp_pattern": r.get("vp_pattern", ""),
                "vp_gate": r.get("vp_gate", ""),
                "vp_score": r.get("vp_score"),
                "vp_veto": r.get("vp_veto", []) if isinstance(r.get("vp_veto"), list) else [],
            })
        with tasks_lock:
            tasks[task_id]["progress"] = 100
            tasks[task_id]["status"] = "completed"
            predict_msg = f"（基于{data_date}数据，预测{actual_target}）" if actual_target else ""
            tasks[task_id]["message"] = f"推荐完成: {len(rec_serialized)}只个股{predict_msg}"
            tasks[task_id]["result"] = {
                "date": data_date,
                "data_date": data_date,
                "target_date": actual_target,
                "market_state": {
                    "cycle_phase": market_state.get("cycle_phase", ""),
                    "smash_coefficient": market_state.get("smash_coefficient"),
                    "smash_trend": market_state.get("smash_trend", ""),
                    "explosion_rate": market_state.get("explosion_rate", 0),
                    "hot_concepts_top5": market_state.get("hot_concepts_top5", []),
                    "max_boards": market_state.get("max_boards", 0),
                    "limit_up_count": market_state.get("limit_up_count", 0),
                    "limit_down_count": market_state.get("limit_down_count", 0),
                    "sentiment": market_state.get("sentiment", ""),
                    "cap_preference": market_state.get("cap_preference", ""),
                    "action_advice": (market_state.get("action_advice") or {}).get("advice_text", "") if isinstance(market_state.get("action_advice"), dict) else str(market_state.get("action_advice", "")),
                },
                "recommendations": rec_serialized,
                "next_day_strategy": {
                    "target_date": actual_target,
                    "target_board_height": next_day.get("target_board_height", ""),
                    "focus_concepts": next_day.get("focus_concepts", []),
                    "risk_control": next_day.get("risk_control", ""),
                    "overall_strategy": next_day.get("overall_strategy", ""),
                },
            }
    except Exception as e:
        logger.error(f"推荐任务异常: {e}", exc_info=True)
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["message"] = f"推荐失败: {e}"


def _run_track_task(task_id, params):
    if not _live_tracker:
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["message"] = "实盘跟踪模块未加载"
        return
    date_str = params.get("date")
    with tasks_lock:
        tasks[task_id]["message"] = "执行实盘跟踪..."
        tasks[task_id]["progress"] = 20
    try:
        if not date_str:
            date_str = _live_tracker.get_latest_date(db_path=DB_PATH)
        if not date_str:
            with tasks_lock:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["message"] = "无法获取最新交易日"
            return
        tracking = _live_tracker.track_daily(date_str, DB_PATH)
        signals = _live_tracker.evaluate_signals(date_str, DB_PATH)
        cum_stats = _live_tracker.get_cumulative_stats(DB_PATH)
        with tasks_lock:
            tasks[task_id]["progress"] = 100
            tasks[task_id]["status"] = "completed"
            tasks[task_id]["message"] = "跟踪完成"
            tasks[task_id]["result"] = {
                "date": date_str,
                "tracking": {
                    "recommendations_count": tracking.get("recommendations_count", 0),
                    "correct_count": tracking.get("correct_count", 0),
                    "win_rate": tracking.get("win_rate", 0),
                    "cumulative_win_rate": tracking.get("cumulative_win_rate", 0),
                    "details": tracking.get("details", []),
                },
                "signals": {
                    "triggered": signals.get("signals_triggered", []),
                    "not_triggered": signals.get("signals_not_triggered", []),
                    "newly_verified": signals.get("newly_verified", []),
                },
                "cumulative": cum_stats,
            }
    except Exception as e:
        logger.error(f"跟踪任务异常: {e}", exc_info=True)
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["message"] = f"跟踪失败: {e}"


def _run_auto_upgrade_task(task_id, params):
    if not _self_upgrader:
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["message"] = "自适应升级模块未加载"
        return
    with tasks_lock:
        tasks[task_id]["message"] = "执行自适应升级..."
        tasks[task_id]["progress"] = 20
    try:
        check_only = params.get("check_only", False)
        accuracy = _self_upgrader.analyze_prediction_accuracy(days=30, db_path=DB_PATH)
        weight_result = _self_upgrader.adjust_weights(DB_PATH, check_only=check_only)
        regime_result = _self_upgrader.detect_regime_change(DB_PATH, check_only=check_only)
        with tasks_lock:
            tasks[task_id]["progress"] = 100
            tasks[task_id]["status"] = "completed"
            tasks[task_id]["message"] = "自适应升级完成"
            tasks[task_id]["result"] = {
                "accuracy": accuracy if not accuracy.get('error') else {'error': accuracy.get('error')},
                "weight_adjust": {
                    "old_weights": weight_result.get("old_weights", {}),
                    "new_weights": weight_result.get("new_weights", {}),
                    "adjustments": weight_result.get("adjustments", []),
                    "reason": weight_result.get("reason", ""),
                },
                "regime": {
                    "current_regime": regime_result.get("current_regime", ""),
                    "prev_regime": regime_result.get("prev_regime", ""),
                    "is_changed": regime_result.get("is_changed", False),
                    "evidence": regime_result.get("evidence", []),
                    "recommended_adjustments": regime_result.get("recommended_adjustments", {}),
                },
            }
    except Exception as e:
        logger.error(f"升级任务异常: {e}", exc_info=True)
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["message"] = f"升级失败: {e}"


def _run_simulate_task(task_id, params):
    from simulator import Simulator
    with tasks_lock:
        tasks[task_id]["message"] = "启动模拟交易..."
        tasks[task_id]["progress"] = 10

    try:
        start_date = params.get('start_date')
        end_date = params.get('end_date')
        init_cash = params.get('init_cash', 1000000)
        grade_filter = params.get('grade_filter', ['S', 'A'])
        take_profit = params.get('take_profit', 0.20)
        stop_loss = params.get('stop_loss', 0.07)
        max_positions = params.get('max_positions', 5)
        position_pct = params.get('position_pct', 0.2)

        if not end_date:
            db = Database(DB_PATH)
            all_dates = db.get_all_dates()
            end_date = all_dates[-1] if all_dates else datetime.now().strftime("%Y-%m-%d")
            db.close()
        if not start_date:
            start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=180)).strftime("%Y-%m-%d")

        with tasks_lock:
            tasks[task_id]["message"] = f"正在回测 {start_date} 至 {end_date}..."
            tasks[task_id]["progress"] = 30

        sim = Simulator(
            start_date=start_date,
            end_date=end_date,
            init_cash=init_cash,
            grade_filter=grade_filter,
            take_profit=take_profit,
            stop_loss=stop_loss,
            max_positions=max_positions,
            position_pct=position_pct
        )
        result = sim.run()

        with tasks_lock:
            tasks[task_id]["progress"] = 100
            tasks[task_id]["status"] = "completed"
            tasks[task_id]["message"] = "模拟交易回测完成"
            tasks[task_id]["result"] = result

    except Exception as e:
        logger.error(f"模拟交易回测异常: {e}", exc_info=True)
        with tasks_lock:
            tasks[task_id]["status"] = "error"
            tasks[task_id]["message"] = str(e)


def _serialize_analysis(analysis):
    if not analysis:
        return {}
    result = {}
    for k, v in analysis.items():
        if isinstance(v, (str, int, float, bool, list, dict, type(None))):
            result[k] = v
        else:
            result[k] = str(v)
    return result


def _serialize_predictions(predictions):
    if isinstance(predictions, dict):
        result = {}
        for k, v in predictions.items():
            if isinstance(v, dict):
                item = {}
                for kk, vv in v.items():
                    if isinstance(vv, (str, int, float, bool, list, dict, type(None))):
                        item[kk] = vv
                    else:
                        item[kk] = str(vv)
                result[k] = item
            else:
                result[k] = {"predicted": str(v) if v is not None else "--"}
        return result
    elif isinstance(predictions, list):
        result = []
        for p in predictions:
            if isinstance(p, dict):
                item = {}
                for k, v in p.items():
                    if isinstance(v, (str, int, float, bool, list, dict, type(None))):
                        item[k] = v
                    else:
                        item[k] = str(v)
                result.append(item)
            else:
                result.append(str(p))
        return result
    return predictions


def _serialize_backtest_results(results):
    serialized = []
    for r in results:
        item = {"date": r.get("date"), "predictions": r.get("predictions", 0)}
        verifications = r.get("verifications", [])
        if verifications:
            scores = [v.get("score", 0) for v in verifications if isinstance(v, dict)]
            item["avg_score"] = round(sum(scores) / len(scores), 3) if scores else 0
            item["verification_count"] = len(verifications)
        else:
            item["avg_score"] = 0
            item["verification_count"] = 0
        serialized.append(item)
    return serialized


# ============ API 处理 ============

def handle_dashboard():
    """
    仪表盘数据接口 - 直接从最新分析结果获取所有数据
    确保数据实时一致，不依赖快照表
    """
    try:
        db = Database(DB_PATH)
        db.init_new_tables()

        from market_analyzer import MarketAnalyzer
        from predictor import Predictor
        from knowledge_base import KnowledgeBase

        analyzer = MarketAnalyzer(db)
        # 数据日期锚点：三个页面统一以涨停明细表最新日期为准（与每日分析/智能推荐同源）
        latest_date = None
        try:
            _row = db.conn.execute("SELECT MAX(date) AS d FROM xgt_limit_up_detail").fetchone()
            latest_date = _row[0] if _row else None
        except Exception:
            pass
        if not latest_date:
            all_dates = db.get_all_dates()
            latest_date = all_dates[-1] if all_dates else None

        analysis_summary = {}
        predictions = {}
        smash_chart = []

        if latest_date:
            analysis = analyzer.analyze_date(latest_date)

            if analysis:
                basic = analysis.get("basic_stats", {})
                smash_info = analysis.get("smash_analysis", {})
                sentiment = analysis.get("sentiment_score", 0)

                cycle_phase = smash_info.get("cycle_phase_by_smash", "")

                if not cycle_phase:
                    snapshots = db.get_daily_snapshots(limit=1)
                    if snapshots:
                        snap = dict(snapshots[0])
                        cycle_phase = snap.get("cycle_phase", "")

                analysis_summary = {
                    "date": latest_date,
                    "limit_up_count": basic.get("total_count", 0),
                    "max_continuous_boards": basic.get("max_boards", 0),
                    "smash_coefficient": smash_info.get("smash_coefficient"),
                    "sentiment_score": sentiment,
                    "avg_seal_amount": basic.get("avg_seal_amount", 0),
                    "cycle_phase": cycle_phase,
                    "main_concept": "",
                    "smash_signal": smash_info.get("signal", ""),
                    "smash_trade_advice": smash_info.get("trade_advice", ""),
                    "smash_trend": smash_info.get("trend", ""),
                    "smash_advantage": smash_info.get("advantage", ""),
                    "board_tiers": analysis.get("board_tiers", {}),
                    "seal_quality": analysis.get("seal_quality", {}),
                    "concept_heat": analysis.get("concept_heat", {}),
                }

                # 涨停数兜底：分析器口径缺失/为0时直接取明细表计数，保证三页数字一致
                if not analysis_summary.get("limit_up_count"):
                    try:
                        _cnt = db.conn.execute(
                            "SELECT COUNT(*) FROM xgt_limit_up_detail WHERE date=?",
                            (latest_date,)).fetchone()
                        analysis_summary["limit_up_count"] = _cnt[0] if _cnt else 0
                    except Exception:
                        pass

                # 最高连板统一为 board_calculator 真实口径（与砸盘图表/连板梯队/
                # 进场确定性/龙头/推荐同源），避免 MarketAnalyzer 直接用 API 的
                # limit_up_days 字段（14.6%不匹配）导致卡片6板、图表7板的口径冲突
                try:
                    from board_calculator import BoardCalculator
                    _bc = BoardCalculator(db.conn)
                    _real_max = _bc.get_daily_max_boards(latest_date, db.conn)
                    if _real_max and _real_max > 0:
                        analysis_summary["max_continuous_boards"] = _real_max
                except Exception as e:
                    logger.warning(f"真实最高连板口径统一失败: {e}")

                try:
                    kb = KnowledgeBase(db)
                    predictor = Predictor(db, kb)
                    preds = predictor.predict_next_day(latest_date, analysis, {})
                    predictions = _serialize_predictions(preds)
                except Exception as e:
                    logger.warning(f"预测生成失败: {e}")

        smash_history = db.get_smash_coefficient_history(limit=30)
        smash_data = [dict(s) for s in smash_history]
        for s in reversed(smash_data):
            smash_chart.append({
                "date": s["date"],
                "value": s["smash_coefficient"],
                "max_boards": s["max_continuous_boards"]
            })

        turning_points_data = {"series": [], "turning_points": [],
                               "dragon_birth_nodes": [], "summary": {}}
        try:
            from turning_point_detector import detect_turning_points
            turning_points_data = detect_turning_points(days=30)
        except Exception as e:
            logger.warning(f"变盘节点检测失败: {e}")

        # 市场量价环境（整体量价走势=筛选/进场首要依据，仪表盘展示闸门状态）
        volume_price_market = None
        try:
            from volume_price_analyzer import analyze_market_volume_price
            if latest_date:
                volume_price_market = analyze_market_volume_price(latest_date, DB_PATH)
        except Exception as e:
            logger.warning(f"市场量价环境分析失败: {e}")

        db.close()

        return {
            "success": True,
            "data": {
                "summary": analysis_summary,
                "smash_chart": smash_chart,
                "turning_points": turning_points_data,
                "predictions": predictions,
                "volume_price_market": volume_price_market
            }
        }
    except Exception as e:
        logger.error(f"Dashboard error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def handle_start_fetch(body):
    data = json.loads(body) if body else {}
    date_str = data.get("date", None)
    task_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(target=run_task, args=(task_id, "fetch", {"date": date_str}))
    thread.daemon = True
    thread.start()
    return {"success": True, "task_id": task_id}


def handle_start_daily(body):
    task_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(target=run_task, args=(task_id, "daily"))
    thread.daemon = True
    thread.start()
    return {"success": True, "task_id": task_id}


def handle_task_status(params):
    task_id = params.get("task_id", [None])[0]
    if not task_id:
        return {"success": False, "error": "缺少 task_id"}
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return {"success": False, "error": "任务不存在"}
    return {"success": True, "data": {
        "id": task["id"],
        "status": task["status"],
        "progress": task["progress"],
        "message": task["message"],
        "result": task["result"],
    }}


def handle_start_backtest(body):
    data = json.loads(body) if body else {}
    max_days = data.get("max_days", 30)
    task_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(target=run_task, args=(task_id, "backtest", {"max_days": max_days}))
    thread.daemon = True
    thread.start()
    return {"success": True, "task_id": task_id}


def handle_reports():
    try:
        reports_dir = os.path.join(PROJECT_DIR, "reports")
        if not os.path.exists(reports_dir):
            return {"success": True, "data": []}
        reports = []
        for f in sorted(os.listdir(reports_dir), reverse=True):
            if f.endswith(".md") or f.endswith(".txt"):
                filepath = os.path.join(reports_dir, f)
                stat = os.stat(filepath)
                date_str = f.replace("report_", "").replace(".md", "").replace(".txt", "")
                reports.append({
                    "filename": f,
                    "date": date_str,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
        return {"success": True, "data": reports}
    except Exception as e:
        return {"success": False, "error": str(e)}


def handle_report_detail(date_str):
    try:
        reports_dir = os.path.join(PROJECT_DIR, "reports")
        filename = f"report_{date_str}.md"
        filepath = os.path.join(reports_dir, filename)
        if not os.path.exists(filepath):
            filename = f"report_{date_str}.txt"
            filepath = os.path.join(reports_dir, filename)
        if not os.path.exists(filepath):
            for f in os.listdir(reports_dir):
                if date_str in f and (f.endswith(".md") or f.endswith(".txt")):
                    filepath = os.path.join(reports_dir, f)
                    break
        if not os.path.exists(filepath):
            return {"success": False, "error": "报告不存在"}
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return {"success": True, "data": {"content": content, "filename": os.path.basename(filepath)}}
    except Exception as e:
        return {"success": False, "error": str(e)}


def handle_smash_history(params):
    try:
        limit = int(params.get("limit", [30])[0])
        db = Database(DB_PATH)
        history = db.get_smash_coefficient_history(limit=limit)
        data = []
        for h in reversed(list(history)):
            h = dict(h)
            data.append({"date": h["date"], "value": h["smash_coefficient"], "max_boards": h["max_continuous_boards"]})
        db.close()
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


def handle_turning_points(params):
    try:
        days = int(params.get("days", [30])[0])
        days = max(7, min(days, 90))
        from turning_point_detector import detect_turning_points
        result = detect_turning_points(days=days)
        return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"turning_points error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def handle_dragon_imminent(params):
    try:
        date_str = params.get("date", [None])[0]
        from turning_point_detector import detect_dragon_imminent
        result = detect_dragon_imminent(date_str)
        return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"dragon_imminent error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def handle_entry_certainty(params):
    try:
        date_str = params.get("date", [None])[0]
        top_n = int(params.get("top_n", [20])[0])
        db = Database(DB_PATH)
        if date_str:
            rows = db.fetch_all(
                "SELECT * FROM entry_certainty_analysis WHERE date=? "
                "ORDER BY composite_score DESC LIMIT ?",
                (date_str, top_n))
        else:
            date_str = db.fetch_one(
                "SELECT MAX(date) as d FROM entry_certainty_analysis")
            date_str = date_str['d'] if date_str else None
            if not date_str:
                return {"success": True, "data": {"date": None, "results": []}}
            rows = db.fetch_all(
                "SELECT * FROM entry_certainty_analysis WHERE date=? "
                "ORDER BY composite_score DESC LIMIT ?",
                (date_str, top_n))
        db.close()

        # 行 → 字典 + JSON 字段解析
        results = []
        for r in rows:
            r = dict(r)
            for k in ('conditions', 'signals', 'risks'):
                if r.get(k):
                    try:
                        r[k] = json.loads(r[k])
                    except Exception:
                        r[k] = []
                else:
                    r[k] = []
            if r.get('dim_detail'):
                try:
                    r['dim_detail'] = json.loads(r['dim_detail'])
                except Exception:
                    r['dim_detail'] = None
            else:
                r['dim_detail'] = None
            results.append(r)

        # ── 质量门槛（宁缺毋滥）：只展示 B 级及以上（校准胜率跑赢20.4%基准），
        # C/D 级（无超额胜率）不进面板；量价否决也剔除。低分无题材票曾导致
        # "12只B级20分票"刷屏——根因是旧B级门槛低于随机基准，已在分析层重定。
        _KEEP_GRADES = ('S+', 'S', 'A', 'B')
        def _passes_gate(r):
            grade = (r.get('certainty_grade') or 'D').upper()
            if grade not in _KEEP_GRADES:
                return False
            # 题材维度过低（无明确概念标签/无板块联动）直接剔除
            if (r.get('theme_score') is not None and r.get('theme_score') < 40):
                return False
            # 量价闸门 fail（一票否决）不进确定性面板
            dd = r.get('dim_detail')
            if isinstance(dd, dict):
                vp = dd.get('volume_price')
                if isinstance(vp, dict) and vp.get('grade') == 'fail':
                    return False
            return True

        qualified = [r for r in results if _passes_gate(r)]
        filtered_out = len(results) - len(qualified)
        return {"success": True, "data": {
            "date": date_str,
            "results": qualified,
            "total_analyzed": len(results),
            "filtered_out": filtered_out,
            "gate_rule": "已过滤C/D级(校准胜率未跑赢20.4%涨停基准)/题材<40(无板块联动)/量价否决 的低确定性标的",
        }}
    except Exception as e:
        logger.error(f"entry_certainty error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def handle_model_health():
    try:
        db = Database(DB_PATH)
        db.init_new_tables()
        db.init_default_weights()
        from knowledge_base import KnowledgeBase
        from self_corrector import SelfCorrector
        kb = KnowledgeBase(db)
        corrector = SelfCorrector(db, kb)
        health = corrector.get_model_health()
        correction_logs = db.fetch_all("SELECT * FROM correction_log ORDER BY created_at DESC LIMIT 50")
        logs = [dict(c) for c in correction_logs]
        weights = db.get_all_weights()
        weight_details = []
        for w in weights:
            w = dict(w)
            history = json.loads(w.get("history", "[]")) if w.get("history") else []
            weight_details.append({
                "name": w["factor_name"],
                "weight": w["weight"],
                "credibility": w.get("credibility", 1.0) or 1.0,
                "consecutive_misses": w.get("consecutive_misses", 0),
                "history": history[-20:],
            })
        db.close()
        return {"success": True, "data": {"health": health, "correction_logs": logs, "weight_details": weight_details}}
    except Exception as e:
        logger.error(f"Model health error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ============ 最新分析结果 API（从数据库读取，无需重跑） ============

def _latest_date_from_table(table, date_col='date'):
    """安全获取某张表的最新日期"""
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT MAX({date_col}) as d FROM {table}").fetchone()
        conn.close()
        return row['d'] if row and row['d'] else None
    except Exception:
        return None


def handle_daily_latest(params):
    """每日分析页：从数据库读取最新结构化分析结果（资金流/龙头/操作计划/进场确定性）"""
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        date_str = params.get("date", [None])[0]

        # 确定最新日期（以涨停明细表为准）
        if not date_str:
            row = conn.execute("SELECT MAX(date) as d FROM xgt_limit_up_detail").fetchone()
            date_str = row['d'] if row and row['d'] else None
        if not date_str:
            conn.close()
            return {"success": True, "data": {"date": None, "has_data": False, "message": "暂无涨停数据，请先获取数据"}}

        result = {"date": date_str, "has_data": True}

        # ── 1. 基础统计 ──
        summary = conn.execute(
            "SELECT * FROM xgt_daily_summary WHERE date = ?", (date_str,)
        ).fetchone()
        result["summary"] = dict(summary) if summary else {}

        # ── 2. 砸盘系数 ──
        smash = conn.execute(
            "SELECT * FROM smash_coefficients WHERE trade_date = ?", (date_str,)
        ).fetchone()
        result["smash"] = dict(smash) if smash else {}

        # ── 3. 资金流分析（结构化） ──
        try:
            cf = conn.execute(
                "SELECT * FROM capital_flow_analysis WHERE date = ?", (date_str,)
            ).fetchone()
            if cf:
                cf = dict(cf)
                for k in ('attack_metrics', 'persistence_metrics', 'rotation_metrics',
                          'combo_signals', 'full_result'):
                    if cf.get(k):
                        try:
                            cf[k] = json.loads(cf[k])
                        except Exception:
                            cf[k] = None
                result["capital_flow"] = cf
            else:
                result["capital_flow"] = None
        except Exception as e:
            logger.warning(f"capital_flow read skipped: {e}")
            result["capital_flow"] = None

        # ── 4. 龙头识别（结构化列表） ──
        dragon_list = []
        try:
            dragons = conn.execute(
                "SELECT * FROM dragon_detections WHERE detect_date = ? "
                "ORDER BY total_score DESC LIMIT 15", (date_str,)
            ).fetchall()
            for d in dragons:
                d = dict(d)
                for k in ('reasons', 'risks'):
                    if d.get(k):
                        try:
                            d[k] = json.loads(d[k])
                        except Exception:
                            d[k] = []
                    else:
                        d[k] = []
                dragon_list.append(d)
        except Exception as e:
            logger.warning(f"dragon read skipped: {e}")
        result["dragons"] = dragon_list

        # ── 5. 操作计划（结构化列表） ──
        plan_list = []
        try:
            plans = conn.execute(
                "SELECT * FROM operation_plans WHERE plan_date = ? "
                "ORDER BY expected_return DESC", (date_str,)
            ).fetchall()
            for p in plans:
                p = dict(p)
                if p.get('plan_details'):
                    try:
                        p['plan_details'] = json.loads(p['plan_details'])
                    except Exception:
                        p['plan_details'] = {}
                else:
                    p['plan_details'] = {}
                plan_list.append(p)
        except Exception as e:
            logger.warning(f"operation_plans read skipped: {e}")
        result["operation_plans"] = plan_list

        # ── 6. 进场确定性（结构化列表，套用与仪表盘面板相同的质量门槛） ──
        eca_list = []
        try:
            eca = conn.execute(
                "SELECT * FROM entry_certainty_analysis WHERE date = ? "
                "ORDER BY composite_score DESC LIMIT 15", (date_str,)
            ).fetchall()
            for r in eca:
                r = dict(r)
                for k in ('conditions', 'signals', 'risks', 'dimensions'):
                    if r.get(k):
                        try:
                            r[k] = json.loads(r[k])
                        except Exception:
                            r[k] = [] if k != 'dimensions' else {}
                    else:
                        r[k] = [] if k != 'dimensions' else {}
                # 质量门槛：只保留 B 级及以上（校准胜率跑赢20.4%基准），
                # 题材分<40（无板块联动）不展示
                _grade = (r.get('certainty_grade') or 'D').upper()
                _theme = r.get('theme_score')
                if _grade not in ('S+', 'S', 'A', 'B'):
                    continue
                if _theme is not None and _theme < 40:
                    continue
                eca_list.append(r)
        except Exception as e:
            logger.warning(f"entry_certainty read skipped: {e}")
        result["entry_certainty"] = eca_list

        # ── 7. 周期上下文 ──
        cycle = conn.execute(
            "SELECT * FROM dragon_cycle_context WHERE date = ?", (date_str,)
        ).fetchone()
        if cycle:
            c = dict(cycle)
            if c.get('details'):
                try:
                    c['details'] = json.loads(c['details'])
                except Exception:
                    c['details'] = {}
            result["cycle_context"] = c
        else:
            result["cycle_context"] = None

        # ── 8. 概念热度 TOP10 ──
        concepts = conn.execute(
            "SELECT concept, count FROM concept_statistics "
            "WHERE date = ? AND concept NOT LIKE '%炸板%' "
            "ORDER BY count DESC LIMIT 10", (date_str,)
        ).fetchall()
        result["concept_heat"] = [dict(r) for r in concepts]

        # ── 9. 连板梯队（统一 board_calculator 真实连板口径，与最高连板卡/砸盘图表一致）──
        tiers = []
        try:
            from board_calculator import BoardCalculator
            _bc = BoardCalculator(conn)
            _stocks = _bc.get_daily_stocks(date_str, conn)
            _tier_map = {}
            for s in _stocks:
                b = s.get('consecutive_boards') or 1
                _tier_map.setdefault(b, []).append(s.get('name', ''))
            tiers = [
                {"limit_up_days": b, "cnt": len(names),
                 "stocks": ','.join([n for n in names if n][:12])}
                for b, names in sorted(_tier_map.items(), reverse=True)
            ]
        except Exception as e:
            logger.warning(f"board tiers 真实口径失败，回退SQL: {e}")
            _rows = conn.execute(
                "SELECT limit_up_days, COUNT(*) as cnt, "
                "GROUP_CONCAT(name) as stocks "
                "FROM xgt_limit_up_detail WHERE date = ? "
                "GROUP BY limit_up_days ORDER BY limit_up_days DESC",
                (date_str,)
            ).fetchall()
            tiers = [dict(r) for r in _rows]
        result["board_tiers"] = tiers

        conn.close()
        return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"daily latest error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def handle_recommend_latest(params):
    """智能推荐页：从数据库读取/快速计算最新推荐结果，不触发完整run_daily"""
    try:
        date_str = params.get("date", [None])[0]
        if not date_str:
            date_str = _latest_date_from_table('xgt_limit_up_detail', 'date')
        if not date_str:
            return {"success": True, "data": {"date": None, "has_data": False,
                    "message": "暂无涨停数据，请先获取数据后点击\"生成智能推荐\""}}

        if not _smart_recommender:
            return {"success": False, "error": "智能推荐模块未加载"}

        # 快速计算推荐（不跑完整run_daily）
        market_state = _smart_recommender.analyze_current_market(date_str, DB_PATH)
        recs = _smart_recommender.generate_recommendations(date_str, top_n=10, db_path=DB_PATH)
        next_day = _smart_recommender.recommend_for_next_day(date_str, DB_PATH)

        target_date = next_day.get("target_date", "") or next_day.get("date", "") or date_str
        rec_serialized = []
        for r in recs:
            rec_serialized.append({
                "code": r.get("code", ""),
                "name": r.get("name", ""),
                "total_score": r.get("total_score", 0),
                "win_rate": r.get("win_rate", 0),
                "grade": r.get("grade", ""),
                "reason": r.get("reason", ""),
                "risk_notes": r.get("risk_notes", []) if isinstance(r.get("risk_notes"), list) else [],
                "suggested_action": r.get("suggested_action", ""),
                "concept": r.get("concept", ""),
                "limit_up_days": r.get("limit_up_days", 1),
                "dimension_scores": r.get("dimension_scores", {}),
                "dimension_reasons": r.get("dimension_reasons", {}),
                "confidence_level": r.get("confidence_level", "C"),
                "confidence_name": r.get("confidence_name", "C级·中等"),
                "historical_win_rate": r.get("historical_win_rate", 0.50),
                "condition_match": r.get("condition_match", ""),
                "dragon_info": r.get("dragon_info"),
                "is_yizi": r.get("is_yizi", False),
                "divergence_state": r.get("divergence_state", ""),
                "divergence_label": r.get("divergence_label", ""),
                "seal_ratio": r.get("seal_ratio", 0),
            })

        # 从数据库读取进场确定性 + 操作计划（如果已有分析结果）
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        entry_certainty = []
        try:
            eca_rows = conn.execute(
                "SELECT * FROM entry_certainty_analysis WHERE date = ? "
                "ORDER BY composite_score DESC LIMIT 15", (date_str,)
            ).fetchall()
            for r in eca_rows:
                r = dict(r)
                for k in ('conditions', 'signals', 'risks'):
                    if r.get(k):
                        try:
                            r[k] = json.loads(r[k])
                        except Exception:
                            r[k] = []
                    else:
                        r[k] = []
                if r.get('dim_detail'):
                    try:
                        r['dim_detail'] = json.loads(r['dim_detail'])
                    except Exception:
                        r['dim_detail'] = None
                # 质量门槛：仅 B 级及以上（跑赢20.4%基准）+题材联动达标
                _g = (r.get('certainty_grade') or 'D').upper()
                if _g not in ('S+', 'S', 'A', 'B'):
                    continue
                if r.get('theme_score') is not None and r.get('theme_score') < 40:
                    continue
                _dd = r.get('dim_detail')
                if isinstance(_dd, dict) and isinstance(_dd.get('volume_price'), dict) \
                        and _dd['volume_price'].get('grade') == 'fail':
                    continue
                entry_certainty.append(r)
        except Exception:
            pass

        plans_map = {}
        try:
            plan_rows = conn.execute(
                "SELECT * FROM operation_plans WHERE plan_date = ?", (date_str,)
            ).fetchall()
            for p in plan_rows:
                p = dict(p)
                if p.get('plan_details'):
                    try:
                        p['plan_details'] = json.loads(p['plan_details'])
                    except Exception:
                        p['plan_details'] = {}
                plans_map[p['code']] = p
        except Exception:
            pass

        # 资金流
        cf_data = None
        try:
            cf_row = conn.execute(
                "SELECT * FROM capital_flow_analysis WHERE date = ?", (date_str,)
            ).fetchone()
            if cf_row:
                cf_data = dict(cf_row)
                for k in ('combo_signals', 'full_result'):
                    if cf_data.get(k):
                        try:
                            cf_data[k] = json.loads(cf_data[k])
                        except Exception:
                            pass
        except Exception:
            pass

        conn.close()

        return {"success": True, "data": {
            "date": date_str,
            "target_date": target_date,
            "has_data": True,
            "market_state": {
                "cycle_phase": market_state.get("cycle_phase", ""),
                "smash_coefficient": market_state.get("smash_coefficient"),
                "smash_trend": market_state.get("smash_trend", ""),
                "explosion_rate": market_state.get("explosion_rate", 0),
                "hot_concepts_top5": market_state.get("hot_concepts_top5", []),
                "max_boards": market_state.get("max_boards", 0),
                "limit_up_count": market_state.get("limit_up_count", 0),
                "limit_down_count": market_state.get("limit_down_count", 0),
                "sentiment": market_state.get("sentiment", ""),
                "cap_preference": market_state.get("cap_preference", ""),
                "action_advice": (
                    market_state.get("action_advice", {}).get("advice_text", "")
                    if isinstance(market_state.get("action_advice"), dict)
                    else str(market_state.get("action_advice", ""))
                ),
            },
            "recommendations": rec_serialized,
            "next_day_strategy": {
                "target_date": target_date,
                "target_board_height": next_day.get("target_board_height", ""),
                "focus_concepts": next_day.get("focus_concepts", []),
                "risk_control": next_day.get("risk_control", ""),
                "overall_strategy": next_day.get("overall_strategy", ""),
            },
            "entry_certainty": entry_certainty,
            "operation_plans": plans_map,
            "capital_flow": cf_data,
        }}
    except Exception as e:
        logger.error(f"recommend latest error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# V4 API
def handle_start_recommend(body):
    data = json.loads(body) if body else {}
    date_str = data.get("date", None)
    top_n = data.get("top_n", 10)
    task_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(target=run_task, args=(task_id, "recommend", {"date": date_str, "top_n": top_n}))
    thread.daemon = True
    thread.start()
    return {"success": True, "task_id": task_id}


def handle_start_track(body):
    data = json.loads(body) if body else {}
    date_str = data.get("date", None)
    task_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(target=run_task, args=(task_id, "track", {"date": date_str}))
    thread.daemon = True
    thread.start()
    return {"success": True, "task_id": task_id}


def handle_start_auto_upgrade(body):
    data = json.loads(body) if body else {}
    check_only = data.get("check_only", False)
    task_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(target=run_task, args=(task_id, "auto_upgrade", {"check_only": check_only}))
    thread.daemon = True
    thread.start()
    return {"success": True, "task_id": task_id}


def handle_start_simulate(body):
    data = json.loads(body) if body else {}
    task_id = str(uuid.uuid4())[:8]
    thread = threading.Thread(target=run_task, args=(task_id, "simulate", data))
    thread.daemon = True
    thread.start()
    return {"success": True, "task_id": task_id}


def handle_signals_data():
    try:
        signals_path = os.path.join(PROJECT_DIR, "discovered_signals.json")
        if os.path.exists(signals_path):
            with open(signals_path, "r", encoding="utf-8") as f:
                signals = json.load(f)
            return {"success": True, "data": signals}
        return {"success": True, "data": []}
    except Exception as e:
        return {"success": False, "error": str(e)}


def handle_signal_history(params):
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        signal_id = params.get("signal_id", [None])[0]
        if signal_id:
            rows = conn.execute("""
                SELECT * FROM signal_tracking
                WHERE signal_id = ? ORDER BY trigger_date DESC LIMIT 50
            """, (signal_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM signal_tracking
                ORDER BY trigger_date DESC LIMIT 100
            """).fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


def handle_recommendation_history(params):
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        limit = int(params.get("limit", [50])[0])
        rows = conn.execute("""
            SELECT * FROM recommendation_log
            ORDER BY rec_date DESC, score DESC LIMIT ?
        """, (limit,)).fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


def handle_weight_history():
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM weight_adjustment_log
            ORDER BY adjust_date DESC LIMIT 100
        """).fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


def handle_upgrade_logs():
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM upgrade_log
            ORDER BY created_at DESC LIMIT 50
        """).fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


def handle_regime_logs():
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM regime_detection_log
            ORDER BY detect_date DESC LIMIT 20
        """).fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


def handle_modules_status():
    return {"success": True, "data": _new_modules_status}


def handle_exit_signals(params):
    if _exit_strategy is None:
        return {"success": False, "error": "exit_strategy模块未加载"}
    try:
        date = params.get('date', [None])[0]
        market_advice = _exit_strategy.check_market_exit_signals(date=date)
        stock_advices = []
        latest_date = market_advice.get('date')
        if latest_date:
            try:
                recents = _exit_strategy.get_limit_up_stocks(latest_date)
                checked = set()
                for stock in recents:
                    if stock.get('limit_up_days', 1) >= 2 and stock['code'] not in checked:
                        checked.add(stock['code'])
                        advice = _exit_strategy.check_stock_exit_signals(
                            stock_code=stock['code'],
                            stock_name=stock.get('name', ''),
                            holding_days=0,
                            buy_price=None
                        )
                        stock_advices.append(advice)
            except Exception as e:
                logger.warning(f"获取个股出场信号失败: {e}")
        overall_action = 'NORMAL'
        if market_advice['market_exit_urgency'] == 'CRITICAL':
            overall_action = 'CLEAR_ALL'
        elif market_advice['market_exit_urgency'] == 'HIGH':
            overall_action = 'REDUCE'
        elif any(s['exit_urgency'] in ['CRITICAL', 'HIGH'] for s in stock_advices):
            overall_action = 'REDUCE'
        elif any(s['exit_recommended'] for s in stock_advices):
            overall_action = 'HOLD'
        return {
            "success": True,
            "data": {
                "date": latest_date,
                "market_advice": market_advice,
                "stock_advices": stock_advices,
                "overall_action": overall_action
            }
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}


# ============ 量化策略 API ============

def handle_quant_signals():
    try:
        from quant_strategy import QuantStrategyEngine
        db = Database(DB_PATH)
        all_dates = db.get_all_dates()
        latest_date = all_dates[-1] if all_dates else None
        db.close()
        if not latest_date:
            return {"success": False, "error": "无交易日数据"}
        engine = QuantStrategyEngine(DB_PATH)
        result = engine.generate_signals(latest_date)
        engine.close()
        return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"量化信号生成失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def handle_quant_backtest(body):
    try:
        from quant_strategy import QuantStrategyEngine, QuantBacktester
        data = json.loads(body) if body else {}
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        init_cash = data.get('init_cash', 1000000)
        if not start_date:
            from datetime import timedelta
            start_date = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
        if not end_date:
            end_date = datetime.now().strftime('%Y-%m-%d')
        engine = QuantStrategyEngine(DB_PATH)
        backtester = QuantBacktester(engine)
        result = backtester.run(start_date, end_date, init_cash)
        engine.close()
        return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"量化回测失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def handle_trade_order(body):
    try:
        from trading_executor import TradingExecutor, TradingChannel
        data = json.loads(body) if body else {}
        channel_name = os.environ.get('TRADING_CHANNEL', 'simulate')
        channel_map = {
            'qmt': TradingChannel.QMT,
            'ths_http': TradingChannel.THS_HTTP,
            'simulate': TradingChannel.SIMULATE
        }
        executor = TradingExecutor(
            channel=channel_map.get(channel_name, TradingChannel.SIMULATE),
            config={
                'miniqmt_path': os.environ.get('QMT_PATH', ''),
                'account': os.environ.get('QMT_ACCOUNT', ''),
                'ths_http_url': os.environ.get('THS_HTTP_URL', 'http://localhost:5000')
            }
        )
        action = data.get('action')
        code = data.get('code')
        price = data.get('price', 0)
        amount = data.get('amount', 0)
        if action == 'buy':
            result = executor.buy(code, price, amount)
        elif action == 'sell':
            result = executor.sell(code, price, amount)
        else:
            return {"success": False, "error": "无效操作"}
        return {
            "success": result.success,
            "data": {
                "order_id": result.order_id,
                "message": result.message,
                "price": result.price,
                "amount": result.amount
            }
        }
    except Exception as e:
        logger.error(f"交易执行失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ============ HTTP 请求处理器 ============

class RequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            params = parse_qs(parsed.query)

            if path == "/health":
                self._json_response({"status": "ok"}, 200)
            elif path == "/api/dashboard":
                self._json_response(handle_dashboard())
            elif path == "/api/daily/status":
                self._json_response(handle_task_status(params))
            elif path == "/api/reports":
                self._json_response(handle_reports())
            elif path.startswith("/api/reports/"):
                date_str = path[len("/api/reports/"):]
                self._json_response(handle_report_detail(date_str))
            elif path == "/api/smash/history":
                self._json_response(handle_smash_history(params))
            elif path == "/api/turning_points":
                self._json_response(handle_turning_points(params))
            elif path == "/api/dragon_imminent":
                self._json_response(handle_dragon_imminent(params))
            elif path == "/api/entry_certainty":
                self._json_response(handle_entry_certainty(params))
            elif path == "/api/daily/latest":
                self._json_response(handle_daily_latest(params))
            elif path == "/api/recommend/latest":
                self._json_response(handle_recommend_latest(params))
            elif path == "/api/model/health":
                self._json_response(handle_model_health())
            elif path == "/api/signals":
                self._json_response(handle_signals_data())
            elif path == "/api/signals/history":
                self._json_response(handle_signal_history(params))
            elif path == "/api/recommendations/history":
                self._json_response(handle_recommendation_history(params))
            elif path == "/api/weights/history":
                self._json_response(handle_weight_history())
            elif path == "/api/upgrade/logs":
                self._json_response(handle_upgrade_logs())
            elif path == "/api/regime/logs":
                self._json_response(handle_regime_logs())
            elif path == "/api/modules/status":
                self._json_response(handle_modules_status())
            elif path == "/api/exit-signals":
                self._json_response(handle_exit_signals(params))
            elif path == "/api/quant/signals":
                self._json_response(handle_quant_signals())
            elif path == "/" or path == "/index.html":
                self._serve_file(os.path.join(TEMPLATE_DIR, "index.html"), "text/html")
            elif path.startswith("/static/"):
                filepath = os.path.join(STATIC_DIR, path[len("/static/"):])
                self._serve_static(filepath)
            else:
                self.send_error(404)
        except Exception as e:
            logger.error(f"Unhandled GET error: {e}", exc_info=True)
            self._json_response({"success": False, "error": str(e)}, 500)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""

            if path == "/api/fetch":
                self._json_response(handle_start_fetch(body))
            elif path == "/api/daily":
                self._json_response(handle_start_daily(body))
            elif path == "/api/backtest":
                self._json_response(handle_start_backtest(body))
            elif path == "/api/recommend":
                self._json_response(handle_start_recommend(body))
            elif path == "/api/track":
                self._json_response(handle_start_track(body))
            elif path == "/api/auto-upgrade":
                self._json_response(handle_start_auto_upgrade(body))
            elif path == "/api/simulate":
                self._json_response(handle_start_simulate(body))
            elif path == "/api/quant/backtest":
                self._json_response(handle_quant_backtest(body))
            elif path == "/api/trade/order":
                self._json_response(handle_trade_order(body))
            else:
                self.send_error(404)
        except Exception as e:
            logger.error(f"Unhandled POST error: {e}", exc_info=True)
            self._json_response({"success": False, "error": str(e)}, 500)

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filepath, content_type):
        if not os.path.exists(filepath):
            self.send_error(404)
            return
        with open(filepath, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def _serve_static(self, filepath):
        if not os.path.exists(filepath) or not os.path.isfile(filepath):
            self.send_error(404)
            return
        mime, _ = mimetypes.guess_type(filepath)
        if not mime:
            mime = "application/octet-stream"
        with open(filepath, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")


# ============ 模板生成（保持原有） ============

def create_templates():
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)

    index_path = os.path.join(TEMPLATE_DIR, "index.html")
    if not os.path.exists(index_path):
        pass

    app_js_path = os.path.join(STATIC_DIR, "app.js")
    if not os.path.exists(app_js_path):
        pass

    css_path = os.path.join(STATIC_DIR, "style.css")
    if not os.path.exists(css_path):
        pass


# ============ 定时任务函数 ============
def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(60)


# ============ 定时任务：按 slot 分流推送（竞价作战卡/盘中速报/盘后专项） ============
def scheduled_fetch_and_recommend(slot="15:01"):
    """slot: 09:25=竞价作战卡 | 09:46/11:30/14:30=盘中速报 | 15:01=盘后专项"""
    logger.info(f"定时任务开始({slot}): 获取数据并生成分析")
    today = datetime.now().strftime("%Y-%m-%d")
    is_trading, msg = TradingDayChecker.is_trading_day(today)
    if not is_trading:
        logger.info(f"定时任务跳过({slot}): {msg}")
        return
    try:
        result = run_fetch(date_str=today)
        if result is None or result == 0:
            logger.warning("定时任务: 获取数据失败或数据为空")
            return
        logger.info(f"定时任务: 获取数据成功，{result}条记录")
    except Exception as e:
        logger.error(f"定时任务: 获取数据异常 {e}")
        return

    try:
        data_date, target_date = _determine_analysis_date(today)
        if not data_date:
            logger.warning("定时任务: 无法确定分析日期")
            return

        # 执行完整每日分析（各模块结果落库，保证页面与通知读取同一份数据）
        try:
            from main import run_daily
            run_daily()
        except Exception as e:
            logger.warning(f"定时任务: run_daily 完整流程失败（降级为仅通知）: {e}")

        # 按时间点分流推送：15:01盘后专项 / 09:25竞价作战卡 / 其余盘中速报
        # 龙头诞生、空仓信号、龙头迹象均已并入盘后专项报告，不再单独推送
        try:
            from notification_scheduler import (
                build_auction_briefing, build_intraday_flash,
                build_close_report, push_with_dedup,
            )
            if slot == "15:01":
                push_with_dedup(notifier, build_close_report(DB_PATH),
                                "close", slot, db_path=DB_PATH)
            elif slot == "09:25":
                push_with_dedup(notifier, build_auction_briefing(DB_PATH),
                                "auction", slot, db_path=DB_PATH)
            else:
                push_with_dedup(notifier, build_intraday_flash(slot, DB_PATH),
                                "intraday", slot, db_path=DB_PATH)
        except Exception as e:
            logger.warning(f"定时任务: 通知生成/发送失败: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"定时任务: 主流程失败 {e}", exc_info=True)


# ============ 启动服务器 ============

class ReuseHTTPServer(HTTPServer):
    allow_reuse_address = True


def main():
    print("=" * 70)
    print("[*] 市场分析系统 V6 (增强部署版)")
    print(f"[TIME] 当前服务器时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("=" * 70)

    create_templates()
    print("[OK] 模板文件已准备")

    try:
        db = Database(DB_PATH)
        db.init_new_tables()
        db.init_default_weights()
        db.close()
        print("[DB] 数据库表初始化完成")
    except Exception as e:
        print(f"[WARN] 数据库初始化失败: {e}")

    # 初始化扩展模块表（资金流/龙头/操作计划/进场确定性）
    try:
        from capital_flow_analyzer import CapitalFlowAnalyzer
        _cf = CapitalFlowAnalyzer(DB_PATH)
        _cf.init_tables()
        _cf.close()
        print("[DB] capital_flow_analysis 表已就绪")
    except Exception as e:
        print(f"[WARN] 资金流分析表初始化失败: {e}")
    try:
        from dragon_detector import DragonDetector
        DragonDetector.init_tables(DB_PATH)
        print("[DB] dragon_detections 表已就绪")
    except Exception as e:
        print(f"[WARN] 龙头识别表初始化失败: {e}")
    try:
        from operation_planner import OperationPlanner
        OperationPlanner.init_tables(DB_PATH)
        print("[DB] operation_plans 表已就绪")
    except Exception as e:
        print(f"[WARN] 操作计划表初始化失败: {e}")
    try:
        from entry_certainty_analyzer import init_tables as eca_init
        eca_init(DB_PATH)
        print("[DB] entry_certainty_analysis 表已就绪")
    except Exception as e:
        print(f"[WARN] 进场确定性表初始化失败: {e}")
    try:
        from notification_scheduler import init_tables as notif_init
        notif_init(DB_PATH)
        print("[DB] wechat_push_log 表已就绪（通知去重）")
    except Exception as e:
        print(f"[WARN] 通知去重表初始化失败: {e}")

    try:
        schedule.every().day.at("09:25").do(scheduled_fetch_and_recommend, slot="09:25")
        schedule.every().day.at("09:46").do(scheduled_fetch_and_recommend, slot="09:46")
        schedule.every().day.at("11:30").do(scheduled_fetch_and_recommend, slot="11:30")
        schedule.every().day.at("14:30").do(scheduled_fetch_and_recommend, slot="14:30")
        schedule.every().day.at("15:01").do(scheduled_fetch_and_recommend, slot="15:01")
        threading.Thread(target=run_scheduler, daemon=True).start()
        print("[SCHEDULE] 交易日定时任务已启用: 9:25竞价作战卡, 9:46/11:30/14:30盘中速报, 15:01盘后专项")
    except Exception as e:
        print(f"[WARN] 定时任务设置失败: {e}")

    try:
        db = Database(DB_PATH)
        all_dates = db.get_all_dates()
        if all_dates:
            latest_date = all_dates[-1]
            cursor = db.conn.execute("SELECT COUNT(*) as cnt FROM xgt_limit_up_detail WHERE date = ?", (latest_date,))
            cnt = cursor.fetchone()[0]
            print(f"[DATA] 数据库最新数据日期: {latest_date}, 涨停记录: {cnt}条")
        else:
            print("[DATA] 数据库暂无数据，请先获取数据")
        db.close()
    except Exception as e:
        print(f"[WARN] 检查数据状态失败: {e}")

    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 5000))
    print(f"[NET] 监听地址: {host}:{port}")
    server = ReuseHTTPServer((host, port), RequestHandler)
    print("[OK] 服务器启动成功！")
    print("[INFO] 按 Ctrl+C 停止服务器")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[STOP] 服务器已停止")
        server.server_close()


if __name__ == "__main__":
    main()