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
import schedule
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta

# 确保项目路径在 sys.path
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

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

# 从环境变量读取 SCKEY，未配置则禁用
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


def send_recommend_notification(date_str, recommendations, market_state, next_day):
    if not notifier:
        return False
    title = f"📊 智能推荐 - {date_str}"
    lines = [f"## 市场状态\n"]
    lines.append(f"- 周期: {market_state.get('cycle_phase', '未知')}")
    lines.append(f"- 砸盘系数: {market_state.get('smash_coefficient', '--')}")
    lines.append(f"- 涨停数: {market_state.get('limit_up_count', 0)}")
    lines.append(f"- 最高连板: {market_state.get('max_boards', 0)}")
    lines.append(f"- 情绪: {market_state.get('sentiment', '')}")
    lines.append("")
    if next_day:
        lines.append("## 次日策略\n")
        lines.append(f"- 目标高度: {next_day.get('target_board_height', '')}")
        lines.append(f"- 关注概念: {', '.join(next_day.get('focus_concepts', []))}")
        lines.append(f"- 风控: {next_day.get('risk_control', '')}")
        lines.append(f"- 总体策略: {next_day.get('overall_strategy', '')}\n")
    if recommendations:
        lines.append("## 🎯 Top推荐\n")
        for i, r in enumerate(recommendations[:5], 1):
            lines.append(f"{i}. {r.get('name', '')}({r.get('code', '')})")
            lines.append(f"   得分: {r.get('total_score', 0)} | 置信度: {r.get('confidence_name', '')}")
            lines.append(f"   建议: {r.get('suggested_action', '')}")
            lines.append(f"   理由: {r.get('reason', '')}")
            lines.append("")
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
            cur = conn.execute("SELECT COUNT(*) FROM akshare_limit_up WHERE date = ?", (today,))
            if cur.fetchone()[0] > 0:
                data_date = today
            else:
                cur = conn.execute("SELECT MAX(date) FROM akshare_limit_up")
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
                try:
                    send_recommend_notification(data_date, rec_serialized, market_state, next_day)
                except:
                    pass
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
                    "sentiment": market_state.get("sentiment", ""),
                    "cap_preference": market_state.get("cap_preference", ""),
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
        try:
            send_recommend_notification(data_date, rec_serialized, market_state, next_day)
        except:
            pass
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
    try:
        db = Database(DB_PATH)
        db.init_new_tables()
        snapshots = db.get_daily_snapshots(limit=1)
        latest = dict(snapshots[0]) if snapshots else {}
        smash_history = db.get_smash_coefficient_history(limit=10)
        smash_data = [dict(s) for s in smash_history]
        latest_smash = smash_data[0] if smash_data else {}
        from market_analyzer import MarketAnalyzer
        analyzer = MarketAnalyzer(db)
        all_dates = db.get_all_dates()
        latest_date = all_dates[-1] if all_dates else None
        analysis_summary = {}
        predictions = []
        if latest_date:
            analysis = analyzer.analyze_date(latest_date)
            if analysis:
                smash_info = analysis.get("smash_analysis", {})
                basic = analysis.get("basic_stats", {})
                analysis_summary = {
                    "date": latest_date,
                    "limit_up_count": basic.get("total_count", latest.get("limit_up_count", 0)),
                    "max_continuous_boards": basic.get("max_boards", latest.get("max_continuous_boards", 0)),
                    "smash_coefficient": smash_info.get("smash_coefficient", latest_smash.get("smash_coefficient")),
                    "sentiment_score": latest.get("sentiment_score", 0),
                    "avg_seal_amount": basic.get("total_seal_amount", latest.get("avg_seal_amount", 0)),
                    "cycle_phase": latest.get("cycle_phase", ""),
                    "main_concept": latest.get("main_concept", ""),
                    "smash_signal": smash_info.get("signal", ""),
                    "smash_trade_advice": smash_info.get("trade_advice", ""),
                    "smash_trend": smash_info.get("trend", ""),
                    "smash_advantage": smash_info.get("advantage", ""),
                    "board_tiers": analysis.get("board_tiers", {}),
                    "seal_quality": analysis.get("seal_quality", {}),
                    "concept_heat": analysis.get("concept_heat", {}),
                }
                from predictor import Predictor
                from knowledge_base import KnowledgeBase
                kb = KnowledgeBase(db)
                predictor = Predictor(db, kb)
                preds = predictor.predict_next_day(latest_date, analysis, {})
                predictions = _serialize_predictions(preds)
        smash_chart = []
        for s in reversed(smash_data):
            smash_chart.append({"date": s["date"], "value": s["smash_coefficient"], "max_boards": s["max_continuous_boards"]})
        db.close()
        return {"success": True, "data": {"summary": analysis_summary, "smash_chart": smash_chart, "predictions": predictions}}
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


def scheduled_fetch_and_recommend():
    today = datetime.now().strftime("%Y-%m-%d")
    is_trading, msg = TradingDayChecker.is_trading_day(today)
    if not is_trading:
        logger.info(f"定时任务跳过: {msg}")
        return

    logger.info("定时任务开始执行: 获取数据并生成推荐")
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
        if _smart_recommender is None:
            logger.warning("定时任务: 智能推荐模块未加载")
            return
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
        send_recommend_notification(data_date, rec_serialized, market_state, next_day)
        logger.info("定时任务: 微信通知发送完成")
    except Exception as e:
        logger.error(f"定时任务: 生成推荐或发送通知失败 {e}")


# ============ 启动服务器 ============

class ReuseHTTPServer(HTTPServer):
    allow_reuse_address = True


def main():
    print("=" * 70)
    print("[*] 市场分析系统 V6 (增强部署版)")
    print("=" * 70)

    create_templates()
    print("[OK] 模板文件已准备")

    try:
        schedule.every().day.at("15:00").do(scheduled_fetch_and_recommend)
        threading.Thread(target=run_scheduler, daemon=True).start()
        print("[SCHEDULE] 每日15:00自动获取数据并推荐已启用")
    except Exception as e:
        print(f"[WARN] 定时任务设置失败: {e}")

    try:
        db = Database(DB_PATH)
        all_dates = db.get_all_dates()
        if all_dates:
            latest_date = all_dates[-1]
            cursor = db.conn.execute("SELECT COUNT(*) as cnt FROM akshare_limit_up WHERE date = ?", (latest_date,))
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