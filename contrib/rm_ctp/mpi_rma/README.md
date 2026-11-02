# MPI单边读写与原版OSU接入（候选，2026-11-02）

起点：GitHub B开发分支 `d2f2dd6b0ca1194be98e780d879868e3de7fe062`。
产品沿用节点已验 `989fd71865046bde988603c97416dc133c582e88`，CASE为
`/home/y00993710/urma_rm_ctp_lab/experiments/hucx-b-segmentation-dev/r1_shared_989fd7186_xncBHX`。
模块SHA256为 `7b08a804a02d67fdc965b0055a55459ebf0dc4af198cbe4096ccf91019fb2530`。
本轮只增加接入与验收工具，不修改transport、UCP、HMPI、RC/UM或任何默认开关。

## 这轮纠正的实现判断

HMPI公开参考 `52d943345a85efc10de2d59311eb4bd4fa94444e` 的osc_ucx申请
RMA、AMO32、AMO64。该事实不等于必须先开发RM硬件原子操作。
当前HUCX已有UCP软件原子回退，能够与原生READ/WRITE通道分别选路。
因此此前“MPI单边必须等待硬件原子能力”的判断过强；历史节点结果保持原状，新的接入路径需要节点实测。

源码调用链见 [SOURCE_AUDIT.md](SOURCE_AUDIT.md)。软件原子请求经现有AM到达对端CPU执行，
不改变RM的硬件原子能力声明；不把UCX_ATOMIC_MODE=cpu等同于打开软件网络仿真。
本工具不设置该变量，也不修改UCP选路来强迫通过。

## 有界验证顺序

默认命令只打印计划，不SSH、不编译、不启动MPI。
`--preflight-only`检查双端候选模块、历史同产品检查点、OSU 7.5.2二进制和HMPI组件/依赖，
用节点mpicc编译MPI专项、元数据探针及诊断拦截器。它不启动双节点MPI作业；OSU帮助查询会在单进程内初始化MPI。

`--run`最多两节点各1进程，先后运行：

1. RC的fence窗口数据门禁。
2. RM的BCOPY和ZCOPY两组fence窗口数据门禁；每组两个window代次、两个方向、每rank32轮。
   每种操作按8/65536/64/16385/4096/8193/8/65536B交替，检查整个本地和暴露缓冲区及guard。
3. MPI的FAA32/64、CAS32/64命中与不命中、guard校验；同时要求实际UCX日志出现sw_amo的rkey选路。
   这只接受所测软件AMO正常路径，不宣称完整原子操作矩阵或故障恢复。
4. RM的lock_all/flush/sync窗口数据门禁。
5. 原版osu_put_latency、osu_get_latency、osu_put_bw、osu_get_bw，fence/create，
   8/4096/65536B，RC与RM，共24个诊断任务。每个20次正式迭代、5次预热。

前一门禁失败即停止，保留原始错误、退出、完整日志、双端实际PID、CPU绑定及maps。
不跳过失败继续汇总PASS，也不将诊断轮输出当作性能结果。

显式追加 `--timing` 才在上述门禁全部通过后增加3轮无trace计时，
每轮1000次迭代、100次预热；RC/RM交替顺序，全部原始样本、异常和回退保留。
生成comparison.md供审阅，不自行设置无回退容忍线。本轮不增加高PPN、设备资源上限或重传任务。

## 验收证据与范围

- 显式指定pml=ucx和osc=ucx，核对双端mca_osc_ucx.so及libmpi哈希。
  实际应用的before_finalize maps必须加载相同组件和候选核心/URMA库。
- 同时清除继承的UCX、MPI_UCX、MCA覆盖，并明确设置两套UCX前缀，避免OSC上下文被旧配置改道。
- 诊断专用拦截器关联MPI_Put/Get内部的UCP调用与rkey，要求实际业务rkey解析为basic_rma；OSC初始化状态写入不能冒充业务原生路径。拦截器不载入计时轮。
- 诊断日志按实际应用PID分文件；流式读取全部日志，不因128MiB门槛误拒或截断取证。
- RM普通数据组必须观察到原生PUT和GET；BCOPY/ZCOPY分组分别要求对应实际post。
  原版OSU诊断组也分别要求原生PUT或GET，不凭环境变量认定路径。
- 原版OSU 7.5.2这些单边用例不支持内置validation，工具不传-c；数据正确性由前面的MPI专项提供。
  OSU成功仅记执行、库加载、选定路径及输出完整，不伪报OSU DATA=PASS。
- 一个rank超时180秒（可改60～600），launcher另加20秒；只清理带本轮nonce和精确目录的进程。
  不pkill、不清理其他同事作业、不改设备和系统设置。
- 产品不变，因此读取既有READ6/WRITE6/UCT8/UCP32/28正常的同产品检查点；不再次跑相同回归。
  新MPI功能不能借该检查点冒充已通过。
- 根据双端实际ompi_info的pkglibdir定位组件；动态OSC组件不在所报告目录时停止并保留实际版本/路径信息，不猜静态构建或偷偷更换OSC。

## 执行

将节点ZIP上传B实验根upload。直接运行 `python3 -u ZIP --run`，不解压覆盖CASE、不重编传输库。
先做功能与原生路径门禁；需要正式计时可直接 `--run --timing`，须有两节点空闲窗口。
默认只占两节点各一个rank，但带宽测试仍会使用网络带宽。

当前状态：本地工具检查与探针编译见交付检查记录；真实节点尚未运行，不能承诺一次通过。
通过后再按所列范围整理同一个AtomGit B分支的阶段commit；完整B4/B/C仍不提前勾选。
