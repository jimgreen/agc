# AGC controller

独立的自动发电控制（AGC）程序，支持交流侧、直流侧及交直流混合场景中的风电、光伏、储能、燃料电池、制氢负荷和柴发协同控制。

## 控制策略

- 决策只使用实时采集量，不依赖 p_fur 预测值。
- 新能源优先：先计算新能源接纳空间（含储能 SOC 约束），再进行小步试探调节。
- 功率缺额按“储能放电 → 燃料电池 → 柴发补偿”处理。
- 功率盈余按新能源消纳、储能充电和制氢吸收处理。
- 受控设备支持 p_step/p_dead（以及兼容别名）控制步长和死区，减少频繁调节。

## 文件

- agc_ctrl.py：AGC 控制器及命令行入口。
- efile_read.py：E-file 读取和写回支持。
- paths.py：独立仓库路径解析。
- model_real.e：可直接运行的 AC/DC 混合示例模型。

## 运行

在本目录执行：

    python agc_ctrl.py --help
    python agc_ctrl.py --model model_real.e --step

控制器默认从模型文件所在目录读取模型；可通过 --model 指定其他 E-file。运行前请确保实时测量源可用；model_real.e 仅用于接口和策略验证。

## Web 人机界面

在本目录执行：

    python web/server.py

浏览器打开 `http://127.0.0.1:8765/`。Web HMI 提供系统总览、实时监视、参数配置、日志查询和历史查询，并支持启动/停止、开环/闭环切换。

实时监视页面可逐台设置“远方 · 自动”或“本地 · 曲线”。远方设备参与闭环自动决策；本地设备跳过自动决策，使用保存的默认功率曲线。每台设备的默认曲线为 96 点，按 15 分钟覆盖 00:00—23:45，可在“96点曲线”编辑器中保存。

实时监视中的“设备控制详情”可选择任意设备，修改受控状态、运行状态、控制步长、控制死区、功率上下限和默认功率；右侧可编辑 96 点曲线。详情面板同时显示该设备的实时功率曲线、AGC 指令曲线、默认曲线，以及每个控制时刻的实时值、指令值、默认值和参数。

实时监视已按“实时总览、设备列表、设备详情、控制指令”拆分为分页标签；设备、日志、历史和控制指令表均采用分页显示，页面保持在当前视口内，不产生横向或纵向滚动条。

Web 配置和历史数据保存在 `web/data/`，不会修改原始 `model_real.e`。Web 服务每次状态刷新都会同步计算后台写入的最新周期、设备采样、控制指令和环境量；计算后台的运行日志也会进入同一数据库的日志表。

实时轮询使用 `/api/state?lite=1`，省略历史曲线和 96 点曲线，设备详情打开时再按需读取，减少浏览器和服务端的 JSON 传输。设备列表支持勾选后调用 `/api/device-batch` 批量切换本地/远方，批量接口仍逐台复用同一套边界、权限和 SCADA 下发校验。计算后台对已成功下发且数值未变化的 YK/YT 指令自动去重，降低 SCADA 链路负荷。

## AGC SQLite 数据库

Web 服务和 AGC 计算后台共用 `D:\\codex\\sqldb\\realtime\\agc\\agc.db` 作为运行数据库，建表脚本为 `web/agc_schema.sql`。也可以通过 `AGC_DB` 环境变量覆盖数据库文件路径。计算后台启动时读取数据库中的系统配置、设备受控状态、控制参数和 96 点默认曲线；每个控制周期完成后写入系统周期、设备实时采样和 AGC 指令，供 Web 界面实时展示、日志查询和历史查询。

- `system_settings`：控制周期、步长和开环/闭环模式
- `devices`：设备受控状态、运行状态、功率上下限、步长、死区和默认功率
- `device_curves`：每台设备 96 个 15 分钟默认曲线点
- `control_cycles`：各控制周期的系统功率、环境量和 AGC 平衡
- `device_samples`：各设备在每个控制时刻的实时功率与指令功率
- `event_logs`：控制启动、模式切换、设备指令和运行日志

服务首次启动时会把旧版 `web/data/settings.json`、`device_modes.json`、`history.json` 和 `command_history.json` 自动迁移到 SQLite；之后界面查询直接读取数据库。SQLite 文件及运行时数据库文件已加入 Git 忽略规则。

## 依赖

仅使用 Python 标准库。

## SCADA 四遥联动

计算周期先通过 SCADA 遥测（YC）和遥信（YX）刷新模型；YX 可通过设备的
`run_yx_id`/`status_yx_id`/`yx_id` 映射同步设备运行状态。闭环模式下，自动决策和
本地默认曲线才会通过遥调（YT）下发有功指令；设备运行状态的界面操作通过遥控（YK）
下发。开环模式仍采集 YC/YX，但不会发送自动 YK/YT 指令。指令接口返回失败或超时
时，内存中的控制值回滚，失败原因写入日志和 `command_results`，下一周期可重试。

现场可以任选一种适配方式：

- Python FES 模块：设置 `AGC_FES_MODULE`（默认 `fes_interface`），提供
  `get_yc_value`、`get_yx_value`、`send_yk_value`、`send_yt_value`；兼容 4/5 参数
 旧接口以及 2/3 参数简化接口。
- HTTP：设置 `AGC_SCADA_API_URL`，读取使用
  `/api/points/yc|yx?rtu_id=&pnt_no=&limit=1`，写入默认使用
  `/api/points/{kind}/{rtu}/{pnt}`。若现场命令路由不同，可用
  `AGC_SCADA_COMMAND_URL` 覆盖，其中支持 `{kind}`、`{rtu}`、`{pnt}` 占位符；
  `AGC_SCADA_TOKEN` 和 `AGC_SCADA_TIMEOUT` 分别配置认证和超时。
- `scdfe_c` 指令服务：设置 `AGC_SCADA_COMMAND_ENDPOINT` 指向认证端点 JSON；若
  设置 `AGC_SCADA_DB`，程序也会自动查找同名 `.scdfe_command.json` 端点文件。

设备到现场控制点的映射写在模型设备行的元数据中：有功遥调支持
`p_yt_id`/`yt_id`/`control_yt_id`/`power_yt_id`，或 `p_yt_rtu`+`p_yt_pnt`；启停
遥控支持 `run_yk_id`/`status_yk_id`/`yk_id`/`control_yk_id`，或
`run_yk_rtu`+`run_yk_pnt`。没有映射的设备仍可运行仿真和界面逻辑，但不会伪造现场
指令成功；接入真实设备前必须按现场点表补齐映射并验证权限。
