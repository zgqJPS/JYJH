"""
main_v2.py - 主入口（v3升级版）
集成周期模型、信号检测、个股推荐、策略跟踪
编排所有模块，支持以下模式：
- python main.py daily      : 执行每日完整流程（含信号检测+个股推荐）
- python main.py backtest   : 回测模式（含5信号深度回测）
- python main.py recommend  : 仅生成个股推荐
- python main.py signals    : 查看当前信号状态
- python main.py cycle      : 查看当前周期状态
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

# 新增模块
try:
    from cycle_model import CycleModel
    from stock_recommender import StockRecommender
    from backtester import Backtester
    from strategy_tracker import StrategyTracker
    V3_ENABLED = True
except ImportError as e:
    print(f"[警告] v3模块加载失败: {e}，降级为v2模式")
    V3_ENABLED = False


def setup_logging():
    """配置日志"""
    logging.basicConfig(
        level=getattr(logging, LOG_CONFIG["level"]),
        format=LOG_CONFIG["format"],
        datefmt=LOG_CONFIG["date_format"],
    )


def run_daily(db=None):
    """
    每日完整流程（v3升级版）：
    Step -1. 获取数据
    Step 0. 计算砸盘系数
    Step 1. 分析市场
    Step 2. 识别模式
    Step 3. 周期阶段判断（v3新增）
    Step 4. 信号检测（v3新增）
    Step 5. 生成预测
    Step 6. 个股推荐（v3新增）
    Step 7. 验证前一天的预测
    Step 8. 策略跟踪（v3新增）
    Step 9. 自我修正
    Step 10. 知识衰减
    Step 11. 生成报告
    """
    logger = logging.getLogger("daily")
    logger.info("=" * 60)
    logger.info("开始每日流程 (v3)")
    logger.info("=" * 60)
    
    close_db = False
    if db is None:
        db = Database(DB_PATH)
        db.init_new_tables()
        db.init_default_weights()
        close_db = True
    
    try:
        # Step -1: 获取数据
        logger.info("Step -1: 检查并获取最新数据...")
        fetcher = DataFetcher(db)
        today_str = datetime.now().strftime("%Y-%m-d")
        
        all_dates = db.get_all_dates()
        latest_existing = all_dates[-1] if all_dates else None
        
        if not all_dates or latest_existing < today_str:
            logger.info(f"尝试获取 {today_str} 的数据...")
            fetched_count = fetcher.fetch_daily_limit_up(today_str)
            if fetched_count > 0:
                logger.info(f"成功获取 {today_str} 涨停数据: {fetched_count} 条")
                fetcher.fetch_concept_data(today_str)
            else:
                logger.info(f"今天 {today_str} 无数据，尝试前一天...")
                from datetime import timedelta
                yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                fetched_count = fetcher.fetch_daily_limit_up(yesterday_str)
                if fetched_count > 0:
                    logger.info(f"成功获取 {yesterday_str} 涨停数据: {fetched_count} 条")
                    fetcher.fetch_concept_data(yesterday_str)
                else:
                    logger.warning("最近两天均无数据，使用数据库已有数据")
        
        all_dates = db.get_all_dates()
        if not all_dates:
            logger.error("数据库中没有涨停数据")
            return
        
        target_date = all_dates[-1]
        logger.info(f"分析日期: {target_date}")
        
        # 初始化模块
        analyzer = MarketAnalyzer(db)
        recognizer = PatternRecognizer(db)
        kb = KnowledgeBase(db)
        predictor = Predictor(db, kb)
        tracker = PredictionTracker(db)
        corrector = SelfCorrector(db, kb)
        reporter = Reporter(db, kb)
        smash_calc = SmashCoefficientCalculator(db)
        
        # Step 0: 计算砸盘系数
        logger.info("Step 0: 计算砸盘系数...")
        smash_coef, smash_max_boards = smash_calc.calculate_daily(target_date)
        if smash_coef is not None:
            logger.info(f"砸盘系数: {smash_coef}, 最高连板: {smash_max_boards}")
        
        # Step 1: 分析市场
        logger.info("Step 1: 分析市场...")
        analysis = analyzer.analyze_date(target_date)
        if not analysis:
            logger.error(f"分析失败: {target_date}")
            return
        
        snapshot = analyzer.generate_snapshot(analysis)
        
        # Step 2: 识别模式
        logger.info("Step 2: 识别模式...")
        patterns = recognizer.recognize_all(target_date, analysis)
        if snapshot:
            snapshot["cycle_phase"] = patterns.get("cycle_phase", "")
            db.save_daily_snapshot(target_date, snapshot)
        
        # v3 新增步骤
        cycle_info = {}
        signals = []
        recommendations = {}
        strategy_result = {}
        
        if V3_ENABLED:
            # Step 3: 周期阶段判断
            logger.info("Step 3: 周期阶段判断...")
            cycle_model = CycleModel(DB_PATH)
            cycle_info = cycle_model.detect_phase(target_date)
            logger.info(f"当前周期: {cycle_info.get('phase', '未知')} "
                       f"(置信度: {cycle_info.get('confidence', 0):.0%})")
            
            # Step 4: 信号检测
            logger.info("Step 4: 信号检测...")
            signals = cycle_model.detect_signals(target_date)
            triggered = [s for s in signals if s.get("triggered")]
            if triggered:
                for s in triggered:
                    logger.info(f"⚡ 信号触发: [{s['name']}] {s['details']}")
            else:
                logger.info("当前无高价值信号触发")
            
            # Step 6: 个股推荐
            logger.info("Step 6: 生成个股推荐...")
            recommender = StockRecommender(DB_PATH)
            recommendations = recommender.recommend(target_date)
            rec_count = len(recommendations.get("recommendations", []))
            logger.info(f"推荐个股: {rec_count}只")
        
        # Step 5: 生成预测
        logger.info("Step 5: 生成预测...")
        predictions = predictor.predict_next_day(target_date, analysis, patterns)
        
        # Step 7: 验证前一天的预测
        logger.info("Step 7: 验证前一天的预测...")
        verifications = []
        current_idx = all_dates.index(target_date)
        if current_idx > 0:
            verifications = tracker.verify_predictions_for_date(target_date, analysis)
        
        # Step 8: 策略跟踪
        if V3_ENABLED and triggered:
            logger.info("Step 8: 策略跟踪...")
            strategy_tracker = StrategyTracker(DB_PATH)
            for s in triggered:
                strategy_tracker.record_signal_trigger(
                    date=target_date,
                    signal_id=s["signal_id"],
                    details=s["details"]
                )
            # 验证前一天的信号
            if current_idx > 0:
                prev_date = all_dates[current_idx - 1]
                for sig_id in range(1, 6):
                    strategy_tracker.verify_signal(sig_id, prev_date)
            strategy_result = strategy_tracker.get_signal_stats()
        
        # Step 9: 自我修正
        logger.info("Step 9: 自我修正...")
        corrections = []
        if verifications:
            corrections = corrector.correct(verifications, target_date)
        
        # Step 10: 知识衰减
        logger.info("Step 10: 知识衰减检查...")
        kb.apply_decay()
        
        smash_adaptation = corrector.adapt_smash_thresholds(target_date)
        
        # v3: 自动调整参数
        if V3_ENABLED:
            strategy_tracker = StrategyTracker(DB_PATH)
            strategy_tracker.auto_adjust_parameters()
        
        # Step 11: 生成报告
        logger.info("Step 11: 生成报告...")
        report = reporter.generate_daily_report(
            target_date, analysis, patterns, predictions,
            verifications, corrections, 
            kb.match_current_pattern(analysis, patterns))
        
        print("\n" + report)
        
        # v3: 输出周期和信号摘要
        if V3_ENABLED:
            print("\n" + "=" * 40)
            print("📊 周期状态: " + cycle_info.get("phase", "未知"))
            print(f"   置信度: {cycle_info.get('confidence', 0):.0%}")
            print(f"   建议: {cycle_info.get('advice', '')}")
            
            triggered = [s for s in signals if s.get("triggered")]
            if triggered:
                print(f"\n⚡ 触发信号 ({len(triggered)}个):")
                for s in triggered:
                    print(f"   [{s['name']}] {s['details']}")
            
            if recommendations.get("recommendations"):
                print(f"\n📈 个股推荐 ({len(recommendations['recommendations'])}只):")
                for r in recommendations["recommendations"][:5]:
                    print(f"   {r['name']}({r['code']}) {r['score']}分 "
                          f"{r['boards']}板 {r['action']} [{r.get('risk_level', '?')}]")
            print("=" * 40)
        
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
            "cycle_info": cycle_info,
            "signals": signals,
            "recommendations": recommendations,
            "strategy": strategy_result,
        }
        
    except Exception as e:
        logger.error(f"每日流程异常: {e}", exc_info=True)
    finally:
        if close_db:
            db.close()


def run_deep_backtest(db=None, start_date=None, end_date=None):
    """
    深度回测模式：
    1. 用5个高价值信号进行回测验证
    2. 评估每个信号的命中率和收益
    3. 输出完整回测报告
    """
    logger = logging.getLogger("deep_backtest")
    logger.info("=" * 60)
    logger.info("开始深度回测 (v3)")
    logger.info("=" * 60)
    
    if not V3_ENABLED:
        logger.error("v3模块未加载，无法执行深度回测")
        return
    
    try:
        try:
            from backtester import DataStore, Backtester
            data_store = DataStore(DB_PATH)
            backtester = Backtester(data_store)
        except ImportError:
            from backtester import Backtester
            backtester = Backtester()
        
        # 执行完整回测
        result = backtester.run_backtest(start_date=start_date, end_date=end_date)
        
        # 打印报告
        print("\n" + "=" * 60)
        print("📊 深度回测报告")
        print("=" * 60)
        
        for signal_id, signal_result in result.get("signals", {}).items():
            name = signal_result.get("name", f"信号{signal_id}")
            total = signal_result.get("total_triggers", 0)
            hits = signal_result.get("hits", 0)
            hit_rate = signal_result.get("hit_rate", 0)
            avg_return = signal_result.get("avg_return", 0)
            
            print(f"\n信号{signal_id} [{name}]:")
            print(f"  触发次数: {total}")
            print(f"  命中次数: {hits}")
            print(f"  命中率: {hit_rate:.1%}")
            print(f"  平均收益: {avg_return:+.1f}只涨停")
            
            # 打印详细触发记录
            for trigger in signal_result.get("triggers", []):
                result_str = "✅" if trigger.get("hit") else "❌"
                print(f"  {result_str} {trigger['date']}: {trigger.get('details', '')}")
        
        # 总体策略评估
        overall = result.get("overall", {})
        print(f"\n{'=' * 60}")
        print(f"总体策略评估:")
        print(f"  回测天数: {overall.get('total_days', 0)}")
        print(f"  总信号触发: {overall.get('total_signals', 0)}")
        print(f"  总命中: {overall.get('total_hits', 0)}")
        print(f"  综合命中率: {overall.get('overall_hit_rate', 0):.1%}")
        
        return result
        
    except Exception as e:
        logger.error(f"深度回测异常: {e}", exc_info=True)
        return None


def run_recommend(date_str=None):
    """仅生成个股推荐"""
    logger = logging.getLogger("recommend")
    
    if not V3_ENABLED:
        logger.error("v3模块未加载")
        return
    
    db = Database(DB_PATH)
    try:
        all_dates = db.get_all_dates()
        target = date_str or (all_dates[-1] if all_dates else None)
        
        if not target:
            print("无数据")
            return
        
        recommender = StockRecommender(DB_PATH)
        result = recommender.recommend(target)
        
        print(f"\n📈 {target} 个股推荐")
        print(f"周期阶段: {result.get('cycle_phase', '未知')}")
        print(f"{'=' * 60}")
        
        for r in result.get("recommendations", []):
            print(f"\n{r['name']}({r['code']}) - 总分: {r['score']}")
            print(f"  连板: {r['boards']}板 | 概念: {', '.join(r.get('concepts', [])[:3])}")
            print(f"  封板: {r.get('seal_style', '')} | 封单: {r.get('seal_amount', 0):.2f}亿")
            print(f"  建议: {r['action']} | 风险: {r.get('risk_level', '?')}")
            print(f"  理由: {r.get('reason', '')}")
        
        return result
        
    finally:
        db.close()


def run_signals(date_str=None):
    """查看当前信号状态"""
    if not V3_ENABLED:
        print("v3模块未加载")
        return
    
    db = Database(DB_PATH)
    try:
        all_dates = db.get_all_dates()
        target = date_str or (all_dates[-1] if all_dates else None)
        
        if not target:
            print("无数据")
            return
        
        cycle_model = CycleModel(DB_PATH)
        
        # 周期状态
        cycle = cycle_model.detect_phase(target)
        print(f"\n📊 {target} 市场状态")
        print(f"{'=' * 60}")
        print(f"周期阶段: {cycle.get('phase', '未知')} (置信度: {cycle.get('confidence', 0):.0%})")
        print(f"建议: {cycle.get('advice', '')}")
        
        ind = cycle.get("indicators", {})
        print(f"\n关键指标:")
        print(f"  砸盘系数: {ind.get('smash_coefficient', '?')}")
        print(f"  最高连板: {ind.get('max_boards', '?')}")
        print(f"  涨停数量: {ind.get('limit_up_count', '?')}")
        print(f"  砸盘变化: {ind.get('sc_change', 0):+.2f}")
        
        # 信号检测
        signals = cycle_model.detect_signals(target)
        triggered = [s for s in signals if s.get("triggered")]
        
        print(f"\n信号检测 ({len(triggered)}个触发):")
        for s in signals:
            status = "⚡触发" if s.get("triggered") else "  --  "
            print(f"  {status} 信号{s['signal_id']} [{s.get('name', '')}] {s.get('details', '')}")
        
        # 连板预测
        board_pred = cycle_model.predict_next_day_boards(target)
        print(f"\n连板预测:")
        print(f"  预测明日: {board_pred.get('predicted_boards', '?')}板")
        print(f"  上升概率: {board_pred.get('probability_up', 0):.0%}")
        print(f"  下降概率: {board_pred.get('probability_down', 0):.0%}")
        print(f"  理由: {board_pred.get('reason', '')}")
        
        return {"cycle": cycle, "signals": signals, "board_prediction": board_pred}
        
    finally:
        db.close()


def run_backtest(db=None, start_date=None, end_date=None, max_days=None):
    """回测模式（保持v2兼容）"""
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
            end_idx = min(end_idx, start_idx + max_days - 1)
        
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
        
        date_range = f"{backtest_dates[0]} ~ {backtest_dates[-1]}"
        backtest_report = reporter.generate_backtest_report(results, date_range)
        
        report_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "reports", "backtest_report.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(backtest_report)
        
        print("\n" + backtest_report)
        
        corrector.export_weights_json(
            os.path.join(KNOWLEDGE_DIR, "model_weights.json"))
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
        health = corrector.get_model_health()
        print(f"\n模型状态: {health.get('status', '未知')}")
        print(f"平均可信度: {health.get('avg_credibility', 0):.3f}")
        
        if V3_ENABLED:
            all_dates = db.get_all_dates()
            if all_dates:
                latest = all_dates[-1]
                cycle_model = CycleModel(DB_PATH)
                cycle = cycle_model.detect_phase(latest)
                print(f"\n周期状态({latest}): {cycle.get('phase', '未知')}")
                
                strategy_tracker = StrategyTracker(DB_PATH)
                stats = strategy_tracker.get_signal_stats()
                print(f"\n信号跟踪统计:")
                for sid, s in stats.get("signals", {}).items():
                    print(f"  信号{sid}: 触发{s.get('total_triggers',0)}次, "
                          f"命中{s.get('hits',0)}次, "
                          f"命中率{s.get('hit_rate',0):.0%}")
        
        return status
    finally:
        if close_db:
            db.close()


def run_fetch(date_str=None):
    """获取指定日期或今天的涨停数据"""
    logger = logging.getLogger("fetch")
    db = Database(DB_PATH)
    db.init_new_tables()
    try:
        fetcher = DataFetcher(db)
        if date_str:
            target = date_str
        else:
            target = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"=== 获取 {target} 数据 ===")
        limit_count = fetcher.fetch_daily_limit_up(target)
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
    finally:
        db.close()


def main():
    """命令行入口"""
    setup_logging()
    
    if len(sys.argv) < 2:
        print("用法: python main.py <command>")
        print("  daily      - 执行每日完整流程（获取→分析→预测→推荐→验证→修正）")
        print("  backtest   - 回测模式（用历史数据验证系统）")
        print("  deep-test  - 深度回测（5信号验证）")
        print("  recommend  - 仅生成个股推荐")
        print("  signals    - 查看当前信号状态")
        print("  report     - 仅生成报告")
        print("  status     - 查看系统状态和模型健康度")
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
    elif command == "deep-test":
        start_date = None
        end_date = None
        for arg in sys.argv[2:]:
            if arg.startswith("--start="):
                start_date = arg.split("=")[1]
            elif arg.startswith("--end="):
                end_date = arg.split("=")[1]
        run_deep_backtest(start_date=start_date, end_date=end_date)
    elif command == "recommend":
        date_str = sys.argv[2] if len(sys.argv) > 2 else None
        run_recommend(date_str=date_str)
    elif command == "signals":
        date_str = sys.argv[2] if len(sys.argv) > 2 else None
        run_signals(date_str=date_str)
    elif command == "report":
        date_str = sys.argv[2] if len(sys.argv) > 2 else None
        run_report(date_str=date_str)
    elif command == "status":
        run_status()
    else:
        print(f"未知命令: {command}")


if __name__ == "__main__":
    main()
