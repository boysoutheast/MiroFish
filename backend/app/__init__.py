"""
MiroFish Backend - Flask应用工厂
"""

import os
import warnings

# 抑制 multiprocessing resource_tracker 的警告（来自第三方库如 transformers）
# 需要在所有其他导入之前设置
warnings.filterwarnings("ignore", message=".*resource_tracker.*")

from flask import Flask, request
from flask_cors import CORS

from .config import Config
from .utils.logger import setup_logger, get_logger


def create_app(config_class=Config):
    """Flask应用工厂函数"""
    app = Flask(__name__)
    app.config.from_object(config_class)
    
    # 设置JSON编码：确保中文直接显示（而不是 \uXXXX 格式）
    # Flask >= 2.3 使用 app.json.ensure_ascii，旧版本使用 JSON_AS_ASCII 配置
    if hasattr(app, 'json') and hasattr(app.json, 'ensure_ascii'):
        app.json.ensure_ascii = False
    
    # 设置日志
    logger = setup_logger('mirofish')
    
    # 只在 reloader 子进程中打印启动信息（避免 debug 模式下打印两次）
    is_reloader_process = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    debug_mode = app.config.get('DEBUG', False)
    should_log_startup = not debug_mode or is_reloader_process
    
    if should_log_startup:
        logger.info("=" * 50)
        logger.info("MiroFish Backend 启动中...")
        logger.info("=" * 50)
    
    # 启用CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # 注册模拟进程清理函数（确保服务器关闭时终止所有模拟进程）
    from .services.simulation_runner import SimulationRunner
    SimulationRunner.register_cleanup()
    if should_log_startup:
        logger.info("已注册模拟进程清理函数")

    # T1: 启动时核对每个 run_state.json 里"应该还活着"的模拟（STARTING/
    # RUNNING/PAUSED/STOPPING），把进程真的已经死掉（例如服务器重启）的标记
    # 为 CRASHED。这是一个新 guard（不是"跟 register_cleanup() 一样的
    # guard"——register_cleanup() 本身没有任何 guard，所有进程都会跑一次，
    # 只有它的 LOG 语句用 debug_mode 做了 guard）。这里直接复用第 34 行算出
    # 的 debug_mode（来自 app.config['DEBUG']），跟 should_log_startup 用
    # 同一个逻辑：非 debug 模式下永远跑；debug 模式下只在 reloader 子进程
    # （WERKZEUG_RUN_MAIN=true，真正服务请求的进程）跑一次，避免父进程和
    # 子进程各跑一次导致 reconciliation 执行两次。
    #
    # 导入和调用都包在 try/except 里：reconcile_on_startup() 内部本身已经
    # 包住 try/except（各别模拟的 reconcile 失败不影响其他模拟），但这里
    # 的 import 语句本身没有被那层保护——如果新依赖（如 psutil）在某个部署
    # 环境没装上，或者模块里有其他 ImportError/SyntaxError，会直接从
    # create_app() 冒出来，导致后端整个起不来。reconciliation 失败绝对不能
    # 让后端启动失败。
    should_reconcile = not debug_mode or is_reloader_process
    if should_reconcile:
        try:
            from .services.simulation_reconciler import reconcile_on_startup
            reconcile_on_startup()
            if should_log_startup:
                logger.info("已完成模拟状态核对（reconciliation）")
        except Exception:
            logger.exception(
                "载入或执行 reconcile_on_startup() 失败，后端仍然继续启动"
                "（reconciliation 不是启动的必要条件）。"
            )
    
    # 请求日志中间件
    @app.before_request
    def log_request():
        logger = get_logger('mirofish.request')
        logger.debug(f"请求: {request.method} {request.path}")
        if request.content_type and 'json' in request.content_type:
            logger.debug(f"请求体: {request.get_json(silent=True)}")
    
    @app.after_request
    def log_response(response):
        logger = get_logger('mirofish.request')
        logger.debug(f"响应: {response.status_code}")
        return response
    
    # 注册蓝图
    from .api import graph_bp, simulation_bp, report_bp
    app.register_blueprint(graph_bp, url_prefix='/api/graph')
    app.register_blueprint(simulation_bp, url_prefix='/api/simulation')
    app.register_blueprint(report_bp, url_prefix='/api/report')
    
    # 健康检查
    @app.route('/health')
    def health():
        return {'status': 'ok', 'service': 'MiroFish Backend'}
    
    if should_log_startup:
        logger.info("MiroFish Backend 启动完成")
    
    return app

