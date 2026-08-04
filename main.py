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


def setup_logging():
    """配置日志"""
    logging.basicConfig(
        level=getattr(logging, LOG_CONFIG["level"]),
        format=LOG_CONFIG["format"],
        datefmt=LOG_CONFIG["date_format"],
    )


def run_daily(db=None):
    """
    每日完整流程：
    1. 获取数据（如果是新数据）
    2. 分析市场
    3. 识别模式
    4. 生成预测
    5. 验证前一天的预测
    6. 自我修正
    7. 生成报告
    """
    logger = logging.getLogger("daily")
    logger.info("=" * 60)
    logger.info("开始每日流程")
    logger.info("=" * 60)
    
    # 初始化组件
    close_db = False
    if db is None:
        db = Database(DB_PATH)
        db.init_new_tables()
        db.init_default_weights()
        close_db = True
    
    try:
        # 先尝试获取今天的数据（如果数据库中还没有）
        logger.info("Step -1: 检查并获取最新数据...")
        fetcher = DataFetcher(db)
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        all_dates = db.get_all_dates()
        latest_existing = all_dates[-1] if all_dates else None
        
        # 如果数据库为空，或者最新数据不是今天，尝试获取今天的数据
        if not all_dates or latest_existing < today_str:
            logger.info(f"尝试获取 {today_str} 的数据...")
            fetched_count = fetcher.fetch_daily_limit_up(today_str)
            
            if fetched_count > 0:
                logger.info(f"成功获取 {today_str} 涨停数据: {fetched_count} 条")
                # 同时获取选股宝概念数据
                fetcher.fetch_concept_data(today_str)
            else:
                logger.info(f"今天 {today_str} 无数据（可能非交易日），尝试前一天...")
                from datetime import timedelta
                yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                fetched_count = fetcher.fetch_daily_limit_up(yesterday_str)
                if fetched_count > 0:
                    logger.info(f"成功获取 {yesterday_str} 涨停数据: {fetched_count} 条")
                    fetcher.fetch_concept_data(yesterday_str)
                else:
                    logger.warning(f"最近两天均无数据，使用数据库已有数据")
        
        # 重新获取日期列表
        all_dates = db.get_all_dates()
        if not all_dates:
            logger.error("数据库中没有涨停数据，请先运行: python main.py fetch")
            return
        
        # 确定要分析的日期（最新有数据的日期）
        target_date = all_dates[-1]
        logger.info(f"分析日期: {target_date}")
        
        # 初始化各模块
        analyzer = MarketAnalyzer(db)
        recognizer = PatternRecognizer(db)
        kb = KnowledgeBase(db)
        predictor = Predictor(db, kb)
        tracker = PredictionTracker(db)
        corrector = SelfCorrector(db, kb)
        reporter = Reporter(db, kb)
        smash_calc = SmashCoefficientCalculator(db)
        
        # 0. 计算砸盘系数（核心主导因素，优先计算）
        logger.info("Step 0: 计算砸盘系数...")
        smash_coef, smash_max_boards = smash_calc.calculate_daily(target_date)
        if smash_coef is not None:
            logger.info(f"砸盘系数: {smash_coef}, 最高连板: {smash_max_boards}")
        else:
            logger.warning("砸盘系数计算失败（可能缺少前日数据）")
        
        # 1. 分析市场（包含砸盘系数分析）
        logger.info("Step 1: 分析市场...")
        analysis = analyzer.analyze_date(target_date)
        if not analysis:
            logger.error(f"分析失败: {target_date}")
            return
        
        # 保存快照
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
        
        # 4. 生成预测（预测次日）
        logger.info("Step 4: 生成预测...")
        predictions = predictor.predict_next_day(target_date, analysis, patterns)
        
        # 5. 验证前一天的预测
        logger.info("Step 5: 验证前一天的预测...")
        verifications = []
        current_idx = all_dates.index(target_date)
        if current_idx > 0:
            prev_date = all_dates[current_idx - 1]
            verifications = tracker.verify_predictions_for_date(target_date, analysis)
            # 注意：验证的是"在prev_date对target_date的预测"
            # 但这里简化为：用target_date的实际数据验证之前对它的预测
        
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
        
        # 8. 生成报告
        logger.info("Step 8: 生成报告...")
        report = reporter.generate_daily_report(
            target_date, analysis, patterns, predictions,
            verifications, corrections, knowledge_match)
        
        # 输出报告
        print("\n" + report)
        
        # 导出权重JSON
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
        }
        
    except Exception as e:
        logger.error(f"每日流程异常: {e}", exc_info=True)
    finally:
        if close_db:
            db.close()


def run_backtest(db=None, start_date=None, end_date=None, max_days=None):
    """
    回测模式：用历史数据逐步验证系统
    从第N天开始，每天用前N-1天数据生成预测，然后用第N天数据验证
    """
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
        
        # 确定回测范围
        if start_date and start_date in all_dates:
            start_idx = all_dates.index(start_date)
        else:
            start_idx = 10  # 最少需要前10天数据作为分析基础
        
        if end_date and end_date in all_dates:
            end_idx = all_dates.index(end_date)
        else:
            end_idx = len(all_dates) - 1  # 默认到最新日期
        
        # max_days限制：从end_idx往前推max_days天，确保回测的是最近的数据
        if max_days:
            # 回测区间长度不超过max_days，从end_idx往前推
            backtest_length = min(max_days, end_idx - start_idx + 1)
            start_idx = max(start_idx, end_idx - backtest_length + 1)
        
        backtest_dates = all_dates[start_idx:end_idx + 1]
        logger.info(f"回测区间: {backtest_dates[0]} ~ {backtest_dates[-1]}, 共{len(backtest_dates)}天")
        
        # 初始化模块
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
            
            # 获取前一天的分析数据
            prev_idx = all_dates.index(date) - 1
            if prev_idx < 0:
                continue
            
            prev_date = all_dates[prev_idx]
            
            # 计算砸盘系数（前日和当日）
            smash_calc.calculate_daily(prev_date)
            smash_calc.calculate_daily(date)
            
            # 用前日数据做分析并生成预测
            prev_analysis = analyzer.analyze_date(prev_date)
            if not prev_analysis:
                continue
            
            prev_patterns = recognizer.recognize_all(prev_date, prev_analysis)
            
            # 保存前日快照
            snapshot = analyzer.generate_snapshot(prev_analysis)
            if snapshot:
                snapshot["cycle_phase"] = prev_patterns.get("cycle_phase", "")
                db.save_daily_snapshot(prev_date, snapshot)
            
            # 生成对今日的预测
            predictions = predictor_inst.predict_next_day(prev_date, prev_analysis, prev_patterns)
            total_predictions += len(predictions)
            
            # 用今日实际数据验证
            actual_analysis = analyzer.analyze_date(date)
            if not actual_analysis:
                continue
            
            # 保存今日快照
            actual_snapshot = analyzer.generate_snapshot(actual_analysis)
            if actual_snapshot:
                actual_patterns = recognizer.recognize_all(date, actual_analysis)
                actual_snapshot["cycle_phase"] = actual_patterns.get("cycle_phase", "")
                db.save_daily_snapshot(date, actual_snapshot)
            
            verifications = tracker.verify_predictions_for_date(date, actual_analysis)
            total_verifications += len(verifications)
            
            # 自我修正
            if verifications:
                corrections = corrector.correct(verifications, date)
            else:
                corrections = []
            
            # 知识衰减
            if (i + 1) % 5 == 0:  # 每5天执行一次知识衰减
                kb.apply_decay()
            
            results.append({
                "date": date,
                "predictions": len(predictions),
                "verifications": verifications,
                "corrections": corrections,
            })
            
            # 打印简要结果
            if verifications:
                avg_score = sum(v.get("score", 0) for v in verifications) / len(verifications)
                logger.info(f"验证: {len(verifications)}条, 平均得分: {avg_score:.3f}")
            
            if corrections:
                logger.info(f"修正: {len(corrections)}项")
        
        # 生成回测报告
        date_range = f"{backtest_dates[0]} ~ {backtest_dates[-1]}"
        backtest_report = reporter.generate_backtest_report(results, date_range)
        
        # 保存回测报告
        report_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "reports", "backtest_report.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(backtest_report)
        
        print("\n" + backtest_report)
        
        # 导出最终权重
        corrector.export_weights_json(
            os.path.join(KNOWLEDGE_DIR, "model_weights.json"))
        
        # 导出知识库
        kb.export_all()
        
        logger.info(f"\n回测完成: 共{len(backtest_dates)}天, "
                   f"{total_predictions}次预测, {total_verifications}次验证")
        
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
        
        # 额外输出模型健康度
        health = corrector.get_model_health()
        print(f"\n模型状态: {health.get('status', '未知')}")
        print(f"平均可信度: {health.get('avg_credibility', 0):.3f}")
        
        return status
        
    finally:
        if close_db:
            db.close()


def run_fetch(date_str=None):
    """
    获取指定日期或今天的涨停数据
    
    策略：
    - 不指定日期（获取当天）：优先从盯盘页直接获取实时数据
    - 指定日期：使用原有的API方式
    """
    logger = logging.getLogger("fetch")
    
    db = Database(DB_PATH)
    db.init_new_tables()
    
    try:
        if date_str:
            # 指定了日期，使用原有的API方式
            logger.info(f"=== 获取历史数据 {date_str}（API模式） ===")
            fetcher = DataFetcher(db)
            
            target = date_str
            logger.info(f"=== 获取 {target} 数据 ===")
            
            # 1. 获取涨停基础数据（akshare）
            limit_count = fetcher.fetch_daily_limit_up(target)
            
            # 2. 获取概念数据（选股宝）
            concept_count = fetcher.fetch_concept_data(target)
            
            logger.info(f"数据获取完成: 涨停{limit_count}条, 概念数据{concept_count}条")
            
            if limit_count == 0:
                logger.warning(f"{target} 可能非交易日，尝试获取前一个交易日...")
                from datetime import timedelta
                prev = (datetime.strptime(target, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                limit_count = fetcher.fetch_daily_limit_up(prev)
                if limit_count > 0:
                    fetcher.fetch_concept_data(prev)
                    logger.info(f"成功获取 {prev} 数据: {limit_count}条")
            
            return limit_count
        else:
            # 获取当天数据，优先使用盯盘页实时数据
            logger.info("=== 获取当天实时数据（盯盘页模式） ===")
            try:
                from realtime_fetcher import fetch_realtime_today, save_realtime_to_db
                
                # 1. 从盯盘页获取实时数据
                realtime_data = fetch_realtime_today()
                
                # 2. 保存到数据库
                saved_count = save_realtime_to_db(DB_PATH, realtime_data)
                
                # 3. 额外获取概念数据（盯盘页可能没有概念信息）
                if saved_count > 0:
                    fetcher = DataFetcher(db)
                    target_date = realtime_data['date']
                    try:
                        concept_count = fetcher.fetch_concept_data(target_date)
                        logger.info(f"概念数据获取完成: {concept_count}条")
                    except Exception as e:
                        logger.warning(f"概念数据获取失败（不影响主流程）: {e}")
                
                logger.info(f"实时数据获取成功: {saved_count}条记录")
                return saved_count
                
            except ImportError as e:
                logger.warning(f"realtime_fetcher模块加载失败，降级到API模式: {e}")
            except Exception as e:
                logger.warning(f"盯盘页数据获取失败，降级到API模式: {e}")
            
            # 降级：使用原有的API方式
            logger.info("=== 降级到API模式获取当天数据 ===")
            fetcher = DataFetcher(db)
            target = datetime.now().strftime("%Y-%m-%d")
            
            limit_count = fetcher.fetch_daily_limit_up(target)
            concept_count = fetcher.fetch_concept_data(target)
            
            logger.info(f"API模式数据获取完成: 涨停{limit_count}条, 概念数据{concept_count}条")
            
            if limit_count == 0:
                logger.warning(f"{target} 可能非交易日，尝试获取前一个交易日...")
                from datetime import timedelta
                prev = (datetime.strptime(target, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                limit_count = fetcher.fetch_daily_limit_up(prev)
                if limit_count > 0:
                    fetcher.fetch_concept_data(prev)
                    logger.info(f"成功获取 {prev} 数据: {limit_count}条")
            
            return limit_count
        
    finally:
        db.close()


def main():
    """命令行入口"""
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
        # 支持可选参数
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