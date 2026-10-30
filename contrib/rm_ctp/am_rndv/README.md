# B4 AM rendezvous正常大消息工作包

开发起点：GitHub `9e1a3a6b5ed4074ac162258397065fd0f54831e0`。
固定节点产品：`80b28a25b5ea0e245bca88db44be79d15d06127f`；module SHA256：
`8d9ed4133c71c4ca77ad900e60433e9781cfef2adf33dc396f3f642b2cc9821c`。
本包新增标准OSU自动化和请求路径取证，产品源码、原版OSU和既有运行器不变；ARM节点验收待执行。

## 2026-10-30 节点执行状态

本包已在80b产品执行：前4项通过，第5项16385B在接收端发送RTR时触发RM pending顺序断言，后续未执行。
原始失败目录`b_am_rndv_30dr4_kh`保留；整体状态为未通过。
[修复候选与重新构建入口](../pending_order/README.md)单独准备；新产品不能继续由这个固定旧安装ZIP验收。

## c7ef候选的日志隔离补验

c7ef02b5产品已通过28正常、18边界、窗口10项和AM前9项。最后PPN4/65536B的MPI退出0，原始OSU行是`65536 1187.29 Pass`；合并REQ日志还产生数字开头的残片，被严格尺寸行解析器误认。
此数值来自诊断/校验运行，不作为性能对照。该最后任务的数据行可见Pass，但maps、SEND及完整协议证据尚需补验。

修正仅在contrib：`trial_profile`可将UCX日志输出到本任务目录的`ucx_%h_%p.log`；主矩阵与pending候选入口均启用。
标准OSU stdout和尺寸/列数/Pass/重复行检查保持原样；按实际应用host/PID读取所有进程的完整UCX日志，逐文件保留哈希、检查ERROR/FATAL，再做原请求路径取证。
只有实际应用PID的日志可以作为证据，wrapper/ucx_info的日志不替代它。对端只取已知文件、限制大小并校验传输哈希，不改原OSU、产品或驱动。

`recheck_last.py --run`固定使用c7ef已构建CASE，先检查原始28/18、15计时及19项矩阵记录，再仅启动一次PPN4/64KB多对任务。
保持20迭代/2预热、AM阈值16KB、TX/RX/FC预算及240秒超时不变；完整数据/退出/绑核/maps/SEND<=4096及8rank协议证据须通过。
旧结果目录不写，新结果保存为`am_log_recheck_*`。新入口默认仅计划、无SSH或MPI；不重编译产品，不重跑前面通过的任务。
生成节点包：`python3 build_recheck_zip.py OUTPUT.zip`。本地解析/证据反例和实际CPU/TCP日志隔离通过，不等于ARM/URMA节点通过。

## 本轮形成的结果

区分“不断发送eager片段”和“先经RTS/RTR协商，再通过AM发送数据”的两条现有UCP路径。
不把配置`RNDV_SCHEME=am`本身当作路径证明：先核验RM不暴露原生PUT/GET和AM_ZCOPY，再关联真实发送进程、UCP请求指针、业务长度及实际协议日志。
切换参数只注入本次测试环境，不修改产品默认阈值。

| 模式 | 原版OSU7.5.2 | PPN | 业务长度/B | 预期路径 | 任务数 |
| --- | --- | ---: | --- | --- | ---: |
| RC/RM门禁 | osu_bw | 1 | 8 | 保留既有小包通路，不取证其协议切换 | 2 |
| RM阈值前后 | osu_bw | 1 | 16383、16384、16385 | eager、AM rendezvous、AM rendezvous | 3 |
| RM同尺寸路径对照 | osu_bw | 1 | 65536 | 阈值inf的eager、阈值16384的AM rendezvous | 2 |
| RM大消息 | osu_bw | 1 | 1048576 | AM rendezvous | 1 |
| RM双向窗口 | osu_bibw | 1 | 65536 | AM rendezvous | 1 |
| RM多对通信 | osu_multi_lat | 4 | 65536 | 各实际发送rank均观察AM rendezvous | 1 |

共10任务，数据校验开启，20次迭代/2次预热；带宽用例窗口8，使用multiple独立缓冲。
最高PPN4，消息最大1MiB；仅该尺寸与用例组合待验，不宣称任意MPI场景支持1MiB。
集合算法可能聚合或拆分MPI内部请求，本轮不把集合每对业务尺寸当作实际UCP请求长度；集合大消息对照留给B5。

## 取证与失败处理

- 复用既有产品/模块/原版OSU/HMPI哈希、配置、真实PID、绑核、两阶段maps、Finalize及nonce限定清理。
- RM软件默认片长4KB，无SEG_SIZE环境覆盖；逐SEND观察器仍只记录元数据，不读取业务内容，不改WR或provider。
- 使用现有`UCX_LOG_LEVEL=req`日志，通过实际host/PID、当前请求指针和长度关联`select tag request`与`using rndv_data protocol`。指针复用后以最近一次选择为准，不把其他进程或小控制请求当作大消息证据。
- RM每任务全部rank均要求SEND记录完整、提交成功、长度不超过4096B；post成功不等于远端交付，业务正确性由独立OSU数据校验判定。
- 诊断级别日志中的正常内存分配回退不误判为数据错误；UCX ERROR/FATAL、OSU校验错误和进程异常仍立即失败，完整日志不删除。
- 数据通过但协议日志不足，也停止并单独保留错误。这表示协议证据不足，不能直接推断数据通路或硬件失败。
- 默认每任务240秒，允许60至600秒。只清理本轮launcher/nonce进程，不全局pkill、不重置设备。

## 范围

本次不恢复原生READ/WRITE或AM_ZCOPY，不新增rkey格式、片段头、PSN或重传协议。
UCP已有RTS/RTR、请求ID、数据offset、pending及完成处理继续复用。该软件rendezvous仍以SEND传数据，不等于原生RDMA。
严格大小反复交替、丢失/重复/重排恢复及完整B4未验收；C继续延期，PPN192暂停。
日志、校验和SEND观察器开启，原始OSU数值保留但不作性能优势结论。没有新增Jetty资源观察，不宣称完整资源泄漏检查。

## 操作入口

`python3 build_zip.py OUTPUT.zip`生成确定性节点工具包。
直接运行ZIP默认只输出计划，无SSH/编译/MPI；`--preflight-only`检查双端依赖和能力，`--run`明确启动节点矩阵。
固定使用B实验根的`r1_shared_80b28a25b_dvKqlu`，不覆盖安装，不需要重新编译产品。
失败回传`B_AM_RNDV_STOPPED`和`RESULTS_JSON`，不修改原始结果以绕过门禁。

CPU检查可选入口`cpu_ucp_smoke.py`只用已有CPU构建和TCP，显式关闭TCP PUT以匹配RM无原生RMA能力；不是正式OSU或ARM/URMA验收。
