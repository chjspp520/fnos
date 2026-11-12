# 变更记录

## 1.1.2（2026-11-12）修复回调线程类型与定时器参数

**修复 Core 崩溃隐患（社区 Issue）**
- `sensor.py` 中 `FnOsAllEntitiesSensor` 的 `_handle_state_changed` /
  `_handle_registry_event` / `_handle_device_event` 补上 `@callback`。
  之前它们是普通同步函数，HA 会按 **Executor 线程**调度，回调里再调
  `async_call_later()` 就是从线程操作事件循环，可能出现
  `RuntimeError: list changed size during iteration` 并导致 Core 退出。
- 同时复核了 `switch.py` / `button.py` / `web_proxy.py` 的全部事件与定时器注册点，
  其它回调均已带 `@callback` 或本身是 `async`。

**修复空闲回收定时器**
- `web_proxy.py` 的空闲回收注册方式写错：`async_track_time_interval` 会把
  `datetime` 作为第一个参数传给回调，而原函数第一个参数是 `hass`，
  导致每 5 分钟刷一条
  `AttributeError: 'datetime.datetime' object has no attribute 'data'`。
  改为闭包包装（`_reap_idle(_now=None)`）。

**新增回归防线**
- `tests/check_callbacks.py`：静态扫描所有 `async_listen` /
  `async_track_time_interval` / `async_dispatcher_connect` / `async_call_later`
  注册点，发现「同步函数且无 `@callback`」直接失败，防止同类问题再次引入。

## 1.1.1（2026-11-01）进一步降低 NAS / HA 开销

**NAS 侧请求（相对 1.1.0 再降约 40%）**
- 主机静态信息（主机名/版本/机器码/风扇模式/电源计划）改为 **10 分钟**一次；每轮只取运行时长
- 无独显 / 非 trim 硬件：`resmon.gpu`、`resmon.sysFan` 探测一次后跳过（10 分钟复探）
- 下载中心未安装 → **10 分钟负缓存**；已安装时优先只调 `task.query`，`stat.all` 仅作兜底
- 虚拟机配置详情（vCPU/内存/芯片组/固件/显卡/磁盘/MAC）TTL 60 → **300 秒**（电源状态仍在快照里每轮更新）
- 相册「最近添加」改为**按需**：只在打开相册悬浮窗时拉取并重签，不再定时轮询
- 悬浮窗专用连接**空闲 30 分钟自动回收**（不再“打开过一次就常驻”）
- 浏览器地址心跳只在 origin **变化时**才上报（原来 30 秒节流但相同地址也发）

**HA 侧**
- 设备注册表只在「容器/虚拟机集合、主机名、版本」变化时重扫，另有 10 分钟兜底
- 传感器一次状态写入只计算一次取值（原来 `native_value` / `native_unit` 各算一遍）

**保持不变（按你的要求）**
- 容器 CPU/内存/网络曲线仍跟随「数据刷新间隔」（默认 30 秒）
- 实体清单、实体状态与属性结构完全不变，前端无需改动

## 1.1.0（2026-11-01）性能与稳定性优化

**分层刷新 + 缓存（NAS 请求量下降约一半）**
- 快照按数据块做 TTL 缓存：主机/性能/容器/虚拟机每轮取；存储、服务开关、网卡 2–5 分钟；系统更新 30 分钟；相册/影视可用性 10 分钟；已装应用 15 分钟；硬件信息 1 小时
- 取数失败（异常或空结果）自动沿用上一轮数据，实体不再闪成“未知/0”
- 开关/按钮/电源操作后主动作废对应缓存，操作立刻可见
- 单次 WS/REST 超时由 30s 降到 10s（流式接口 15s），避免拖垮整轮刷新

**实体写入优化**
- 网速固定 **KB/s**、磁盘读写固定 **MB/s**、内存/存储/虚拟机容量固定 **GB**，不再出现 HA「单位已改变」告警
- 「远程地址」「控制台地址」传感器改为事件驱动（设备链接变化 + 远程控制开关 + 12 小时续签），不再每 30 秒重签一次长链接
- 相册「最近添加」5 分钟才真正拉取一次
- `sensor.fnos_all_entities` 改为签名比对，状态没变不重写整包属性
- 平台轮询间隔统一到 60 秒
- 虚拟机详情缓存按配置条目隔离，多台 NAS 同名虚拟机不再串数据

**其它**
- 新增 `diagnostics.py`：设备与服务 → 飞牛 fnOS → 下载诊断（密码/token 已脱敏）
- `manifest.json` 补充 `integration_type` / `loggers`，新增 `hacs.json`

## 1.0.4（2026-11-01）修复重载与 aiohttp 兼容

- 修复 `async_forward_entry_unload` 误传列表导致**卸载失败、必须重启 HA**（改用 `async_unload_platforms`）
- 修复 aiohttp 3.13+ 移除 `aiohttp.web.WSCloseCode` 导致的 WebSocket 关闭异常
- WS 收尾逐个兜异常，重载/停机时不再刷 aiohttp 错误日志
- 修复 `DeviceEntry.config_entries` 弃用告警（改用 `config_entry_id`）

## 1.0.3（2026-11-01）设备增删跟随

- 新建虚拟机/容器：设备与实体在同一轮刷新内一起出现（不再等 60 秒定时器）
- 删除虚拟机/容器：实体与设备同轮清理；HA 停机期间删除的，启动时补一次清理
- 新增虚拟机会同时补建「远程地址」「控制台地址」，并补签「访问」链接

## 1.0.1 – 1.0.2

- 网速、磁盘读写单位固定，避免单位跳变告警
- 远程控制开关（noVNC）回调改为线程安全（修复 `async_write_ha_state` 跨线程报错）
- 设备注册表接口改用 `async_get_device_by_identifier`，消除弃用告警
- 仓库地址/维护者更新为 `github.com/chjspp520/fnos`、`@chjspp520`

## 1.0.0

- 首个版本：系统监控、硬盘健康、Docker 容器、虚拟机、文件/相册/影视、服务开关、电源操作
- 聚合实体 `sensor.fnos_all_entities`
- 设备「访问」链接 + 同源反向代理 + 前端悬浮窗；虚拟机 noVNC 控制台（远程控制总开关）
