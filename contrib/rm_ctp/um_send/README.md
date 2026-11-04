# RM＋CTP SEND：沿用 UM 框架

本分支是新的候选实现。开发基线为 `15ba1a9756bb63d4cfc5e9eacdf5784ee10a616c` 中的 UM，替换旧 RM 后端；RC、UM、公共 MD 代码保持原样。旧 RM 的验收记录不能用于本分支。

## 实现与适配

直接沿用 UM 的 conn_match、CREQ/CREP、EP 状态、发送窗口、累计 ACK、NACK、16 位 PSN 环绕比较、拥塞窗口、重发队列、CQE 与窗口释放关系及定时器。PSN 属于 EP，Jetty 是 iface 共享资源。没有新增共享 peer 协议或可靠性状态机。

RM 相对 UM 的差异可查看 `um_normalized.diff`，已消除命名和模式枚举替换造成的噪声。实际适配包括：

- JFR/JFS 使用 `URMA_TM_RM`；导入远端 Jetty 使用 `URMA_CTP`；JFS priority=1。
- RM 的 SEG_SIZE 默认 4096；仅允许 17..4096。AM_BCOPY 能力为片长减 8，默认 4088；UCP 根据该能力分片并在接收端重组 MPI 消息。
- 每次底层 SEND（含控制包、重发）检查 SGE 总长度不超过 SEG_SIZE。超限与原 UM 提交失败一样走 fatal 路径；本轮未实现额外的硬件故障恢复状态机。
- 仅上报 AM_SHORT/AM_BCOPY；不向 UCP 上报 PUT/GET、AM_ZCOPY、原子和 peer failure 能力。UM 原有软件 PUT、AM_ZCOPY 函数保留为框架代码，不代表 RM 原生 READ/WRITE 可用。
- AM_SHORT 长度计算避免小 inline 上限下的无符号下溢，并受片长限制。

协议头与 UM 相同：packet_type 4B（含目标 EP ID、AM ID/控制标志）、PSN 2B、累计 ACK PSN 2B，共 8B。8B OSU 短消息通常是 8B 传输头＋8B UCP tag/header＋8B 用户数据，即实际 SEND 24B；以逐 WR 观察结果为准。CREQ/CREP 仍有独立的一次性连接信息，不包含在“每个 DATA 8B”里。

默认定时器沿用 UM：TIMER_TICK=10ms、TIMER_BACKOFF=2、TIMEOUT=5m、MAX_WINDOW 上限1025。节点验收为了有界结束显式设置 TIMEOUT=10s、MAX_WINDOW=32、TX_DEPTH=64、RX_DEPTH=512，全部记录在 trial.json。该窗口是 EP 拥塞/发送窗口，不是旧 RM 的 K×W 接收预算公式。原 UM 的失序策略是拒收非连续片、NACK/ACK 和重发，未新增完整乱序缓存。

## 文件职责

| 文件 | 内容 | 来源 |
|---|---|---|
| rm_urma_ep.c/.h | EP 连接、SEND、PSN、窗口、ACK、重传、pending/flush | UM 对应文件 |
| rm_urma_iface.c/.h | 共享 Jetty/JFR、收发 CQ、资源、能力、conn_match、进展和定时器 | UM 对应文件 |
| rm_urma_def.h | 协议内部常量和比较宏 | 与 UM 完全对应 |
| rm_urma_inl.h | 两个小内联函数 | 与 UM 完全对应 |

这里只有两个 C 编译单元，与 UM 一致。旧 addr、link、reliable 的六个文件已移除。本次没有改动公共 MD/rkey，也没有为未来 READ/WRITE 新建占位对象。

## 验证

`run_candidate.py CASE` 默认只显示计划；显式 `--run` 才运行两节点任务。`--timing` 追加独立的原版 OSU 8B RC/RM 五轮轮换计时。所有任务使用同一候选安装中的 RC 和 RM；RM 不使用其他网络辅助传输。保持原版 OMB 7.5.2 与现有 HMPI，强制双端候选基础库，检查实际进程 maps、数据校验、退出与清理。

功能计划共19项：RC 8B门禁，RM 13个尺寸（含4080与8136附近边界、1MiB），PPN4多对及Alltoall，最后一项原版OSU软件丢DATA/丢ACK/重复包注入。探针只在功能轮加载，检查每个实际rank的WR长度、inline、软件注入次数与重复PSN的成功提交；计时轮不加载探针。软件注入点在RX协议入口，不代表链路丢包或硬件故障。

原生 UM/UTP 的节点通路此前未确认，因此默认不把 UM 控制组作为新 RM 的前置条件；`--with-um-control` 可增加 UM 8B 控制组并将计时扩为三模式，此时UM失败会照实停止。代码复用关系由源码差异复核，不依赖UM实际网络可用。

本地可复现检查：`python3 -B contrib/rm_ctp/um_send/test_controller.py`。`state_test.c` 直接包含实际 EP 源码，检查16位环绕、重发区间/ACK推进、拥塞窗口下限和 inline 提交，使用CPU模拟provider，不接触设备。完整x86编译使用上传的公开URMA头文件和仅供链接的stub，不能代替ARM/真实URMA运行。

尚待真实节点结果：新的建链与SEND、重传、分片、规模、失败边界及性能。原方案所有通过记录均只作历史对照。本候选尚未完成完整需求验收。

## 后续 READ/WRITE

本轮暂停。以后在同一 EP/iface 框架内核对并接入公共 MD 的注册/rkey，目标 Jetty、权限、RMA 提交、CQE、完成/flush 和缓冲区生命周期，分别验证 PUT/GET。SEND 的 PSN 只覆盖本协议可见的 SEND 片；不能直接证明底层一次原生 READ/WRITE 的内部切片和重试语义。是否原生实现或由 AM 模拟，须另行定方案，不在本轮打开能力位。
