# console-tui srv-1 部署手册（线E，2026-09-29）

owner 目标形态：**ssh newbox 后敲 `crane-tui` 即起治理面 TUI**（全貌 8 面板 + `n` 下单）。

## 一、首次安装（root；本机已装则跳到第三节）

```sh
git clone https://github.com/CloudCrane-Software/jiuwen-glue.git /opt/company-ops/src/jiuwen-glue
sh /opt/company-ops/src/jiuwen-glue/tools/console-tui/deploy/install-srv1.sh
```

脚本幂等可重放，做五件事：
1. 独立 venv `/opt/company-ops/console-tui-venv`（PyPI 不通自动切清华镜像）；
2. DDL：`sql/003_console_views.sql`（v2：v_node_utilization 带节点心跳投影）；
3. DCL：`sql/010_console_grants.sql`（`v_usage`/`v_signal_timeline` → jiuwen SELECT）；
4. `/usr/local/bin/crane-tui` 包装脚本；
5. `/opt/company-ops/coldstart/tui.env`（root 0600）——`CONSOLE_TUI_DSN` 经 bao
   `kv/company/gpu/team-db` 现取写入，**不回显不落日志**；另含
   `CONSOLE_TUI_PROBE_TARGETS=anuser@36.139.118.235:jiuwenswarm-app,vllm-local`
   （探针走 GPU 机公网 IP：tailnet 数据面实测单向劣化，ICMP 通但 TCP/22 超时；
   GPU 机 sshd 仅密钥认证）。

## 二、升级（deploy 同步走 PR + 实机拉取）

```sh
cd /opt/company-ops/src/jiuwen-glue && git pull
sh tools/console-tui/deploy/install-srv1.sh        # 幂等：重跑只更新包与 DDL
```

## 三、headless 冒烟（不进 TUI 也能验证部署）

```sh
/opt/company-ops/console-tui-venv/bin/python - <<'PY'
import os
from console_tui.data import connect_pg
store = connect_pg()                                # DSN 从 CONSOLE_TUI_DSN 读
print("mode:", store.mode, "| tasks:", len(store.tasks()),
      "| nodes:", len(store.nodes()), "| usage:", len(store.usage()),
      "| timeline:", len(store.timeline()))
PY
```

期望输出 `mode: pg` 且各面板行数 > 0（usage/timeline 依赖 010 授权）。
测试套件（可选）：`$VENV/bin/pip install pytest pytest-asyncio` 后在
`tools/console-tui` 下 `$VENV/bin/python -m pytest -q`。

## 四、键位速查

| 键 | 动作 |
| --- | --- |
| `n` | 新建工单（自然语言 → RULES 分解 周报/复审/测试/部署 → PENDING → worker 自取） |
| `s`/`a`/`p` | 三级干预 steer/审批/暂停（全部留痕） |
| `r` | 刷新（另有 30s 自动轮询） |
| `o` | 工单看板排序轮换（状态阻塞优先/创建时间/Owner） |
| `?` | 帮助 + 颜色语义图例（绿=运行 黄=待处理 红=阻塞 灰=终态） |
| `q` | 退出 |

## 五、红线与边界

- 不动 company-bao-1/temporal 既有容器；DB 只加视图/授权（001-009 语义不变）；
- DSN 只在 `tui.env`（0600）与进程 environ，不入代码/git/日志；
- 服务健康探针为 ssh **只读**（`systemctl is-active`），对 GPU 机无写操作；
- 探针前置：srv-1 的公钥已在 GPU 机 anuser `authorized_keys`（安装时另行配置），
  未配置时面板显示 unreachable，不影响其余面板。
