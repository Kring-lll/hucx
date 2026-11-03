# 可靠SEND：集合、失败退出和原版OSU性能/资源

固定产品为97b50328718c3c7c1b613c2ee09a90c198666142，模块SHA256为
3245bcca9bb5319230ed1d3e153191501fba4602a1529f405944992beb6eeec7。
本包只新增工具与说明，不改src、默认模式、RC/UM或已测CASE/source/install。
节点结果尚未回收，下面均为待执行检查。

## 运行

`python3 NODE_ZIP`只核包并显示计划，不SSH、不编译、不启动设备任务。
`--preflight-only`检查40项通过的固定检查点、双端库/OSU/配置/能力/CPU与依赖，编译本轮探针；
不启动双节点MPI或UCT。`--run`执行功能与退出门禁；`--run --timing`在全部门禁通过后继续性能/资源。
每次新建send_validation_tools_*与send_validation_*，不覆盖已测文件。

运行位置：node89 root。对端root@9.88.43.97。节点包放在
/home/y00993710/urma_rm_ctp_lab/experiments/hucx-c-send-reliability-dev/upload。
既有CASE为同实验根下r1_shared_97b503287_f1PZta，工具不重编传输库。
原OSU、HMPI、资源观察器继续复用旧实验根hmpi_rm_resources_20261019的已测安装。

## 功能与退出门禁

1. 集合22项：原版OSU7.5.2 Alltoall/Allgather，各1项RC PPN1/8B；可靠模式各PPN1/2/4、
   8/3982/65536B固定eager，共18项；另各PPN4/16385B固定AM阈值16KiB，共2项。
   检查原程序Validation Pass、完整退出、所有实际应用maps/绑核/Finalize、逐SEND长度。
   集合的AM配置只说明所选配置，不当作实际请求路径证明；已有40项的6个点已做实际AM请求证明。
2. 原版OSU软件注入1项：osu_multi_lat PPN1、64KiB、20轮/2预热。
   复用已测内部片注入器：双向各丢一个内部DATA片、丢前两个ACK并重复一个内部片。
   必须有每个真实PID的触发计数及生产实际retry日志，原程序数据校验和退出通过。
   原OSU二进制不修改；统计含初始化/校验通信，不把WR数等同于MPI消息片数。
3. 直接UCT退出6项：同一个公共UCT驱动，每组先无故障对照，再持续丢DATA、持续丢ACK。
   重试上限组：RTO100ms、RETRIES3、TIMEOUT5s，要求实际软件重传3次后失败。
   绝对期限组：RTO=TIMEOUT=800ms、RETRIES256，要求先到绝对期限、重传0次。
   两种故障都必须看到两个共享iface业务EP各一次UCS_ERR_ENDPOINT_TIMEOUT回调，iface/EP flush
   保持该错误，后续SEND拒绝；接收交付按故障分别为0或1次，内容与去重检查后双方关闭并退出0。
   外层timeout退出124永远是失败，不作为预期失败通过。失败专用参数不改性能轮或产品默认。

软件注入在成功硬件接收后、生产译码前发生，不是硬件丢包、provider错误CQE、网卡复位或故障恢复。
退出检查覆盖所列软件期限和共享iface错误上报，不代表CANCEL、迟到CQE、OOM、事件唤醒等完整D4。
接收端无业务EP，因此不要求它也收到业务EP错误回调。资源释放以完整close和退出为本轮证据，
不声称已独立核算失败路径所有设备资源的余额。

## 正式OSU：六组负载，三种模式

- osu_multi_lat、osu_alltoall：PPN1与4，8B，4组。
- osu_bw、osu_bibw：PPN1，65536B，固定window8，2组。
- 模式：同一97b库的RC、iface_v2、reliable_v3。iface_v2是同库旧建链模式，不是改造前产品。
- 每组每模式：1次数据校验，5次独立计时，1次独立资源轮，共126个MPI任务。
  加功能23个MPI与6个直接UCT任务，总共149个MPI＋6个UCT双端任务；最高PPN4，绝不跑PPN192。
  预检CPU许可不是空闲证明。请在两节点合适窗口执行，避免与同事争用设备和CPU；本包不修改节点设置。
- 计时与资源轮均1000迭代、100预热；三模式顺序按轮次轮换。数据校验另跑20/2，不混入计时样本。
- 计时不加载WR、故障或Jetty观察器，日志warn。三模式保留相同轻量MPI身份探针，
  只包装Init/Init_thread/Finalize，在计时循环外采maps/RSS，不改数据操作或MPI_Wtime。
- Jetty单独资源轮观测：所有真实rank/PID、每context/每进程峰值、TX深度、成功删除且退出余额0。
  RM要求每context峰值1；RC保留实际波动，不按PPN公式伪填。UM辅助Jetty不混入目标模式计数。
- RSS以五次计时轮各rank Finalize前RSS之和计算中位数及最小/最大值，两节点合计MiB；
  保留每rank原始值与初始RSS。采样不同步，不是某一瞬间全节点占用，也不是Jetty/设备独占内存。
  独立资源轮RSS另外保存，避免把观察器引起的增加混入主RSS表。
- 设备、rank绑定、TX64、RX64/128、流控窗口8及eager配置一致；RM4KiB、RC8KiB片长明确保留。
  原版OSU的带宽口径原样保留；BiBW本身已含双向，不再乘二。
- 所有样本、异常值与回退完整保留；无任意丢弃、自动挑优、延长超时或切回其他库后继续。
  相对变化同时对RC和iface_v2计算；时延上升是变慢，带宽上升是变快，RSS上升是占用增加。

## 超时、证据与交付边界

MPI每rank240秒，launcher额外20秒；只按本轮nonce、精确run目录和PID启动时间清理。
直接UCT进度期限20秒、TCP20秒、独立GNU timeout45秒；它只终止本轮执行的命令/进程组，
不执行节点级pkill、killall、设备重置或清理别人的任务。失败后停止后续扩大，保留原始退出码。

保留package/tool receipt、固定40项检查点SHA、双端有效配置、OSU/HMPI/探针SHA、
每轮参数/环境/launcher、所有真实rank/PID/maps/Finalize/RSS、WR/Jetty、软件注入和完整必要日志。
计时表comparison.md、comparison.csv在每个完整组后更新，失败不丢已完成组，原始失败仍在results.json/trial.json。
整个包在节点上尚未运行。CPU解析、调度契约、实际dlopen注入器和公共UCT头语法检查不是节点验收。
本阶段不启用READ/WRITE或原子，不调整100ms正常RTO，不宣称完整C/D或规模与硬件故障恢复完成。
代码精简留到性能结果回收之后。
