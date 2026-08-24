"""
main.py - 主入口
编排所有模块，支持以下模式：
- python main.py daily      : 执行每日完整流程
- python main.py backtest   : 回测模式
- python main.py report     : 仅生成报告
- python main.py status     : 查看系统状态
"""
import sys
import os
import logging
import json
from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DB_PATH, KNOWLEDGE_DIR, LOG_CONFIG
from db import Database
from data_fetcher import DataFetcher
from market_analyzer import MarketAnalyzer
from pattern_recognizer import PatternRecognizer
from predictor import Predictor
from prediction_tracker import PredictionTracker
from self_corrector import SelfCorrector
from knowledge_base import KnowledgeBase
from reporter import Reporter
from smash_coefficient import SmashCoefficientCalculator
from dragon_detector import DragonDetector
from operation_planner import OperationPlanner
from capital_flow_analyzer import CapitalFlowAnalyzer
from smart_recommender import generate_recommendations as smart_generate_recs

def setup_logging():
    logging.basicConfig(
        level=getattr(logging, LOG_CONFIG["level"]),
        format=LOG_CONFIG["format"],
        datefmt=LOG_CONFIG["date_format"],
    )

def run_daily(db=None):
    logger = logging.getLogger("daily")
    logger.info("=" * 60)
    logger.info("开始每日流程")
    logger.info("=" * 60)
    
    close_db = False
    if db is None:
        db = Database(DB_PATH)
        db.init_new_tables()
        db.init_default_weights()
        close_db = True
    
    # 初始化报告变量，防止异常时未定义
    cf_report = ""
    dragon_report = ""
    plan_report = ""
    
    try:
        # 先尝试获取今天的数据（如果数据库中还没有）
        logger.info("Step -1: 检查并获取最新数据...")
        fetcher = DataFetcher(db)
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        all_dates = db.get_all_dates()
        latest_existing = all_dates[-1] if all_dates else None
        
        if not all_dates or latest_existing < today_str:
            logger.info(f"尝试获取 {today_str} 的数据...")
            # 获取前先清空当日数据，避免残留
            clear_day_data(today_str)
            fetched_count = fetcher.fetch_daily_limit_up(today_str)
            if fetched_count > 0:
                logger.info(f"成功获取 {today_str} 涨停数据: {fetched_count} 条")
            else:
                logger.info(f"今天 {today_str} 无数据（可能非交易日），尝试前一天...")
                from datetime import timedelta
                yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                clear_day_data(yesterday_str)
                fetched_count = fetcher.fetch_daily_limit_up(yesterday_str)
                if fetched_count > 0:
                    logger.info(f"成功获取 {yesterday_str} 涨停数据: {fetched_count} 条")
                else:
                    logger.warning(f"最近两天均无数据，使用数据库已有数据")
        
        all_dates = db.get_all_dates()
        if not all_dates:
            logger.error("数据库中没有涨停数据，请先运行: python main.py fetch")
            return
        
        target_date = all_dates[-1]
        logger.info(f"分析日期: {target_date}")
        
        analyzer = MarketAnalyzer(db)
        recognizer = PatternRecognizer(db)
        kb = KnowledgeBase(db)
        predictor = Predictor(db, kb)
        tracker = PredictionTracker(db)
        corrector = SelfCorrector(db, kb)
        reporter = Reporter(db, kb)
        smash_calc = SmashCoefficientCalculator(db)
        
        # 0. 计算砸盘系数
        logger.info("Step 0: 计算砸盘系数...")
        smash_coef, smash_max_boards = smash_calc.calculate_daily(target_date)
        if smash_coef is not None:
            logger.info(f"砸盘系数: {smash_coef}, 最高连板: {smash_max_boards}")
        else:
            logger.warning("砸盘系数计算失败（可能缺少前日数据）")
        
        # 1. 分析市场
        logger.info("Step 1: 分析市场...")
        analysis = analyzer.analyze_date(target_date)
        if not analysis:
            logger.error(f"分析失败: {target_date}")
            return
        
        snapshot = analyzer.generate_snapshot(analysis)
        if snapshot:
            db.save_daily_snapshot(target_date, snapshot)
        
        # 2. 识别模式
        logger.info("Step 2: 识别模式...")
        patterns = recognizer.recognize_all(target_date, analysis)
        if snapshot:
            snapshot["cycle_phase"] = patterns.get("cycle_phase", "")
            db.save_daily_snapshot(target_date, snapshot)
        
        # 3. 知识库匹配
        logger.info("Step 3: 知识库匹配...")
        knowledge_match = kb.match_current_pattern(analysis, patterns)
        
        # 4. 生成预测
        logger.info("Step 4: 生成预测...")
        predictions = predictor.predict_next_day(target_date, analysis, patterns)
        
        # 5. 验证前一天的预测
        logger.info("Step 5: 验证前一天的预测...")
        verifications = []
        current_idx = all_dates.index(target_date)
        if current_idx > 0:
            verifications = tracker.verify_predictions_for_date(target_date, analysis)
        
        # 6. 自我修正
        logger.info("Step 6: 自我修正...")
        corrections = []
        if verifications:
            corrections = corrector.correct(verifications, target_date)
        
        # 7. 知识衰减
        logger.info("Step 7: 知识衰减检查...")
        kb.apply_decay()
        
        # 7.5 砸盘系数自适应调整
        logger.info("Step 7.5: 砸盘系数自适应调整...")
        smash_adaptation = corrector.adapt_smash_thresholds(target_date)

        # 7.7 资金进攻/持续/轮动分析
        logger.info("Step 7.7: 资金流分析（进攻/持续/轮动）...")
        capital_flow_result = None
        try:
            cf_analyzer = CapitalFlowAnalyzer(DB_PATH)
            capital_flow_result = cf_analyzer.analyze(target_date, save=True)
            cf_report = cf_analyzer.format_report(capital_flow_result)
            print("\n" + cf_report)
            logger.info(f"资金流综合评分: {capital_flow_result['composite_score']}, "
                       f"仓位系数: {capital_flow_result['position_multiplier']}")
            cf_analyzer.close()
        except Exception as e:
            logger.error(f"资金流分析异常: {e}", exc_info=True)

        # 7.8 确定性龙头识别 + 操作计划（传入资金流仓位系数）
        logger.info("Step 7.8: 确定性龙头识别与操作计划...")
        try:
            DragonDetector.init_tables(DB_PATH)
            OperationPlanner.init_tables(DB_PATH)
            dragon_detector = DragonDetector(DB_PATH)
            op_planner = OperationPlanner(DB_PATH)
            dragons = dragon_detector.detect_dragons(target_date, save=True)
            # 将资金流仓位系数传入操作计划
            cf_pos_mult = capital_flow_result['position_multiplier'] if capital_flow_result else 1.0
            op_plans = op_planner.generate_plans(
                target_date, top_n=5, save=True,
                capital_flow_multiplier=cf_pos_mult)
            dragon_report = dragon_detector.format_dragon_report(dragons, target_date)
            plan_report = op_planner.format_plans(op_plans, target_date)
            # 输出龙头和操作计划
            print("\n" + dragon_report)
            print("\n" + plan_report)
            logger.info(f"龙头识别: {len(dragons)}只候选, "
                       f"操作计划: {sum(1 for p in op_plans if p.get('action')=='operate')}只可操作")
        except Exception as e:
            logger.error(f"龙头识别/操作计划异常: {e}", exc_info=True)

        # 7.85 进场确定性深度分析（题材强弱/卡位/换手/竞价/次日推演）
        logger.info("Step 7.85: 进场确定性深度分析...")
        try:
            from entry_certainty_analyzer import (
                EntryCertaintyAnalyzer, init_tables as eca_init,
                save_analysis as eca_save, analyze_date as eca_analyze
            )
            eca_init(DB_PATH)
            eca_date, eca_results = eca_analyze(target_date, top_n=20)
            if eca_results:
                print("\n" + "=" * 60)
                print(f"🎯 进场确定性分析（{eca_date}）")
                print("=" * 60)
                for r in eca_results[:8]:
                    c = r['composite']
                    op = r['operation']
                    bp = r['dimensions']['next_day_certainty']['details']['bayes_probability']
                    pos_tag = f" 仓位{op['position_pct']:.0%}" if op['position_pct'] > 0 else ""
                    print(f"  [{c['certainty_grade']:2s}] {c['score']:5.1f}分 "
                          f"{r['name']:8s} {r['boards']}板 {r.get('concept','')[:6]:6s} | "
                          f"次日{bp:.0%} | {op['action_name']}{pos_tag}")
                logger.info(f"进场确定性分析: {len(eca_results)}只, "
                           f"S/S+级{sum(1 for r in eca_results if r['composite']['certainty_grade'] in ('S+','S'))}只")
            else:
                logger.info("进场确定性分析: 无符合条件的标的")
        except Exception as e:
            logger.error(f"进场确定性分析异常: {e}", exc_info=True)

        # 7.9 智能推荐（融合龙头识别+资金流分析结果）
        logger.info("Step 7.9: 智能推荐（融合龙头+资金流）...")
        try:
            smart_recs = smart_generate_recs(target_date, top_n=5)
            if smart_recs:
                print("\n" + "=" * 60)
                print("📊 确定性推荐（融合龙头识别+资金流分析）")
                print("=" * 60)
                for r in smart_recs:
                    dinfo = r.get('dragon_info')
                    dragon_tag = ''
                    if dinfo:
                        dragon_tag = f" 🏆{dinfo['certainty_level']}级{dinfo.get('dragon_type','')}"
                    print(f"  {r['name']}({r['code']}) | 评分{r['total_score']:.1f} | "
                          f"胜率{r['win_rate']:.0%} | {r['confidence_level']}级 | "
                          f"{r['suggested_action']}{dragon_tag}")
                logger.info(f"智能推荐: {len(smart_recs)}只")
            else:
                logger.info("智能推荐: 无符合确定性门槛的标的，宁缺毋滥")
        except Exception as e:
            logger.error(f"智能推荐异常: {e}", exc_info=True)

        # 8. 生成报告
        logger.info("Step 8: 生成报告...")
        report = reporter.generate_daily_report(
            target_date, analysis, patterns, predictions,
            verifications, corrections, knowledge_match)
        
        print("\n" + report)
        corrector.export_weights_json(
            os.path.join(KNOWLEDGE_DIR, "model_weights.json"))
        
        logger.info("每日流程完成!")
        return {
            "date": target_date,
            "analysis": analysis,
            "patterns": patterns,
            "predictions": predictions,
            "verifications": verifications,
            "corrections": corrections,
            "capital_flow_report": cf_report,
            "dragon_report": dragon_report,
            "plan_report": plan_report,
        }
        
    except Exception as e:
        logger.error(f"每日流程异常: {e}", exc_info=True)
    finally:
        if close_db:
            db.close()

def run_backtest(db=None, start_date=None, end_date=None, max_days=None):
    logger = logging.getLogger("backtest")
    logger.info("=" * 60)
    logger.info("开始回测模式")
    logger.info("=" * 60)
    
    close_db = False
    if db is None:
        db = Database(DB_PATH)
        db.init_new_tables()
        db.init_default_weights()
        close_db = True
    
    try:
        all_dates = db.get_all_dates()
        if not all_dates:
            logger.error("无数据可回测")
            return
        
        if start_date and start_date in all_dates:
            start_idx = all_dates.index(start_date)
        else:
            start_idx = 10
        
        if end_date and end_date in all_dates:
            end_idx = all_dates.index(end_date)
        else:
            end_idx = len(all_dates) - 1
        
        if max_days:
            backtest_length = min(max_days, end_idx - start_idx + 1)
            start_idx = max(start_idx, end_idx - backtest_length + 1)
        
        backtest_dates = all_dates[start_idx:end_idx + 1]
        logger.info(f"回测区间: {backtest_dates[0]} ~ {backtest_dates[-1]}, 共{len(backtest_dates)}天")
        
        analyzer = MarketAnalyzer(db)
        recognizer = PatternRecognizer(db)
        kb = KnowledgeBase(db)
        predictor_inst = Predictor(db, kb)
        tracker = PredictionTracker(db)
        corrector = SelfCorrector(db, kb)
        reporter = Reporter(db, kb)
        smash_calc = SmashCoefficientCalculator(db)
        
        results = []
        total_predictions = 0
        total_verifications = 0
        
        for i, date in enumerate(backtest_dates):
            logger.info(f"\n--- 回测 [{i+1}/{len(backtest_dates)}] {date} ---")
            prev_idx = all_dates.index(date) - 1
            if prev_idx < 0:
                continue
            prev_date = all_dates[prev_idx]
            
            smash_calc.calculate_daily(prev_date)
            smash_calc.calculate_daily(date)
            
            prev_analysis = analyzer.analyze_date(prev_date)
            if not prev_analysis:
                continue
            prev_patterns = recognizer.recognize_all(prev_date, prev_analysis)
            snapshot = analyzer.generate_snapshot(prev_analysis)
            if snapshot:
                snapshot["cycle_phase"] = prev_patterns.get("cycle_phase", "")
                db.save_daily_snapshot(prev_date, snapshot)
            
            predictions = predictor_inst.predict_next_day(prev_date, prev_analysis, prev_patterns)
            total_predictions += len(predictions)
            
            actual_analysis = analyzer.analyze_date(date)
            if not actual_analysis:
                continue
            actual_snapshot = analyzer.generate_snapshot(actual_analysis)
            if actual_snapshot:
                actual_patterns = recognizer.recognize_all(date, actual_analysis)
                actual_snapshot["cycle_phase"] = actual_patterns.get("cycle_phase", "")
                db.save_daily_snapshot(date, actual_snapshot)
            
            verifications = tracker.verify_predictions_for_date(date, actual_analysis)
            total_verifications += len(verifications)
            if verifications:
                corrections = corrector.correct(verifications, date)
            else:
                corrections = []
            if (i + 1) % 5 == 0:
                kb.apply_decay()
            
            results.append({
                "date": date,
                "predictions": len(predictions),
                "verifications": verifications,
                "corrections": corrections,
            })
            
            if verifications:
                avg_score = sum(v.get("score", 0) for v in verifications) / len(verifications)
                logger.info(f"验证: {len(verifications)}条, 平均得分: {avg_score:.3f}")
            if corrections:
                logger.info(f"修正: {len(corrections)}项")
        
        date_range = f"{backtest_dates[0]} ~ {backtest_dates[-1]}"
        backtest_report = reporter.generate_backtest_report(results, date_range)
        report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", "backtest_report.md")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(backtest_report)
        
        print("\n" + backtest_report)
        corrector.export_weights_json(os.path.join(KNOWLEDGE_DIR, "model_weights.json"))
        kb.export_all()
        
        logger.info(f"\n回测完成: 共{len(backtest_dates)}天, {total_predictions}次预测, {total_verifications}次验证")
        return {
            "results": results,
            "total_days": len(backtest_dates),
            "total_predictions": total_predictions,
            "total_verifications": total_verifications,
            "report": backtest_report,
        }
        
    except Exception as e:
        logger.error(f"回测异常: {e}", exc_info=True)
    finally:
        if close_db:
            db.close()

def run_report(db=None, date_str=None):
    logger = logging.getLogger("report")
    close_db = False
    if db is None:
        db = Database(DB_PATH)
        close_db = True
    try:
        all_dates = db.get_all_dates()
        target = date_str or (all_dates[-1] if all_dates else None)
        if not target:
            print("无数据可生成报告")
            return
        analyzer = MarketAnalyzer(db)
        recognizer = PatternRecognizer(db)
        kb = KnowledgeBase(db)
        reporter = Reporter(db, kb)
        analysis = analyzer.analyze_date(target)
        patterns = recognizer.recognize_all(target, analysis) if analysis else {}
        knowledge_match = kb.match_current_pattern(analysis, patterns)
        report = reporter.generate_daily_report(
            target, analysis, patterns, {}, [], [], knowledge_match)
        print(report)
        return report
    finally:
        if close_db:
            db.close()

def run_status(db=None):
    close_db = False
    if db is None:
        db = Database(DB_PATH)
        close_db = True
    try:
        kb = KnowledgeBase(db)
        reporter = Reporter(db, kb)
        corrector = SelfCorrector(db, kb)
        status = reporter.generate_status_report()
        print(status)
        health = corrector.get_model_health()
        print(f"\n模型状态: {health.get('status', '未知')}")
        print(f"平均可信度: {health.get('avg_credibility', 0):.3f}")
        return status
    finally:
        if close_db:
            db.close()

def clear_day_data(date_str: str) -> int:
    """
    在重新获取某日数据前，清空该日所有相关表的数据，避免脏数据/重复/残留。
    覆盖：
      1) 原始行情：xgt_limit_up_detail / xgt_break_limit_up / xgt_limit_down / xgt_daily_summary / limit_up_stocks
      2) 分析衍生：daily_snapshot / daily_analysis / daily_summary / smash_coefficients / smash_coefficient_results
         / market_sentiment / sector_flow / concept_analysis / concept_statistics / stock_concept_reason
      3) 龙头/操作/资金流：dragon_detections / dragon_lifecycle / dragon_cycle_context / operation_plans
         / capital_flow_analysis / capital_flow_concept_tracking / cycle_context
      4) 推荐/预测/信号：recommendation_log / prediction_records / signal_tracking / regime_detection_log
         / daily_tracking_report / strategy_tracking
    不清理：配置类表（model_weights、signal_weights、trading_calendar、data_fetch_config 等）、
           回测/历史归档表（historical_limit_up、backtest_records、upgrade_log、correction_log、
           notification_log、xgt_*_cache）。
    返回删除行数合计。
    """
    logger = logging.getLogger("clear_day")
    if not date_str:
        return 0

    # 表 → 日期字段
    tables_to_clear = [
        # 原始行情
        ("xgt_limit_up_detail", "date"),
        ("xgt_break_limit_up", "date"),
        ("xgt_limit_down", "date"),
        ("xgt_daily_summary", "date"),
        ("limit_up_stocks", "trade_date"),
        # 每日分析衍生
        ("daily_snapshot", "date"),
        ("daily_analysis", "date"),
        ("daily_summary", "date"),
        ("smash_coefficients", "trade_date"),
        ("smash_coefficient_results", "date"),
        ("market_sentiment", "date"),
        ("sector_flow", "date"),
        ("concept_analysis", "date"),
        ("concept_statistics", "date"),
        ("stock_concept_reason", "date"),
        # 龙头/操作/资金流
        ("dragon_detections", "detect_date"),
        ("dragon_lifecycle", "first_seen_date"),
        ("dragon_cycle_context", "date"),
        ("operation_plans", "plan_date"),
        ("entry_certainty_analysis", "date"),
        ("capital_flow_analysis", "date"),
        ("capital_flow_concept_tracking", "date"),
        ("cycle_context", "date"),
        # 推荐/预测/信号
        ("recommendation_log", "rec_date"),
        ("prediction_records", "date"),
        ("signal_tracking", "trigger_date"),
        ("regime_detection_log", "detect_date"),
        ("daily_tracking_report", "report_date"),
        ("strategy_tracking", "date"),
    ]

    conn = None
    total_deleted = 0
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        for table, col in tables_to_clear:
            try:
                # 检查表是否存在
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)
                ).fetchone()
                if not exists:
                    continue
                # 检查字段是否存在
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                if col not in cols:
                    logger.debug(f"跳过 {table}：缺少字段 {col}")
                    continue
                cur = conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (date_str,))
                deleted = cur.rowcount
                total_deleted += deleted if deleted and deleted > 0 else 0
                if deleted:
                    logger.info(f"清空 {table}.{col}={date_str}: {deleted} 行")
            except Exception as e:
                logger.warning(f"清空 {table} 失败: {e}")
        conn.commit()
        logger.info(f"clear_day_data({date_str}) 完成，合计删除 {total_deleted} 行")
    except Exception as e:
        logger.error(f"clear_day_data 异常: {e}")
        if conn:
            try: conn.rollback()
            except Exception: pass
    finally:
        if conn:
            conn.close()
    return total_deleted


def run_fetch(date_str=None):
    logger = logging.getLogger("fetch")
    db = Database(DB_PATH)
    db.init_new_tables()
    try:
        if date_str:
            logger.info(f"=== 获取历史数据 {date_str}（API模式） ===")
            # 获取前先清空当日数据，避免残留/重复
            clear_day_data(date_str)
            fetcher = DataFetcher(db)
            target = date_str
            limit_count = fetcher.fetch_daily_limit_up(target)
            logger.info(f"数据获取完成: 涨停{limit_count}条")
            if limit_count == 0:
                logger.warning(f"{target} 可能非交易日，尝试获取前一个交易日...")
                from datetime import timedelta
                prev = (datetime.strptime(target, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                # 前一天是新目标，同样先清空
                clear_day_data(prev)
                limit_count = fetcher.fetch_daily_limit_up(prev)
                logger.info(f"成功获取 {prev} 数据: {limit_count}条")
            return limit_count
        else:
            logger.info("=== 获取当天实时数据（盯盘页模式） ===")
            today_str = datetime.now().strftime("%Y-%m-%d")
            # 获取前先清空当日数据，避免残留/重复
            clear_day_data(today_str)
            try:
                from realtime_fetcher import fetch_realtime_today, save_realtime_to_db
                realtime_data = fetch_realtime_today()
                saved_count = save_realtime_to_db(DB_PATH, realtime_data)
                logger.info(f"实时数据获取成功: {saved_count}条记录")
                return saved_count
            except ImportError as e:
                logger.warning(f"realtime_fetcher模块加载失败，降级到API模式: {e}")
            except Exception as e:
                logger.warning(f"盯盘页数据获取失败，降级到API模式: {e}")
            
            logger.info("=== 降级到API模式获取当天数据 ===")
            fetcher = DataFetcher(db)
            target = today_str
            limit_count = fetcher.fetch_daily_limit_up(target)
            logger.info(f"API模式数据获取完成: 涨停{limit_count}条")
            if limit_count == 0:
                logger.warning(f"{target} 可能非交易日，尝试获取前一个交易日...")
                from datetime import timedelta
                prev = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                clear_day_data(prev)
                limit_count = fetcher.fetch_daily_limit_up(prev)
                logger.info(f"成功获取 {prev} 数据: {limit_count}条")
            return limit_count
    finally:
        db.close()

def main():
    setup_logging()
    if len(sys.argv) < 2:
        print("用法: python main.py <command>")
        print("  daily    - 执行每日完整流程（获取→分析→预测→验证→修正）")
        print("  backtest - 回测模式（用历史数据验证系统）")
        print("  report   - 仅生成报告")
        print("  status   - 查看系统状态和模型健康度")
        return
    command = sys.argv[1].lower()
    if command == "daily":
        run_daily()
    elif command == "fetch":
        run_fetch()
    elif command == "backtest":
        max_days = None
        start_date = None
        end_date = None
        for i, arg in enumerate(sys.argv[2:], 2):
            if arg.startswith("--max="):
                max_days = int(arg.split("=")[1])
            elif arg.startswith("--start="):
                start_date = arg.split("=")[1]
            elif arg.startswith("--end="):
                end_date = arg.split("=")[1]
        run_backtest(start_date=start_date, end_date=end_date, max_days=max_days)
    elif command == "report":
        date_str = sys.argv[2] if len(sys.argv) > 2 else None
        run_report(date_str=date_str)
    elif command == "status":
        run_status()
    else:
        print(f"未知命令: {command}")
        print("支持的命令: daily, backtest, report, status")

if __name__ == "__main__":
    main()