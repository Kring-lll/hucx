# 可靠SEND按完整长度使用inline

本候选从5ff9635e4d1b9479fbb60832889067de9771238e继续，只改rm_urma_ep.c中的uct_rm_urma_ep_reliable_post。完整WR长度不超过iface实际inline上限时使用inline及NULL本地tseg，超过上限保持已注册缓冲的非inline路径。参考同文件现有link_post写法，不新增产品文件、配置项或协议字段。

重放副本、ACK策略、PSN、报文格式、本地CQE及回收条件保持原实现。正常RTO100ms保持原值；失败出口测试单独采用原有受控配置。仍需等待ARM构建、设备功能及正式OSU结果，不能提前认定0.5us来自inline或承诺收益。READ/WRITE继续暂停，未做AtomGit交付。

## 节点范围

独立源码归档、独立构建与安装、独立结果目录；保留97b旧安装和全部历史结果。复核既有诊断SHA24645b7416761cdd99b0b4d7bb14dd8e2a958633e9b84a808bf130c92f917db0及正式性能SHA5a88cb94e89e1d8494a9382aae8b86684ef53e753ecbd056f10a7da97aff6a13，不混入本轮统计。

1. 双端候选库、SDK、原版OSU7.5.2及HMPI预检，旧安装单独复核。
2. 候选重跑既有legacy/iface_v2/RC正常回归28项。
3. 新可靠模式11项原版OSU功能验证：multi_lat PPN1的8/12/13/14/3982/65536/1048576B，multi_lat PPN4的8B，Alltoall PPN4的8/65536B，以及1项64KiB AM rendezvous实际路径验证。12/13/14B用于检查真实127/128/129B可靠DATA WR，不把应用大小直接当WR大小；若原版OSU实际路径没有命中预期边界，明确停止，不伪造覆盖。
4. 每个真实应用rank都验证WR长度不超4096B、实际inline标志与Jetty声明上限匹配、complete_enable保持1。旧可靠模式对照验证其原有非inline提交。声明上限与iface决策不一致会报错，不凭配置值通过。
5. 两项PPN1/4严格大小交替；一项原版OSU软件丢内部DATA/ACK/重复注入，保留真实重传日志；六项既有重试上限/期限/无故障对照退出验证。软件注入不是硬件丢包。
6. --timing请求时，两组8B性能负载（multi_lat PPN1、Alltoall PPN4），每组旧可靠97b、新可靠候选、RC候选三种模式各1校验、5轮计时、1资源观察，共42项MPI。轮换模式顺序，保留全部样本，不筛异常。加上功能任务，本控制器56项MPI，另有既有28项回归和6项UCT退出测试；不强求perf采样。

正式计时无WR/故障/资源观察器及调试日志；三种模式统一加载同一Init/Finalize身份探针。旧可靠使用原97b整套安装，新可靠和RC使用候选整套安装；源代码产品差异仅一函数，重建产物路径等仍是实验条件，不能假称两库二进制只差一个bit。队列、流控、绑核及OSU参数沿用原方案，RM4KiB、RC8KiB片长。RSS为各rank Finalize前采样求和，两节点合计、未同步，不是设备内存。资源轮与计时分开。

默认只输出计划；显式--run执行功能，--run --timing在功能通过后执行本轮计时。建议节点空闲窗口运行。失败即停，原始日志和中间结果保留。不能将功能通过写成性能优势、完整C/D或硬件故障验收。

## 本地证据

test_inline.py从实际产品文件提取完整post函数，原样编译到CPU模型，104种组合覆盖上限0/38/39/64/127/128/256、39/115/123/127/128/129/4096B、首次建控制EP/复用、保留字节再提交和EAGAIN/ENOMEM/终止错误。ASan/UBSan开启。模型不是实际URMA ABI和硬件验证。

控制器测试覆盖两个安装的切换/恢复、边界拒绝、旧模式保持非inline、真实编译DSO/RTLD_LOCAL模拟provider、完整任务流、首次错误停止计时，以及保留异常样本。原工具测试继续运行。当前环境无完整URMA SDK，未做本地整库构建；实际ARM整库构建由节点helper完成。

本地运行：python3 -B -m unittest discover -s contrib/rm_ctp/send_inline/tests -v。
