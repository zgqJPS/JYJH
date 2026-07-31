# app_v2_additions.py
# 这些函数需要追加到app.py中

# ============ v3 新增 API ============

def handle_stock_recommendations(params):
    """个股推荐API"""
    try:
        from stock_recommender import StockRecommender
        date_str = params.get("date", [None])[0]
        
        db = Database(DB_PATH)
        all_dates = db.get_all_dates()
        db.close()
        
        if not date_str:
            date_str = all_dates[-1] if all_dates else None
        
        if not date_str:
            return {"success": False, "error": "无数据"}
        
        recommender = StockRecommender(DB_PATH)
        result = recommender.recommend(date_str)
        
        return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Stock recommendations error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def handle_signal_detection(params):
    """信号检测API"""
    try:
        from cycle_model import CycleModel
        date_str = params.get("date", [None])[0]
        
        db = Database(DB_PATH)
        all_dates = db.get_all_dates()
        db.close()
        
        if not date_str:
            date_str = all_dates[-1] if all_dates else None
        
        if not date_str:
            return {"success": False, "error": "无数据"}
        
        cycle_model = CycleModel(DB_PATH)
        
        # 周期状态
        cycle = cycle_model.detect_phase(date_str)
        
        # 信号检测
        signals = cycle_model.detect_signals(date_str)
        
        # 连板预测
        board_pred = cycle_model.predict_next_day_boards(date_str)
        
        return {
            "success": True,
            "data": {
                "date": date_str,
                "cycle": cycle,
                "signals": signals,
                "board_prediction": board_pred,
            }
        }
    except Exception as e:
        logger.error(f"Signal detection error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def handle_deep_backtest(body):
    """深度回测API（5信号回测）"""
    try:
        from backtester import Backtester
        data = json.loads(body) if body else {}
        start_date = data.get("start_date")
        end_date = data.get("end_date")
        
        task_id = str(uuid.uuid4())[:8]
        
        def _run():
            with tasks_lock:
                tasks[task_id] = {
                    "id": task_id, "type": "deep_backtest",
                    "status": "running", "progress": 10,
                    "message": "正在执行深度回测...",
                    "result": None,
                    "created_at": datetime.now().isoformat(),
                }
            
            try:
                from backtester import DataStore, Backtester
                data_store = DataStore(DB_PATH)
                backtester = Backtester(data_store)
            except ImportError:
                from backtester import Backtester
                backtester = Backtester()
            result = backtester.run_backtest(start_date=start_date, end_date=end_date)
            
            with tasks_lock:
                tasks[task_id]["progress"] = 100
                tasks[task_id]["status"] = "completed"
                tasks[task_id]["message"] = "深度回测完成"
                tasks[task_id]["result"] = result
        
        thread = threading.Thread(target=_run)
        thread.daemon = True
        thread.start()
        
        return {"success": True, "task_id": task_id}
    except Exception as e:
        logger.error(f"Deep backtest error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def handle_strategy_status():
    """策略跟踪状态API"""
    try:
        from strategy_tracker import StrategyTracker
        tracker = StrategyTracker(DB_PATH)
        
        stats = tracker.get_signal_stats()
        report = tracker.generate_strategy_report()
        
        return {"success": True, "data": {"stats": stats, "report": report}}
    except Exception as e:
        logger.error(f"Strategy status error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def handle_cycle_history(params):
    """周期历史API（用于可视化）"""
    try:
        from cycle_model import CycleModel
        start_date = params.get("start_date", [None])[0]
        end_date = params.get("end_date", [None])[0]
        
        cycle_model = CycleModel(DB_PATH)
        cycles = cycle_model.get_historical_cycles(start_date=start_date, end_date=end_date)
        
        return {"success": True, "data": cycles}
    except Exception as e:
        logger.error(f"Cycle history error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# 需要在 RequestHandler.do_GET 中添加以下路由:
# elif path == "/api/recommendations":
#     self._json_response(handle_stock_recommendations(params))
# elif path == "/api/signals":
#     self._json_response(handle_signal_detection(params))
# elif path == "/api/strategy/status":
#     self._json_response(handle_strategy_status())
# elif path == "/api/cycle/history":
#     self._json_response(handle_cycle_history(params))

# 需要在 RequestHandler.do_POST 中添加:
# elif path == "/api/deep-backtest":
#     self._json_response(handle_deep_backtest(body))
