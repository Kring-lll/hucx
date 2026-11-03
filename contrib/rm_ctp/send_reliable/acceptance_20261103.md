# reliable_v3 首轮真实节点补核（2026-11-03）

固定源码 `97b50328718c3c7c1b613c2ee09a90c198666142`，tree
`09a0052466236c742a50b054be7fbfc088ddc8ff`。节点 CASE 为
`r1_shared_97b503287_f1PZta`，模块 SHA256 为
`3245bcca9bb5319230ed1d3e153191501fba4602a1529f405944992beb6eeec7`。

用户返回最终只读补核：源码/模块及旧正常28项身份门禁 PASS；normal 与 injection
两轮双端 EXIT=0。软件注入中发送端丢弃 ACK 2 次，接收端丢弃 DATA 1 次、重复
交给生产译码 1 次；生产代码实际记录 15 次重传 post。15 是重传提交次数，不是
15 个丢包。

本轮新模式范围只有四业务 EP、无反向业务 EP、每轮 512 条 64B SHORT/BCOPY、
160 次 EP 创建销毁、32 次在途销毁及 pending。旧 `legacy_ep/iface_v2/RC` 的
28 项只是历史正常回归，不是可靠新模式 28 项。

结论：`FOCUSED_SEND_ACCEPTANCE=PASS`。软件注入位于成功硬件接收之后、生产可靠
译码之前，因此证明软件保序/去重/重传链路被实际触发，但不证明硬件网络丢包恢复。
完整 C/D、可靠模式 UCP/MPI 多片大消息、正式性能/资源仍待验。
