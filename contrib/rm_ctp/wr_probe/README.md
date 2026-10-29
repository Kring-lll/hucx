# RM SEND长度与正常分片边界验证

## 当前结果

- PPN1五档逐SEND专项已通过，见[WR_NODE_20261029.md](WR_NODE_20261029.md)。
- PPN4两种原版OSU的16项RM边界校验和两项RC 8B门禁全部通过，见[WR_BOUNDARY_NODE_20261029.md](WR_BOUNDARY_NODE_20261029.md)。
- 产品仍为2a8e，观察器不修改WR、返回值或业务payload，原版OSU算法不变。
- 本轮代码与已运行节点包一致，记录更新不要求重新运行节点。

## 功能

默认无参数只输出计划，无SSH、编译或MPI任务。
`--boundary-matrix --run`选择两节点PPN4、multi_lat/alltoall、
8/4084/4085/4086/8145/8146/8147/16384B，共18个任务；每项100次校验、10次预热、120秒rank时限。
原PPN1五档入口兼容，支持`--ppn 1`或`--ppn 4`、`--sizes`和`--benchmarks`选择小矩阵；
`--boundary-matrix`优先使用固定配置。后续运行前先确认是否需要新证据，避免重复已过矩阵。

双端核对4K配置、UCT AM bcopy 4093、产品/OSU/HMPI库身份、绑核、实际应用PID和nonce。
全部rank检查Init后及Finalize前映射、SEND元数据摘要；peer摘要单SSH取回并保存SHA256收据。
观察器按RM SEND的SGE长度之和统计直方图，聚合字节数/SGE数/inline标志；不读取payload。
任何缺失、错误身份、转发失败、记录不完整或SEND大于4096B均停止，不给通过结论。
运行和清理沿用现有OSU工具的本轮nonce、目录及进程起始时间边界，无全局pkill或设备重置。

## 使用位置

新电脑、中转和节点路径见[WORKSPACE_PATHS.md](../WORKSPACE_PATHS.md)。
节点内包为新B上传目录中的`hucx_b_eager_boundaries_20261029.zip`，SHA256：
`f87e15d4478b872308c09c3b7d3b98edf439d1deb00dfdcb38b88573c3e50bcc`。
产品安装和本轮结果继续保留在原已测CASE，不搬迁或覆盖。

## 历史修复与限制

旧版观察器首项8B曾因RTLD_NEXT找不到局部加载URMA的符号退出127，没有产生SEND长度证据。
修正版先查RTLD_NEXT，失败后只从已加载liburma取得RTLD_NOLOAD句柄，不额外加载第二份库。
双端MPI前执行局部加载模拟门禁，随后真实节点专项已通过。
本地16项WR/矩阵检查和既有OSU23项回归通过；PowerShell脚本未在开发环境实际执行。

总WR含流控、建链及集合通信，不等于单条MPI消息片数；post成功不是SEND CQE或交付契约。
WR不是网络物理包。OSU数据校验与长度观察只证明本轮正常路径，不证明硬件极限、去重、保序或故障恢复。
本工具有观察开销，不用于性能表；C重传延期。协议和条件化边界见[B2审查](../B2_EAGER_REUSE_20261029.md)。
