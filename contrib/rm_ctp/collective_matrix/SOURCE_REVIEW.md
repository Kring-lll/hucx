# 本轮源码审查

## OSU 7.5.2

原始来源：https://mvapich.cse.ohio-state.edu/benchmarks/ 。本地复用官方7.5.2归档，
SHA256 `618de3d0b1122f73a9229177d2da1e5cd62e431190580cb915f2605849cbbbdc`。
只阅读，不修改或替换节点原版二进制。

- `c/mpi/pt2pt/standard/osu_multi_lat.c:multi_latency`：尺寸循环乘message_size_incr，不支持任意大小来回序列。
- `c/mpi/collective/blocking/osu_allgather.c:main`：同一communicator内递增尺寸，MPI_Allgather后做validate_data；每rank贡献size，接收缓冲为size×numprocs。
- `c/mpi/collective/blocking/osu_alltoall.c:main`：按每目标size分配收发缓冲，循环内校验；不把总缓冲等同于单条UCT发送。
- `c/util/osu_util.c:set_message_size`和set_message_size_incr：支持[MIN:]MAX[:倍率]，不是文件/列表序列；倍率至少2。
- `c/util/osu_util_mpi.c:print_stats_validate`与通用选项：full输出含平均/最小/最大/迭代数和验证结果。Allgather与Alltoall使用同一full解析契约，预检仍要求真实帮助支持。

因此本工作包推进B5集合正常场景；严格反复交替在现有正式OSU边界下没有直接入口，明确保留待补，不将重启进程或递增扫描改名为交替。

## HUCX、HMPI与URMA

- UCP `tag_send.c`的阈值选择及AM rendezvous路径继续复用；已在pending_order阶段取证点对点请求，集合内部请求不能按业务贡献长度硬套该检查器。
- HMPI选择`pml=ucx`、`coll=tuned,basic,libnbc,self`和已测固定MCA；复用读取ompi_info，不强塞未经核对的算法编号。
- UCT RM AM short/bcopy、pending group与公共额度保持节点c7ef代码不变；RC/UM也不修改。本轮用实际新场景回归现有能力，不为工具增加transport功能。
- URMA逐SEND元数据观察复用已验证的局部加载符号转发探针，不改WR/provider；真实数据正确性仍由原OSU检验。
- 日志复用UCX LOG_FILE=%h/%p。`ucp_context.c:ucp_version_check`正常版本分支记录INFO，真实初始化后每个应用PID应有日志；日志缺失仍作为证据门禁失败。
- 复用候选身份、动态registry、实际PID/maps、CPU预算及限定清理。仅通过新上下文覆写本轮argv/解析/配置并在退出时恢复，不修改共享运行器。

没有增加消息头、PSN、ACK、软件重传、原生RMA或MPI层地址协议。
