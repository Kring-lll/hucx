# RM SEND 保序与重传：阶段实现

起点：GitHub B 开发分支 d93900c3d2ed25cf8b0d8eae485704b96715fe4c。
这是新方向的过程版本。旧版本的 ARM、OSU 和性能结果不属于本候选的节点验收证据。

## 本轮改动

新增显式 `UCX_RM_URMA_LINK_MODE=reliable_v3`；默认仍为 legacy_ep，iface_v2 继续保留。
只处理 AM SHORT/BCOPY 经 SEND 传输的可靠性。新模式拒绝同时开启 NATIVE_WRITE、NATIVE_READ、NATIVE_RMA；不开放 AM_ZCOPY、原子或 MPI 单边。
RC、UM、共用 MD 和共用 RX 实现没有改动。

参考 UM 的发送窗口、累计确认、接收连续序号和超时重发原则，适配 RM 的共享 Jetty、目标复用和无反向业务 EP。
没有复制 UM 的整个连接/定时器实现，也没有照搬 BRPC 的 RPC 重试。

- 一个本地 iface 到一个远端 iface 是一条有序流。多个业务 EP 共用 PSN 与窗口；不同远端分别计数。
- PSN 为 64 位，从 1 开始；本轮不支持回绕，耗尽后明确报错。地址 incarnation 区分不同 iface 生命周期。
- 只有预期 PSN 才交付给 UCP。重复和超前 DATA 均丢弃，随后回复最新累计 ACK。没有乱序缓存，缺口通过重传补齐。
- 每条已接受 DATA 保留已注册的副本。远端 ACK 与最后一次本地 CQE 都到达后才释放。
- DATA 和 ACK 复用一个 iface 控制完成 owner，不增加本地硬件 Jetty。接收方无需建立反向业务 EP。
- 累计 ACK 同时归还发送窗口。接收 WQE 补回之前不发 ACK；ACK 丢失后，DATA 重发触发再次确认。
- 重传不再次扣窗口。窗口满、SQ/CQ 不足时返回 NO_RESOURCE，交给现有 pending；不消耗新的 PSN。
- 默认重发间隔 100ms、最多 256 次、未完成操作绝对期限 30s，最先达到的终止条件生效。计时依赖 worker 持续 progress，没有睡眠线程或异步唤醒定时器；新模式不声明 event/wakeup 能力。
- 终态 provider 提交错误/CQE 错误直接进入共享 iface 失败路径，不对故障 Jetty 盲目重试。提交状态不确定的缓冲隔离到 Jetty 删除之后。
- 销毁逻辑 EP 不挖掉已经接受的流序号：数据继续完成协议，EP 延迟回收。flush/CANCEL 等待已经接受的数据及本地 DMA，不承诺撤回已交付消息。

## 报文和资源

外层沿用 3B UB AM 头，内层公共头 36B。DATA 加 68B 返回地址，总软件头为 107B；ACK 总长 39B。
公共头包含 magic/version/kind/AM-ID、源和目标 incarnation、PSN 或累计 ACK、payload 长度。
每个 DATA 都携带返回地址，第一条 DATA 丢失不会破坏无反向业务 EP 的引导。

本轮 reliable_v3 只接受固定 4KB SEG_SIZE，拒绝其他片长，避免尚无MTU协商时两端配置不匹配。旧模式的显式片长覆盖不变。
4KB SEND 上限下 `AM.max_bcopy=3989B`；AM_SHORT 的 8B 用户头计入这个 payload。
UCP 必须按新上报能力分片，原 4085/8146 等边界不直接沿用到可靠模式。
接收回调使用 flags=0，要求立即消费/复制；不把带新头偏移的缓冲误报成可保留的 DESC。

沿用地址中的静态 K/W/R 约束：K 是整个 iface 生命周期中不同远端 iface 的总上界，W 是每个远端可用发送窗口，R 是已发布接收深度；需要 `R >= K*W+2*K+2`。
发送保留条目最多是各远端公布窗口之和，每个远端最多一个 ACK 在途；还保留一个 SQ/CQ 槽供控制消息。
默认配置不自动猜 K；各参与方必须按实际作业配置一致的边界。动态成员、重启重连和恶意报文不在本轮范围。
本轮不添加校验和，不替代平台对成功 SEND 接收、数据完整性、注册和完成语义的底层契约；真实设备异常仍待单独核对和测试。

## 本地检查与待验边界

`run_cpu.sh BUILD_DIR URMA_INCLUDE` 运行既有实际 RM 源码 mock 与本轮五组可靠性用例。
新可靠性实现、EP、iface 源码直接参与 ASan/UBSan 检查，分别开启和关闭断言及参数检查。
完整 x86 构建使用真实头文件和 URMA 链接桩，仅证明编译/链接，不证明设备操作可用。

`run_candidate.py CASE` 默认只打印计划；`--run` 才启动节点任务：

1. 候选双端部署和模块身份检查；旧模式 legacy_ep/iface_v2/RC 的既有 28 项正常回归。
2. reliable_v3 正常直接 UCT 生命周期：四个逻辑 EP、无反向业务 EP、512 条 64B 数据、SHORT/BCOPY、顺序、跨窗口、160 次 EP 创建/销毁、32 次在途销毁和 pending 取消/重提。
3. 同一程序开启独立接收注入器：首次 PSN3 DATA 丢弃、前两次 ACK 丢弃、PSN2 重复一次。注入发生在成功硬件接收之后、可靠性译码之前，必须实际触发，否则本轮失败。

每端仅一个直接 UCT 测试进程；既有回归最高 4 个远端 rank。不会跑满核或启动 PPN192。
每个新任务各建目录，保留命令、环境、双端日志、maps、模块和测试二进制哈希。
只终止本轮进程组；远端本轮任务有独立 timeout，不使用节点级 pkill。

首轮正常/软件注入已通过，见[首轮记录](acceptance_20261103.md)。后续可靠模式UCP/MPI所选40项集成已通过，
包含大消息、新边界、AM切换及严格大小交替，见[集成记录](../send_integration/acceptance_20261103.md)。
正式OSU性能、同规模资源对比、多线程、高规模、真实provider/硬件丢包和设备故障恢复仍未验收。
当前只交付明确列出的阶段范围，清单完整C/D保持未完成，默认模式不变。
