# 可靠 SEND 正常路径成本诊断

目标是解释已恢复 inline 后，可靠 RM 相对 iface_v2、RC 的额外开销。
不修改已经验收的 a2b 源码、安装库、OSU 二进制或历史结果。

节点 ZIP 默认只打印计划；`--preflight-only` 只核对本地产品、检查点和原源码包，
不构建、不 SSH、不启动 MPI。`--run` 在新目录从固定 a2b 源码包生成诊断源码、
隔离构建、复制安装至 node97，再按固定顺序运行 54 个双节点 MPI 任务。
最高 PPN4，每个消息 8B。构建日志和失败原始证据保留。

原产品 a2b4b07c12be5401afaf427877aadd6752871810；模块
2543923f6b2cae2a37365d293e715bc63fefa539ecc8b146880b6270b0351aa0。
固定 CASE：
`/home/y00993710/urma_rm_ctp_lab/experiments/hucx-c-send-reliability-dev/r1_shared_a2b4b07c1_Z1tIOO`。
复用已上传的 `upload/hucx-c-send-inline-20261103-source.tar.gz`，原 SHA256
f0d11b1066f735cfa23ceaa94c2700a9187f674dfe15d5d02fb0e9a5dcf38bae。
工具会核对完整归档哈希、Git commit、五个插桩文件的原哈希和既有验收结果哈希。

## 测什么

两个负载：原版 OSU7.5.2 `osu_multi_lat PPN1` 和 `osu_alltoall PPN4`。
每个负载均测同一安装里的 rc、iface_v2、reliable_v3 三种模式。

| 安装与阶段 | 每模式任务 | 用途 |
| --- | ---: | --- |
| 原版库数据校验 | 1 | 校验数据、实际进程 maps、RM WR/inline、退出 |
| 原版库独立计时 | 5 | 同一批正常性能；轮换三模式顺序，保留全部样本 |
| 诊断库开启采集并校验 | 1 | 验证诊断构建可用、实际运行身份及原生 profiling 文件 |
| 诊断库关闭采集 | 1 | 观察临时编译变化、弱符号分支、边界探针的扰动 |
| 诊断库开启采集 | 1 | 采集代码段次数/耗时、接收批量与扫描等事件 |

合计 2×3×9=54。关闭/开启采集各一轮仅作扰动观察，不能当作精确或稳定的净成本。
诊断库与原版库不同，模块哈希独立记录；它没有获得产品验收。

## 测量方式及边界

复用 UCX 自带 `UCS_PROFILE_CODE_ALWAYS`，只在临时源码副本增加选定代码段。
保持普通 profiling 编译选项关闭，避免开启其他大量 UCX 打点。
`UCX_PROFILE_MODE=accum` 只在诊断采集进程使用，不依赖 perf、PMU 或内核修改。
独立 MPI 边界 DSO 在 Init 完成后打开统计、进入 Finalize 前关闭，
不包装 MPI 数据操作、不替换 MPI_Wtime。包含预热、OSU 控制通信及基准操作。
所有真实应用 PID 的 maps、结束状态、日志、独立 nonce 都按既有流程核对；
UCX 二进制累积文件还核对格式版本、PID、hostname、实际 libucs 安装路径及完整长度。

时间为包含子代码段的 elapsed 时间：包含插桩及线程被调度出去的时间，
不是纯 CPU 时间，不能把父子 scope 相加，也不能直接分摊未插桩的 OSU 时延。
保留空 scope 的均值以展示计时成本下限；不机械减去空 scope，不输出伪精确成本。
累计调用次数与事件计数也会受插桩改变 ACK 合并、批处理的影响。
RX 统计来自公共 UB 接收提交路径，可能包含辅助 UM 流量，不是纯 RM 数据。

重点观察：

- DATA/ACK 元数据及 TX 缓冲分配回收、重放记录释放；
- DATA 头部构造、短消息副本、地址解码、peer 命中后重复编码/比较；
- 可靠收包、上层交付、可靠 progress、本地 TX 完成处理；
- ACK 接收、DATA 接收/交付/丢弃、peer 和 replay 遍历次数；
- 接收 post 调用数、成功补充缓冲总数和批量分布；
- 资源检查失败和实际软件重传次数；
- 各进程用户/内核 CPU 用时及上下文切换次数。

不修改 ACK 策略、PSN、报文头、窗口、CQE 回收语义、100ms RTO 或重试限额。
不运行暂停的 READ/WRITE、MPI RMA、PPN192，不宣称完整 C/D、硬件故障或优化验收。
本地 CPU 检查不能替代 ARM 整库编译和两节点实测。
