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
from datetime import datetime, timedelta

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
from cycle_model import CycleModel
from realtime_fetcher import fetch_realtime_today, save_realtime_to_db  # 新增导入


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, LOG_CONFIG["level"]),
        format=LOG_CONFIG["format"],
        datefmt=LOG_CONFIG["date_format"],
    )


def update_daily_summary(db, date_str):
    """
    计算并更新指定日期的每日汇总数据（xgt_daily_summary表）
    需要 xgt_limit_up_detail 和 xgt_break_limit_up 表中有数据
    """
    try:
        # 获取涨停数
        limit_up_rows = db.fetch_all("SELECT code FROM xgt_limit_up_detail WHERE date = ?", (date_str,))
        limit_up_count = len(limit_up_rows)

        # 获取炸板数
        break_rows = db.fetch_all("SELECT code FROM xgt_break_limit_up WHERE date = ?", (date_str,))
        break_count = len(break_rows)

        # 获取跌停数（如果有表）
        try:
            down_rows = db.fetch_all("SELECT code FROM xgt_limit_down WHERE date = ?", (date_str,))
            limit_down_count = len(down_rows)
        except:
            limit_down_count = 0

        # 计算炸板率
        total = limit_up_count + break_count
        explosion_rate = break_count / total if total > 0 else 0.0

        # 获取最高连板
        max_boards_row = db.fetch_one(
            "SELECT MAX(limit_up_days) as max_boards FROM xgt_limit_up_detail WHERE date = ?",
            (date_str,)
        )
        max_boards = max_boards_row[0] if max_boards_row else 0

        # 获取连板分布
        board_rows = db.fetch_all(
            "SELECT limit_up_days, COUNT(*) as cnt FROM xgt_limit_up_detail WHERE date = ? GROUP BY limit_up_days",
            (date_str,)
        )
        board_dist = {row[0]: row[1] for row in board_rows}

        # 获取涨跌家数（如果有数据，暂不强制）
        rise_count = 0
        fall_count = 0

        # 插入或更新汇总
        db.execute("""
            INSERT OR REPLACE INTO xgt_daily_summary
            (date, limit_up_count, limit_down_count, break_limit_up_count,
             rise_count, fall_count, explosion_rate, rise_fall_ratio,
             market_heat, max_continuous_boards, board_distribution)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date_str,
            limit_up_count,
            limit_down_count,
            break_count,
            rise_count,
            fall_count,
            explosion_rate,
            1.0,  # rise_fall_ratio 默认
            0,    # market_heat 默认
            max_boards,
            json.dumps(board_dist)
        ))
        logger.info(f"每日汇总已更新: {date_str} 涨停{limit_up_count} 炸板{break_count} 炸板率{explosion_rate:.1%}")
        return True
    except Exception as e:
        logger.error(f"更新每日汇总失败 ({date_str}): {e}", exc_info=True)
        return False


def run_daily(db=None):
    """每日完整流程"""
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

    try:
        # Step -1: 获取数据（改用 run_fetch 以确保 xgt_limit_up_detail 有数据）
        logger.info("Step -1: 检查并获取最新数据...")
        from main import run_fetch  # 避免循环导入，但 run_fetch 在本文件内，直接调用即可
        today_str = datetime.now().strftime("%Y-%m-%d")

        # 尝试获取当天数据
        fetched = run_fetch(date_str=today_str)
        if fetched == 0:
            # 若当天无数据，尝试前一天
            yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            fetched = run_fetch(date_str=yesterday_str)
        if fetched == 0:
            logger.warning("最近两天均无数据，使用数据库已有数据")
        else:
            logger.info(f"数据获取成功: {fetched} 条记录")

        # 确定分析日期（取数据库中最新日期）
        all_dates = db.get_all_dates()
        if not all_dates:
            logger.error("数据库中没有涨停数据")
            return None

        target_date = all_dates[-1]
        logger.info(f"分析日期: {target_date}")

        # ---- 新增：确保每日汇总和砸盘系数正确计算 ----
        # 更新每日汇总
        update_daily_summary(db, target_date)
        # 计算砸盘系数（覆盖之前可能存在的错误值）
        smash_calc = SmashCoefficientCalculator(db)
        smash_coef, smash_max_boards = smash_calc.calculate_daily(target_date)
        if smash_coef is not None:
            logger.info(f"砸盘系数: {smash_coef}, 最高连板: {smash_max_boards}")
        # ---------------------------------------------

        # 初始化模块
        analyzer = MarketAnalyzer(db)
        recognizer = PatternRecognizer(db)
        kb = KnowledgeBase(db)
        predictor = Predictor(db, kb)
        tracker = PredictionTracker(db)
        corrector = SelfCorrector(db, kb)
        reporter = Reporter(db, kb)
        cycle_model = CycleModel(DB_PATH)

        # Step 0.5: 周期识别
        logger.info("Step 0.5: 识别市场周期...")
        cycle_info = cycle_model.detect_phase(target_date)
        if cycle_info.get('phase'):
            logger.info(f"周期阶段: {cycle_info['phase']} (置信度{cycle_info.get('confidence', 0):.0%})")

        # Step 1: 分析市场
        logger.info("Step 1: 分析市场...")
        analysis = analyzer.analyze_date(target_date)
        if not analysis:
            logger.error(f"分析失败: {target_date}")
            return None

        snapshot = analyzer.generate_snapshot(analysis)
        if snapshot:
            db.save_daily_snapshot(target_date, snapshot)

        # Step 2: 识别模式（传入cycle_info）
        logger.info("Step 2: 识别模式...")
        patterns = recognizer.recognize_all(target_date, analysis, cycle_info)
        if snapshot:
            snapshot["cycle_phase"] = patterns.get("cycle_phase", "")
            db.save_daily_snapshot(target_date, snapshot)

        # Step 3-8: 后续流程
        knowledge_match = kb.match_current_pattern(analysis, patterns)
        predictions = predictor.predict_next_day(target_date, analysis, patterns, cycle_info)

        verifications = []
        current_idx = all_dates.index(target_date)
        if current_idx > 0:
            verifications = tracker.verify_predictions_for_date(target_date, analysis)

        corrections = []
        if verifications:
            corrections = corrector.correct(verifications, target_date)

        kb.apply_decay()
        corrector.adapt_smash_thresholds(target_date)

        report = reporter.generate_daily_report(
            target_date, analysis, patterns, predictions,
            verifications, corrections, knowledge_match)

        print("\n" + report)

        corrector.export_weights_json(os.path.join(KNOWLEDGE_DIR, "model_weights.json"))

        logger.info("每日流程完成!")
        return {
            "date": target_date,
            "analysis": analysis,
            "patterns": patterns,
            "predictions": predictions,
            "verifications": verifications,
            "corrections": corrections,
            "cycle_info": cycle_info,
        }

    except Exception as e:
        logger.error(f"每日流程异常: {e}", exc_info=True)
    finally:
        if close_db:
            db.close()


def run_backtest(db=None, start_date=None, end_date=None, max_days=None):
    """回测模式"""
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
        cycle_model = CycleModel(DB_PATH)

        results = []
        total_predictions = 0
        total_verifications = 0

        for i, date in enumerate(backtest_dates):
            logger.info(f"\n--- 回测 [{i+1}/{len(backtest_dates)}] {date} ---")

            prev_idx = all_dates.index(date) - 1
            if prev_idx < 0:
                continue

            prev_date = all_dates[prev_idx]

            # 计算砸盘系数
            smash_calc.calculate_daily(prev_date)
            smash_calc.calculate_daily(date)

            # 周期识别
            cycle_info = cycle_model.detect_phase(date)

            # 用前日数据做分析
            prev_analysis = analyzer.analyze_date(prev_date)
            if not prev_analysis:
                continue

            prev_patterns = recognizer.recognize_all(prev_date, prev_analysis, cycle_info)

            snapshot = analyzer.generate_snapshot(prev_analysis)
            if snapshot:
                snapshot["cycle_phase"] = prev_patterns.get("cycle_phase", "")
                db.save_daily_snapshot(prev_date, snapshot)

            predictions = predictor_inst.predict_next_day(prev_date, prev_analysis, prev_patterns, cycle_info)
            total_predictions += len(predictions)

            actual_analysis = analyzer.analyze_date(date)
            if not actual_analysis:
                continue

            actual_snapshot = analyzer.generate_snapshot(actual_analysis)
            if actual_snapshot:
                actual_patterns = recognizer.recognize_all(date, actual_analysis, cycle_info)
                actual_snapshot["cycle_phase"] = actual_patterns.get("cycle_phase", "")
                db.save_daily_snapshot(date, actual_snapshot)

            verifications = tracker.verify_predictions_for_date(date, actual_analysis)
            total_verifications += len(verifications)

            corrections = corrector.correct(verifications, date) if verifications else []

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
    """仅生成报告"""
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
        cycle_model = CycleModel(DB_PATH)

        cycle_info = cycle_model.detect_phase(target)
        analysis = analyzer.analyze_date(target)
        patterns = recognizer.recognize_all(target, analysis, cycle_info) if analysis else {}
        knowledge_match = kb.match_current_pattern(analysis, patterns)

        report = reporter.generate_daily_report(
            target, analysis, patterns, {}, [], [], knowledge_match)

        print(report)
        return report

    finally:
        if close_db:
            db.close()


def run_status(db=None):
    """查看系统状态"""
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


def run_fetch(date_str=None):
    """获取涨停数据（使用 realtime_fetcher 或 akshare）"""
    logger = logging.getLogger("fetch")
    db = Database(DB_PATH)
    db.init_new_tables()

    try:
        if date_str:
            logger.info(f"=== 获取历史数据 {date_str}（API模式） ===")
            fetcher = DataFetcher(db)
            target = date_str
            limit_count = fetcher.fetch_daily_limit_up(target)
            concept_count = fetcher.fetch_concept_data(target)
            logger.info(f"数据获取完成: 涨停{limit_count}条, 概念数据{concept_count}条")

            if limit_count == 0:
                prev = (datetime.strptime(target, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                limit_count = fetcher.fetch_daily_limit_up(prev)
                if limit_count > 0:
                    fetcher.fetch_concept_data(prev)
                    logger.info(f"成功获取 {prev} 数据: {limit_count}条")

            return limit_count
        else:
            logger.info("=== 获取当天实时数据（盯盘页模式） ===")
            try:
                from realtime_fetcher import fetch_realtime_today, save_realtime_to_db
                realtime_data = fetch_realtime_today()
                saved_count = save_realtime_to_db(DB_PATH, realtime_data)

                if saved_count > 0:
                    fetcher = DataFetcher(db)
                    target_date = realtime_data['date']
                    try:
                        concept_count = fetcher.fetch_concept_data(target_date)
                        logger.info(f"概念数据获取完成: {concept_count}条")
                    except Exception as e:
                        logger.warning(f"概念数据获取失败: {e}")

                logger.info(f"实时数据获取成功: {saved_count}条记录")
                return saved_count

            except ImportError as e:
                logger.warning(f"realtime_fetcher模块加载失败: {e}")
            except Exception as e:
                logger.warning(f"盯盘页数据获取失败: {e}")

            logger.info("=== 降级到API模式获取当天数据 ===")
            fetcher = DataFetcher(db)
            target = datetime.now().strftime("%Y-%m-%d")
            limit_count = fetcher.fetch_daily_limit_up(target)
            concept_count = fetcher.fetch_concept_data(target)
            logger.info(f"API模式数据获取完成: 涨停{limit_count}条, 概念数据{concept_count}条")

            if limit_count == 0:
                prev = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                limit_count = fetcher.fetch_daily_limit_up(prev)
                if limit_count > 0:
                    fetcher.fetch_concept_data(prev)
                    logger.info(f"成功获取 {prev} 数据: {limit_count}条")

            return limit_count

    finally:
        db.close()


def main():
    setup_logging()

    if len(sys.argv) < 2:
        print("用法: python main.py <command>")
        print("  daily    - 执行每日完整流程")
        print("  fetch    - 获取数据")
        print("  backtest - 回测模式")
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


if __name__ == "__main__":
    main()