# B4 AM rendezvous正常大消息工作包

开发起点：GitHub `9e1a3a6b5ed4074ac162258397065fd0f54831e0`。
固定节点产品：`80b28a25b5ea0e245bca88db44be79d15d06127f`；module SHA256：
`8d9ed4133c71c4ca77ad900e60433e9781cfef2adf33dc396f3f642b2cc9821c`。
本包新增标准OSU自动化和请求路径取证，产品源码、原版OSU和既有运行器不变；ARM节点验收待执行。

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
