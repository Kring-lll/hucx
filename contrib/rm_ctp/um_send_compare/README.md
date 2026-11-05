# RC／RM／UM：原版 OSU 正常 SEND 性能与资源

2026-11-05 新阶段：祝博确认 latency/bw/bibw 只做双节点各一进程，只有 Alltoall 扩高PPN。
新包使用独立入口 run_scale_compare.py，计划255个MPI任务；详见 [SCALE_20261105.md](SCALE_20261105.md)。
当前产品不变，READ/WRITE与PSN精简均不在本轮范围。以下129/183项为此前阶段，保留原计划和结果口径。

2026-11-05：首批129项及510个实际rank身份已由节点只读复核通过，原结果SHA为dd6693098648d59db8ab4ac548be8efaddad84b4fbbbbce80c8dc087e1767a17。
PPN4 Alltoall的RM五轮[12.01,4.55,4.71,4.54,8.45]us波动，64KiB双向RM比RC低28.58%，不能隐去。
新增--repeat-study：三模式各30轮Alltoall及12轮bw/bibw，183个任务；产品不变，新结果um_osu_repeat_单独保存。
源码解释、负载取证限制和复测口径见[BANDWIDTH_SOURCE_REVIEW_20261105.md](BANDWIDTH_SOURCE_REVIEW_20261105.md)。
跨轮p95是OSU每轮平均值的分位数，不是单消息尾延迟；节点共享影响需结合复测和负载证据，不能直接归因。

固定产品为 ARM 已测 a4d07599bd5b66b84c5de8257e5f7fe15e3a6f63，模块 SHA256
48dcd30a30f29a39d8f9b63e0c2afd7279bdd3da23b1757a610c828b77b04c2f。
本工作包只增加工具和记录，src/UM/RC/公共 MD 不改，不重编译产品，不覆盖原 source/install/results。

## 工作负载

先跑三种传输的 osu_multi_lat PPN1 8B 数据校验门禁。UM 是必测 UM+UTP；失败即停，不能降级为只比较 RC/RM。

| 原版 OMB 7.5.2 | PPN | 大小 | 计时迭代／预热 |
|---|---:|---:|---:|
| osu_multi_lat | 1、4 | 8B | 10000／1000 |
| osu_alltoall | 1、4 | 8B | 1000／100 |
| osu_bw | 1 | 64KiB | 1000／100 |
| osu_bibw | 1 | 64KiB | 1000／100 |

每组每模式1轮校验（100／10），5轮计时，1轮独立资源取证；6×3×7=126任务，外加3门禁，共129任务。
5轮按RC/RM/UM、RM/UM/RC、UM/RC/RM轮换顺序；不删除高值，不把资源轮或校验轮混入计时。
带宽窗口固定8、buffer-num=multiple；三模式相同。每任务rank超时120秒、启动器140秒。
只清理当前nonce及当前结果目录绑定的进程，不做全局pkill、驱动重载或节点重置。

固定同一OSU、HMPI、候选基础库、CPU绑定和PML/COLL；TLS分别为rc_urma,um_urma:aux,self、rm_urma,self、um_urma,self。
PROTO_ENABLE=n、RNDV_THRESH=inf、ZCOPY_THRESH=inf，比较SEND eager/bcopy路径，原生RMA不是本轮范围。
三模式TX_DEPTH64/RX_DEPTH512；RM/UM窗口32、超时10s；RC流控窗口8。窗口语义不同，不能把这些数字解释为完全相同的可靠传输机制。
片长不强制相同：RM默认4096，RC/UM保留各自默认；每个trial记录双端ucx_info实际片长/inline/窗口/队列/超时。
本结果为这些明确配置下的比较，不称各传输所有生产默认配置的比较。

## 证据与统计

复用现有osu_compare启动器、解析后的有效配置、实际rank/PID/CPU/maps/Finalize和nonce清理。
原版OSU二进制不改，预检双端OSU7.5.2、HMPI、候选库和CPU许可。
三模式均保留同一个轻量PMPI身份记录，只在Init/Finalize边界保存maps、RSS，不包装通信函数或MPI_Wtime。
校验和计时均不加载WR/重发/故障/Jetty观察器；不编译、加载旧PSN历史表探针。
资源轮另外加载已有URMA资源观察器，按RM=1、RC=2、UM=4统计目标模式Jetty，并要求各rank释放完成。
RC中的辅助UM Jetty不混入RC目标模式计数。没有据此声称JFR/JFC/TP等全部资源均已验收。

RSS报告两种口径：5轮计时各rank Finalize前RSS合计的样本及中位数；独立资源轮RSS另外列出。
前者是两节点进程RSS之和、各进程采样不同步，不是设备专属内存或同一瞬间整机内存。
Jetty报告独立资源轮每rank峰值范围，不把进程数/节点数混成一个连接数。

results.json保存每次任务，trial.json含相同profile元数据。只有三模式全部7轮完成的组才生成比较值。
comparison.md与comparison.csv含中位数、范围、全部样本、RSS和Jetty；正的RM improvement表示更优，负值照实保留。
失败即停，保留原始日志/已完成组，未完成组不补数。

## 执行

新电脑应用工具Git bundle后，只上传包内hucx_rm_um_osu_three_20261105.zip到新RM实验根upload。
无需源码tar或ARM重建。默认执行ZIP只显示计划；--preflight-only只准备预检，不启动MPI；--run只跑3门禁+18校验；--run --timing跑129任务。
先核验既有um_send_accept_ftoq07pj/results.json固定SHA d6fc0dba364e1befa36d428fd0773dedc3e4b992eb618b670e033d5a8178e515，
复核原6功能/10计时trial与总报告关系（仅允许旧计时记录缺profile标签），不重跑、不把旧样本混入新对照。
运行时读取原ARM目录辅助代码，在当前Python进程加载固定新版控制器，不改原文件；只编译轻量身份记录，不重编译产品。

本地CPU检查：python3 -B contrib/rm_ctp/um_send_compare/test_compare.py。
本地测试不是ARM/真实MPI/URMA验收。三模式129项、RSS/Jetty及性能数字须以节点真实结果为准。
严格大小交替、失败退出、丢包下恢复性能、规模、硬件故障及READ/WRITE继续待验。
