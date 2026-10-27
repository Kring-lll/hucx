# A8/A9 两节点正常建链规模验证工具（2026-10-27）

本包只新增测试和交付工具，`src`与节点已测2a8e相同。不实现C重传，不打开RMA或zcopy。
CPU测试通过与真实MPI/URMA节点通过分别记录；当前A8/A9节点结果仍为待验收。

## 固定对照和阶段

| 名称 | 实际安装 | 目的 |
|---|---|---|
| iface | 已测`r1_shared_2a8e3b771_ps2tIz/install`，iface_v2 | 当前RM |
| old_rm | 已测`r1_shared_4b77878ce_DbsqE3/install` | iface改造前RM，已有共享Jetty/目标复用 |
| rc | 当前2a8e安装的RC | 同库、同拓扑的RC对照 |
| 旧8B控制 | d58最小安装 | 环境与既有8B校验门禁，不代替旧RM的规模对照 |

同规模三种模式使用相同W=8、TX=64、RX预算、8/64/1024B消息与轮次。
原28项回归仍用TX=8/RX=64；规模试验TX=64另行解析确认并由observer核验，不能混为同一配置。
K=2*PPN（含可能的self/local地址账户），`R >= K*(W+2)+2`，R向上取2的幂且至少64。
因此PPN20是R=512，PPN192是R=4096。设备若不支持或内存不足则停止，不偷偷截断额度。

运行顺序：固定库/依赖双端核验 → 两端实际CPU/内存/进程与fd限制 → 旧8B →
28项正常矩阵 → 四EP/零反向业务EP → 160EP生命周期 → MPI地址阶段 →
A8 PPN 1/2/4/8/12/16/20 → A9仅RM逐级到实际可用物理核数（最多192）。
A9的运行门禁是A8完整正常功能与资源安全门禁通过；此时性能结论仍须审阅。
它不会把尚未审阅的A8性能判定自动标成通过，也不会自动关闭A10。

若只具备20核，A8可以运行，A9保持未验收；不以超额占核凑PPN192。
可用核数依据两端进程affinity与物理core分组，实际每个rank绑核、hostname和maps都保存。
每级保存完整时序、样本与异常。任一失败停止扩大；RC永不超过PPN20。

## 计时口径

`mpi_scale.c`不用MPI_Barrier/Reduce作预先同步，测试专用TCP只做阶段同步，计时外运行。
首次通信前未通过额外应用collective预建待测连接；HMPI在MPI_Init内部做的事情仍属于Init成本。

- `init_max_ms`：各rank实测MPI_Init耗时的最大值，非整个mpirun启动耗时；另存launcher wall time。
- `first_all_peers_max_ms`：首次与每个对端rank通信的一整轮8B Sendrecv最大rank耗时。
  包含必要逻辑EP创建及第一次数据，不把它叫“纯建链”或网络握手次数。
- `exchange8_max_rank_us`：预热后8B双向Sendrecv交换的最大rank耗时/交换数。
  **不是OSU单向延迟**，不能把此数直接与此前约2us的OSU latency相减。
- `burst64/1024_payload_MBps`：两节点所有rank同时向所有跨节点对端发送，Irecv/Isend/Waitall
  每轮数据校验；总有效payload字节（两个方向各计一次）/最慢rank阶段耗时。
  包含验证/进度开销，是此用例有效吞吐，不是链路物理带宽。
- Init/首次/稳定同阶段RSS分别保存；ru_maxrss单列为峰值，不把它当同阶段内存。
- 性能每模式每规模默认3轮，轮换模式顺序。资源观测另跑一次，不混入性能样本。
  每次性能/资源进程都保存三阶段真实maps；只资源轮LD_PRELOAD observer。全部样本含异常值保留。
- observer报告的是目标模式Jetty成功创建/释放和每context峰值，排除UM辅助资源；
  不把Jetty数量下降写成已测“总硬件内存”下降。进程RSS、队列预算、对象计数分别报告。

对RC/旧RM的变化正负都保留。无预先约定性能回退阈值，因此工具只自动判断数据/身份/资源/退出，
不会自行给“稳定优势”“无回退”“A整体完成”的结论。人工审阅中位数、区间及原始样本后再交付。

## 节点运行

将交付的`hucx_a89_20261027.zip`手动上传实验根`upload`。zip是Python zipapp，不是源码tar包；
入口核验内含工具哈希，解压到固定已测CASE下新的toolkit目录。**不用重编译传输库，不覆盖旧版本。**

```bash
python3 -u /home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/upload/hucx_a89_20261027.zip
```

默认全部回归+A8+A9。仅看预算可追加`--plan`（不SSH不启动MPI）；只运行A8可追加`--phase a8`。
`--skip-regressions`仅诊断，明确阻止A9完整验收，不作为成功交付方式。
每次新建`CASE/a89_随机目录`；同端远端同名唯一目录，日志/配置/报告均保留。
旧回归工具另用实验根下新建的`a89_随机_regressions`安装副本，避免覆盖原CASE的测试二进制；
源码只读引用固定原版本，产品库不重编译。该隔离副本需要额外磁盘空间。
默认每个规模任务300秒，每个rank有独立GNU timeout，超时只清理本轮launcher和nonce匹配进程。
清理使用`/proc`身份+启动时刻+随机nonce+本轮路径，不使用pkill、killall、驱动复位或设备重启。
历史回归沿用已测工具的现有超时边界；旧8B失败立即停止，不在未知环境上继续扩大。
若运行被断电/SSH中断，rank timeout仍有上限；不要手工清理不属于本轮的MPI进程。

截图看终端各PPN的功能/时延/资源行。详细看`results.json`、`summary.md`、
每个trial的`rank*.json`、三个阶段maps、`pid_rank*.json`、`trial.json`和launcher/config日志。
失败时保留该目录，回传终端停止行及结果路径即可。没有节点输出之前，A8/A9仍是□。

## CPU检查

```bash
python3 -m unittest discover -s contrib/rm_ctp/a89/tests -v
python3 contrib/rm_ctp/a89/tests/cpu_program.py
```

第二个命令使用文件传递的测试专用MPI API替身，编译实际`mpi_scale.c`，执行2/8个进程，
覆盖TCP阶段同步、配对/并发发送、结果/内存快照和主动损坏payload的失败检查。
**它不是实际MPI实现，不加载UCX/URMA，所有耗时不能当性能结果。**
真实HMPI/URMA编译、硬件资源、当前固件行为和完整运行器端到端仍须节点验证。

B下一步参见[B_NEXT.md](B_NEXT.md)，本包没有提前完成分片。
