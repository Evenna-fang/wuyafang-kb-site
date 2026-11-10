#!/usr/bin/env python3
"""总地图 N4/N5：展板 cron/事件兼容入口；stamp、输出与锁统一由渲染器负责。"""
from render_dashboard import main


if __name__ == "__main__":
    raise SystemExit(main())
