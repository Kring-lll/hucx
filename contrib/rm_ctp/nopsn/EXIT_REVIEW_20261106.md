# PPN8退出失败审查与修正候选

## 节点证据

产品370bbc3/module5b85f108；结果nopsn_accept__ub8d93v。前16项通过，第17项为16进程环形大小交替。
日志先由rank0打印旧NOPSN_ALTERNATING_PASS，再出现多个rank的rm_urma_ep.c:1035 Fatal。
调用栈明确处于MPI_Finalize → PML删除进程/连接 → ucp_worker_progress → pending → resend。
直接退出原因为实验副本的DATA重传保护。旧标记在Finalize之前，不能据此认定所有rank已正常退出。
日志没有记录当时窗口头长度/本地序号，不能只凭它判定硬件丢包，也不能证明下面的修正已解决全部节点根因。

## 已复现的代码缺陷与修正

1. DATA保护位于原UM的重传跳过条件之前。原实现可能因窗口界限、大包无NACK、已在重发或本地flush标记而不发送该记录；旧副本看到DATA就先Fatal。真实函数体CPU测试复现5类过早中止。新保护放在所有原跳过判断之后、分配重传缓冲与改写状态之前，真正DATA重传依旧Fatal；窗口比较使用DATA本地键，不误读6B头里的ACK为PSN；大包阈值按实际头长调整，保持与原UM相同的payload门槛。
2. 新增完整EP flush测试由UBSan抓到指针完成描述符未8B对齐、栈ACK对象未满足send_skb的64B对齐要求。新增一个本地skb_trailer对齐帮助函数，供completion/control/zcopy元数据的4个访问点复用；ACK栈对象预留对齐余量后按64B对齐。只修改本地内存位置，不增加线上报头、复制、协议状态或新产品文件。
3. 正确性程序原PASS过早。现在每rank记录DATA_DONE；rank0仅在MPI_Finalize成功返回后打印PASS。解析器要求16个不同rank数据完成标记、唯一PASS，原rank identity/maps/全进程正常退出检查仍保留。

DATA头仍6B，ACK与建链头仍8B，ACK序号/窗口保留，单SEND≤4096。未改重传定时器、超时、RX深度、窗口和绑核，未删DATA安全保护。RC、UM、公共URMA产品不改。

## 本地验证及限度

- x86全量编译链接通过，SDK仅为本地链接桩，不是硬件证明。
- 真实EP函数ASan/UBSan通过：70000循环回绕、大小交替、异步收取/延迟释放、8B ACK、4096个长度的trailer对齐、真实大包选择跳过、独立ACK处理、发送窗口释放、flush回调及真正DATA重发拒绝。fixture仍非真实网络，多进程定时器行为仍待节点。
- 真实resend函数体12分支检查通过；旧代码误中止已复现。外部资源调用为桩，不夸称整套UCX验收。
- 15项工具CPU测试通过，含真实213个one_trial配置与原解析器、6项退出诊断调度、缺失/重复rank标记拒绝。交替程序文件API harness验证2/4进程及内容损坏/Finalize返回错误拒绝；不是MPI实现或URMA证据。

## 下一次节点仅做退出专项

沿新case隔离ARM重建，不覆盖370bbc3或旧失败目录。
同一新交替程序对固定完整RM a4d库与修正副本，各PPN8/16rank/260轮，最多各3次（总6项），轮换顺序；RX4096/TX64/window32/timeout10s不变。
仅正确性/正常Finalize/资源释放和库身份，零性能轮、零资源测量轮、无线上观察器和故障注入。任一失败立即停止。
若仍触发真正DATA保护，新的Fatal包含EP、目标、len/local_psn/acked/next/max/rx_head/flags/pending，继续定位ACK/进度，不把超时拉长或删除保护来刷通过。
本候选也修正了本地内存对齐，故本轮完整库对照只用于正确性诊断，不能拿它宣称纯PSN性能成本；后续性能归因须使用匹配的本地实现与明确差异。
本文件不构成节点通过、完整性能或同等可靠性验收。
