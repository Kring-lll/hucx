# 标准OSU 7.5.2 新版RM与RC对照（2026-10-27）

导师要求正式性能使用OSU；原A8/A9自编用例只作功能、资源、扩展性辅助取证。
A9已由用户退出，暂不恢复。本包产品src仍是节点已测2a8e，不修改OSU通信代码或原二进制。
标准OSU两批PPN1/8/16/20已通过，见acceptance_standard_20261027.md；下面原工具准备/预检失败段落为历史记录。
仅RM扩展的新入口及其最高通过规模仍待节点验收。

## 2026-10-27 新入口：先合表，再仅RM扩展

产品、原OSU二进制、已实测的run.py/analysis.py/identity.c/osu_rank.py及A89依赖均不改。
新增baselines.py复核两批原始证据，scale_rm.py只调度mode=rm，旧对照入口的PPN20保护保留。

上传新包`hucx_osu_rm_scale_20261027.zip`。只复核并合并已通过的两批表格（无SSH/编译/新MPI）：

```bash
python3 -u /home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/upload/hucx_osu_rm_scale_20261027.zip --rm-scale --merge-only
```

该命令固定复核osu_compare_87i9g4lw（PPN1）和osu_compare_697v3h8j（PPN8/16/20）的results/trial/config、完整OSU输出、实际rank/maps、Jetty及退出；原文件不改。
合表保留各批来源和原始样本范围，不能把两批当成同一批重复采样。

用户协调节点空闲窗口后，执行仅RM扩展：

```bash
python3 -u /home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/upload/hucx_osu_rm_scale_20261027.zip --rm-scale --run
```

此命令会使用高规模资源，计划PPN32/48/64/96/128/160/192；每档两节点各PPN个进程，最多共384rank。
两端CPU许可再次探测，按较小物理核数裁剪，不超配，不表示节点空闲。
每个用例依次独立校验、5次正式计时、1次资源轮，固定8B/5000迭代/1000预热，验证轮1000/100。
默认每任务600秒（参数--timeout），只清理本轮nonce/准确目录/starttime匹配的进程；单档失败停止，不重试掩盖错误。
`--max-ppn 64`可明确限制规模；`--rm-scale --plan`不SSH/编译/MPI。

标准OSU当前共同最高已通过PPN20。扩展运行前，还要求两批历史与当前的boot/CPU许可、库/OSU/HMPI/观察器/保存启动配置一致，改变时停止，不静默沿用旧可比基线。
各用例分别更新最高完整通过PPN；共同最高仅在该档两个用例全部14个任务通过后更新。
某个用例失败不抹掉另一个已完成的结果，也不把工具/环境失败称为RM硬件上限。
高规模RC一律不启动，表中RC/相对收益填XX（本轮未做同规模RC），导师给出的PPN24不是本轮新增实测。

输出完整comparison.md/CSV、results.json及每轮原始证据；表头口径与原对照一致，统计不用资源观察轮的时间/RSS。
即使192全部通过，也只说明这两个用例在该配置下已验证至PPN192，不证明协议最大值或所有MPI场景。
本次新增7项CPU检查（含合表失败留档），旧16项合计23项；CPU替身/文件契约不是真实节点扩展证明。

## 本轮范围

- 原版`osu_multi_lat`和`osu_alltoall`，固定8B，默认两节点PPN1/8/16/20。
- 每个用例、规模、模式：一轮`--validation`校验、五轮不加validation的正式计时、一轮不加validation但加载URMA资源观察器的资源轮。
- 默认112个MPI任务，逐个串行；最多每节点20进程，共40rank，不是192满核任务。
- 两模式同一HMPI、同一已测UCX安装、同样大小/迭代/预热/窗口/绑核/集合组件；重复轮RC/RM交错。
- 主体OSU程序不改；可执行文件的双节点SHA、版本、完整帮助、依赖与每轮原始输出保存。
- 不在本包开启大消息、RMA/atomic、GPU、多线程或C重传，不把本包通过当作A/B/C全部完成。

## 性能与资源口径

1. multi_lat取OSU输出的多对平均单向时延；alltoall取OSU输出的集合平均耗时，另保留`--full`的各rank最小/最大值与迭代数。
2. 只将五轮正式计时的中位数填入性能表，完整原始值、最小/最大、负收益、失败都保留。
3. Jetty按对应模式RM=1或RC=2记录，输出每进程同时存活峰值的全rank最小/最大范围，并保留各context峰值、深度64及退出live0。UM辅助Jetty不混入目标计数。
4. RSS从没有URMA观察器的正式计时轮取值：各进程进入MPI_Finalize前RSS之和，再取多轮中位数，单位MiB。各rank采样不是同步瞬时总量；含MPI/UCX/分配器等，不是Jetty或设备独占内存。
5. 小型`identity.c` PMPI取证库只包装MPI_Init/Init_thread/Finalize，在OSU计时循环外保存真实应用PID、maps、RSS及退出状态；没有SEND/RECV/collective/MPI_Wtime包装，不替代OSU性能计时。
6. multi_lat源码每轮还有MPI_Barrier，故不能预测其Jetty等于“一对进程一条连接”；不复用原自编A8的2/16/32/40计数或2.84/2.57等交换耗时。
7. 固定PML=ucx、集合组件tuned/basic/libnbc/self，关闭tuned动态规则，保持相同HMPI默认固定规则。没有猜测数值型算法ID；保存ompi_info参数证据。结果代表此配置，不代表所有集合算法。

## 使用方式

上传`hucx_osu_compare_collfix_20261027.zip`到实验根`upload`。

默认只预检，不启动双节点性能任务：

```bash
python3 -u /home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/upload/hucx_osu_compare_collfix_20261027.zip --preflight-only
```

预检会核对双端候选库/OSU二进制、完整`-h/-v`和参数、HMPI依赖/CPU许可/观察器，
用已有mpicc编译小型取证库；不重编译传输库或OSU。帮助命令只运行本地单进程MPI初始化，PML用ob1。
成功后显示`PREFLIGHT_PASS_NOT_MEASURED`，不能将它当作性能或双节点收发通过。

在两节点无其他通信/重负载任务的窗口正式执行：

```bash
python3 -u /home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/upload/hucx_osu_compare_collfix_20261027.zip --run
```

可先追加`--ppn 1`只跑两节点各1进程；其结果仅该规模，不冒充完整PPN20矩阵。
只查看预算可用`--plan`，无SSH/编译/MPI。
默认计时迭代5000、预热1000、每任务180秒；`--ppn`最多20，禁止本脚本误冲击高规模RC。
节点CPU许可不等于空闲核数；本工具不能自动判断同事是否正在使用网络或CPU，须协调窗口。

## 退出与证据

- 每次创建CASE下新`osu_compare_*`结果目录，两端唯一；不覆盖已测库、OSU或旧结果。
- 超时只停止本轮launcher组，并按随机nonce、准确结果目录环境变量及进程启动时刻清理本轮rank/timeout。
  OSU没有A89自编用例的结果目录位置参数，因此OSU包装器使用独立的严格环境匹配，不依赖命令行子串；无全局pkill/设备复位。
- 复用A89单SSH压缩流取证及逐文件SHA，增加明确`osu`文件约定，不伪造旧三阶段maps文件。
- 每轮核对实际主机/rank/CPU绑定、两阶段候选UCX与HMPI映射；资源轮再核对应用PID/Jetty/退出。
- 一轮失败即停止扩大，写`STOPPED_INCOMPLETE`；已完成组可见，但不能标成完整通过。
- 输出`results.json`、`comparison.md`、UTF-8 BOM的`comparison.csv`。两个OSU用例分表；RC/RM原始日志、配置、完整异常和所有样本都保留。

## 本地检查与限制

OSU专用16个CPU检查通过：官方格式解析/失败拒绝、CSV/Markdown、真实C身份探针动态加载、
PMPI两个启动API、真实PID/maps/RSS、12种用例×模式×阶段的文件流程、正确资源schema及nonce清理。
其中使用显式CPU MPI/API和Jetty事件替身，不是真实MPI/UCX/URMA硬件或性能证明。
既有A89 34项检查中33通过、1因Work环境PID虚拟化跳过；C测试主体PPN1/4文件API与故意损坏数据检查仍通过。
身份探针真实HMPI编译、PowerShell执行与完整矩阵仍待用户节点/新电脑输出，不能承诺一次通过。

官方来源与核对结论见[SOURCES.md](SOURCES.md)。GitHub仅过程提交，待实际OSU结果审阅后再按方向分支约定整理AtomGit交付。

## 2026-10-27 首次预检组件遗漏修复

节点`osu_compare_1u7mztv9`中，OSU在MPI_Init阶段报告coll初始化/销毁失败，日志明确提示basic、libnbc、self。
原工具白名单`tuned,basic,self`漏掉libnbc；仅测试阻塞集合并不表示可以缺少通信器初始化所需的非阻塞集合支持。
当前统一补成`tuned,basic,libnbc,self`，用于本地帮助预检和正式RM/RC测试，其他测试口径不变。
在OSU-h前先通过两端ompi_info可解析输出检查四个必需组件；完整组件输出、帮助/版本的实际环境、命令和退出码另存JSON/TXT。
本地新增3项检查拒绝旧缺失配置并覆盖双端检查和失败诊断留档；CPU共16项通过。真实HMPI预检和正式矩阵仍待节点，不能从CPU解析测试推导硬件通过。
此次未改产品库、OSU二进制或身份探针，没有开始A9；初次失败记录保留。
