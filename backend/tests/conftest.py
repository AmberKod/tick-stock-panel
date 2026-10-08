"""pytest 全局夹具。

S4 切片① 引入 Postgres 镜像后, 测试必须**默认不连 DB**:
`TSP_POSTGRES_ENABLED=false` 让 `app.state.db.init()` 走 disabled 分支
(不建池、不建表、不写、不探活, 整个进程零 DB 调用)。

pytest 保证 conftest.py 先于任何测试模块被导入, 因此 `app.config.settings`
实例化时读到的就是 false。集成测试想真连库时, 在 shell 里显式设
`TSP_POSTGRES_ENABLED=true` 覆盖(setdefault 不会盖掉已有的值)。
"""
from __future__ import annotations

import os

os.environ.setdefault("TSP_POSTGRES_ENABLED", "false")
