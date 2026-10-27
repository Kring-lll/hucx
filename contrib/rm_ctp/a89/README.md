# A8/A9 两节点正常建链规模验证工具（2026-10-27）

本包只新增测试和交付工具，`src`与节点已测2a8e相同。不实现C重传，不打开RMA或zcopy。
CPU测试通过与真实MPI/URMA节点通过分别记录；当前A8功能资源门禁已通过，性能审阅及A9更高规模待完成（以下最新说明优先于历史段落）。

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

将交付的`hucx_a89_transferfix_20261027.zip`手动上传实验根`upload`。zip是Python zipapp，不是源码tar包；
入口核验内含工具哈希，解压到固定已测CASE下新的toolkit目录。**不用重编译传输库，不覆盖旧版本。**

```bash
python3 -u /home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/upload/hucx_a89_transferfix_20261027.zip
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

## 2026-10-27 启动命令兼容修复

首次节点运行a89_fethe1ma：旧8B及四组既有回归通过；A8首次任务在解析保存命令时
遇到应用侧`/usr/bin/env`而停止，规模MPI任务尚未启动。不是传输层数据失败。
修复把MPI选项与应用env分开，保留应用赋值写入rank配置；本轮受控的mode/FC/队列等
参数仍由规模配置覆盖。MCA值中的env不会被误判为应用边界。
支持plain env及可选`--`后的NAME=value；清空环境/取消变量/执行shell等未实现动作仍明确拒绝，
不会静默忽略。新增5项CPU用例覆盖两侧env、优先级、MCA值边界和不支持动作。
预检提前到旧8B/四组回归前，失败保留来源JSON哈希和停止位置。传输src未改。
新zip用独立文件名，不覆盖旧包；节点仍需实际重跑，A8/A9保持未验收。

## 2026-10-27 资源观察器进程归属修复与本轮恢复

节点a89_xtypmczp四组回归再次通过。PPN1首个资源轮两rank数据/Finalize均通过，
每rank记录却有两个PID：timeout进程RM acquire=0，实际程序RM acquire=1。
根因为旧包装把observer同时预加载进timeout和应用，不是已证实的产品泄漏。
修复仅把观察器赋值放到timeout之后的env执行边界。非观测计时路径不加载observer，
资源轮仍严格要求每rank一份记录、TX深度正确、退出live0，并核对实际应用PID。

本次可用`--resume-from CASE/a89_xtypmczp/results.json`，只接受此已知检查点和原三轮配置。
工具重新核对双端库、boot ID/CPU许可、启动JSON、原工具manifest、回归通过日志、
原二进制、每rank原始数据及maps、空清理结果；只复用四组回归及九个PPN1无观察器计时。
原始失败不覆盖，新报告保留路径、哈希和失败对象。三模式资源轮全部重跑；后续规模照常运行。
恢复只允许一小时内同一节点/库环境；过期则自动完整重跑门禁和计时，不复用旧数据；其他证据不符仍停止。
原始计时和恢复后资源观测来自不同时间点，性能仍须人工审阅，不能自动宣称稳定优势。

新增4项观察器测试使用真实gcc动态库和GNU timeout，复现双构造并验证仅应用被记录；
新增4项恢复测试用明确标注的CPU模拟数据验证通过复用、失败保留和变更拒绝。
所有src保持已测2a8e；真实节点资源重跑尚未执行，A8/A9仍未验收。

## 2026-10-27 三阶段maps取证文件契约修复

a89_e_vkgiij节点观察器归属已正确：两rank观察器PID=应用PID，RM各acquire/release=1/1，
深度64，数据及Finalize通过，清理无残留。但检查器错误沿用旧many_peer的per-PID maps文件名，
而mpi_scale.c实际生成rankN_after_init/after_first/after_steady.maps，导致FileNotFoundError。
现按这三个真实阶段核验库加载，任何阶段缺失或混库仍拒绝，不复制文件假造旧格式，也不取消检查。

CPU文件契约检查已接通实际C基准体的PID、三阶段kernel maps、资源解析、比较统计和Markdown/JSON报告；
该检查使用明确标注的文件MPI替身、空DSO和模拟Jetty事件，不是实际UCX/URMA验收。
恢复入口保持核验；一小时内可复用原回归和九个PPN1计时，只有“过期”会自动改为完整新跑，
其他证据不符仍停止。原失败不修改，后续节点结果仍待回收。

## 2026-10-27 大规模取证取回修复及PPN96有界续跑

实际节点`a89_n8hhrpqz`中，A8七档三模式功能/资源通过；A9到PPN64通过。
PPN96资源任务启动器退出0、清理无残留，旧scp逐文件取回超过120秒；局部文件不足不等同于MPI失败。

`artifacts.py`以一次SSH连接输出tar+gzip流，带每文件大小和SHA256；只收本轮指定peer rank的
JSON、三阶段maps及资源JSONL；拒绝跨路径、链接、重复、损坏、覆盖不同内容及不完整的文件集。
全批校验成功后才发布到取证目录。压缩包、stderr及收件凭据均保留；900秒是取证传输上限，
不改变MPI任务300秒上限，也不包含任何重传协议改动。只传文件时不占用整机MPI计算资源。

### 本轮推荐入口（node89/root，已协调双节点占用窗口）

```bash
python3 -u /home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/upload/hucx_a89_transferfix_20261027.zip --recover-a9
```

仅接受固定已测CASE下`a89_n8hhrpqz/results.json`，创建新的`a89_recover_随机`目录。
复核源工具、产品及全部基础库、boot ID/CPU许可、观察器、保存的启动命令、原二进制、
四组回归标记以及100轮原始trial证据。PPN1旧计时仍沿原检查点路径核验，不改原日志或时间。
复核此前已通过的每rank数据、三阶段maps、绑核、完成退出及Jetty资源，不重跑A8。
只允许原错误为启动器退出0、清理为空、PPN96资源轮scp超时；其他失败不得按此入口跳过。
从peer重新补取96个rank证据，在新目录核验全部192个rank；全部通过才把PPN96列入已通过范围。
原失败trial/report保持字节不变，新报告保留来源SHA和恢复说明；不把failed字段直接改成通过。

随后重新执行旧版8B门禁，在相同产品/CPU许可下只续跑RM PPN128/160/192，每档三轮无观察器计时
和一轮资源取证。默认会到每节点192进程，须协调同事暂停两节点重负载工作。
若只想补证而不启动任何MPI，用同一命令追加`--recover-only`；该选项不宣称A9到192完成。
若节点重启/库或原始数据变化，停止且解释原因；不会借“恢复”自动接受新环境或更改历史样本。

历史A8/低规模A9与本轮续跑明确分成不同测量轮次，不混合重新统计旧中位数；
这次恢复不设置一小时失效后自动重跑A8，因为其用途是保存并复核历史证据，不能当作同一时间窗性能样本。
完整无回退、性能审阅及A10仍须人工结论。C继续延期，不修改src，也不在此启动B正式交付。

CPU检查新增全100轮合成文件矩阵、PPN96中断后补证、PPN192批量取回、损坏/缺失/跨路径/混库/
泄漏/不正常清理拒绝。合成数据与CPU替身不代表真实MPI/URMA或硬件性能；真实续跑仍待节点执行。
