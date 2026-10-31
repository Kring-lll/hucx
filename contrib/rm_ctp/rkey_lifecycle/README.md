# B4 原生RMA前置：远端内存key的身份与释放

起点：GitHub `82cff2375b58768fa02c28ce961cef5d86676158`。
本工作包只修复共用MD代码的四项问题，不开放PUT、GET、AM_ZCOPY，不修改建链地址或报文格式。
产品修改仅为`src/uct/urma/base/urma_md.c`和`urma_md.h`；RC、UM、RM共用，必须回归RC及现有RM路径。

| 问题 | 原行为 | 修复 |
| --- | --- | --- |
| 64位远端地址 | 缓存key的va为32位，高32位被截断，不同内存段可能共用同一个key对象 | 内部缓存保留完整64位VA |
| 完整EID | 相等比较只看EID最后32位 | 比较全部16字节；哈希也混入全部EID和VA |
| 分配失败 | kh_put已插入条目，随后calloc失败没有撤销 | 删除本次新条目，允许重试 |
| 解包后未使用就释放 | 尚未import_tseg时返回NO_RESOURCE，留下ref_cnt=0的条目 | 无导入句柄则跳过unimport，仍删除并释放缓存对象 |

哈希碰撞本身允许存在，真正错误是旧相等比较把不同身份当成相同身份。
64位VA变化是内部缓存布局变化；对外packed rkey仍为44字节，不改变已有rkey交换格式。
URMA unimport失败仍保留原有错误处理，不释放仍可能被设备持有的对象；本轮不宣称该异常恢复已解决。

## 源码审查结论与后续顺序

1. RC和RM的PUT/GET入口已经存在，均经过共用rkey解包和延迟import_seg。复制来的入口不等于RM+CTP已可用。
2. RM接口能力屏蔽由R1阶段加入，是分阶段验收边界，不是硬件不能READ/WRITE的证明。
3. SDK的`urma_write`、`urma_read`最终提交WRITE/READ opcode，但通用接口存在不能证明当前节点的RM+CTP provider支持它们。
4. UCP wireup会依据PUT/GET能力选择rendezvous和RMA lane。因此仅删除mask会同时改变MPI大消息路径，不能这样一次开放。
5. WRITE下一步需核对：RM+CTP可用操作、注册和远端key、共享发送额度、pending顺序、提交失败返回、完成和flush、销毁时的缓冲/目标寿命。先实现一个操作并单独验收，之后才恢复GET。
6. READ还需处理读取额度及返回完成、零长路径、目标和本地缓冲注册；不能沿用AM信用作为READ可用资源的证明。
7. 已观察到的共用延迟import路径还需审查同锁内重复检查、本地context归属、相同地址/身份重新注册时的key一致性、import/unimport失败处理。本补丁没有宣称这些边界均已解决。

参考位置：`base/urma_md.c`的`uct_ub_rkey_unpack/release`；`base/urma_ep.h`的`uct_ub_ep_get_tseg/import_seg`；
`rm_urma_ep.c`与`rc_urma_ep.c`的PUT/GET；`src/ucp/wireup/select.c`的RMA带宽选择条件。
已核对的URMA参考源码为UMDK `50288b2a4a62fb76e3584acd8d8ed1c1071a485d`中的`urma_cp_api.c`、`urma_dp_api.c`及所存UDMA Jetty/JFR文件；
该有限源码范围不能替代当前驱动/固件能力确认。HMPI继续使用原有UCP路径，不新增MPI交换协议。

## CPU验证

`bash contrib/rm_ctp/rkey_lifecycle/run_cpu.sh BUILD_DIR URMA_INCLUDE`

测试包含真实`urma_md.c`，URMA仅为mock，使用ASan/UBSan；五项为unused/address/eid/allocation/shared。
旧版四项断言失败，shared成功；修复后五项通过。没有设备、网络或真实性能结论。
完整x86构建、新构建上的五项key测试、既有RM实际C/地址编解码回归通过；既有Python工具38项及新控制器3项通过。
旧版失败详情及实际检查项记录在`cpu_checks_20261030.json`。
LSan在受限运行环境默认关闭；用缓存条目归零及unimport次数验证本轮对象生命周期。

## 节点执行边界

必须从新源码归档隔离构建，不覆盖c7ef可用CASE。不再使用写死旧CASE的独立ZIP运行新库。
构建成功后运行新CASE内的`source/contrib/rm_ctp/rkey_lifecycle/run_candidate.py CASE --run`。
默认不加`--run`只列计划，不进行SSH/MPI。

运行器复用原版OSU、nonce限定清理、实际进程库加载检查、双端部署与完整日志：
28正常、18边界、10窗口、10 AM rendezvous、10集合任务，并与c7ef和RC各五轮8B对照。
最高PPN4；AM日志按实际host/PID隔离。任何失败立即停止，保留JSON及全部原始记录。
不修改OSU，不更改SEG环境默认，不将诊断时延当性能结论；五轮性能仍须审阅。
这些是修复后的现有功能回归，不能代替真实设备rkey导入/READ/WRITE数据及生命周期验收。
C继续延期，严格大小交替、原生RMA、完整B仍未完成；通过CPU测试不在AtomGit提前标记完成。

## 2026-10-31 控制器目录字段修复与续跑

ac4146054候选已在ARM隔离构建；模块c211385e1fa4a5c4096f49cdc74b69af8ec82104e80edb9b460e0b7667789308。
节点终端28项正常回归通过，随后boundary_matrix读取report['controller_dir']时出现KeyError。
原始rkey_accept__txtvu2a/results.json保留，本轮不是传输层报错证据。
主脚本现补齐controller_dir；新增主流程交接和失败停止检查，旧主流程测试复现失败，新旧控制器测试合计10项通过。

resume.py默认只列计划，--run才继续。它严格限定ac4146054已测模块与CASE，
复核原报告确为本字段中断、原28项名称/结果/产品身份完整并记录两份报告SHA256；
通过原candidate.validate_build校验CASE源清单和本地库，复核双端模块，
另建rkey_resume_*，从18边界、15轮计时、10窗口、10AM和10集合继续；不调用normal_regression。
它导入原CASE中未修改的工具，校验原控制器内容SHA，不覆盖CASE/source、install或原报告。
独立ZIP仅包含续跑工具，不包含产品库；无需重编译。原生RMA、完整B/C及新候选全回归仍未验收。

## 2026-10-31 1MiB日志补验与剩余矩阵

控制器修正已由用户提交为5e0b3727a1720e068bdee54f218ce540cc2f360a；节点产品仍ac4146054/modulec211。
实际rkey_resume_j6c51b_5已复核28正常、完成18边界/15计时/10窗口/前7项AM。
1MiB osu_bw退出0且原版OSU显示1048576 427.70 Pass；因真实发送进程日志142.02MiB超过128MiB上限中断。
这是日志取证门禁，不能把该旧trial的passed改成true，也不能据此宣称协议路径已补验或完整回归已通过。

split_logs现逐行哈希、错误扫描及AM路径解析，不再整文件读取；每个进程仍限256MiB。
保留实际PID、普通文件/非软链、完整字节数、双端SHA256、全部尾部错误检查。
兼容原collect文本接口；AM调用流式解析；即使解析器提前返回，仍耗尽尾部并完成错误与哈希检查。

resume_large_log.py只处理固定本次检查点：重验先前28正常、18边界、15计时和17个已过矩阵任务的身份及覆盖。
读取1MiB原OSU输出、原rank/nonce/Finalize/绑核/maps、默认配置、WR总结和观察器哈希，
对旧日志逐行补验实际UCP路径与双端完整哈希；只在新目录写补验报告，旧trial/result不改动。
之后只新增2项AM（双向64KiB、PPN4多对64KiB）和10项集合；不重跑1MiB和前置用例。
原始1MiB带宽427.70来自诊断开启的功能任务，不作为正式性能数据。

CPU：AM/流式26项、rkey与续跑15项、集合12项、原pending4项通过，共57项。
另外实际写入142MiB模拟日志：旧128MiB门禁复现拒绝，新流式路径通过，
tracemalloc记录Python增量峰值12987字节；该值不是进程RSS、不是节点数据，也不是产品性能结论。
节点真实大日志补验和12项剩余任务待执行；产品不变、不重编译、RMA关闭、C延期、192暂停。

## 2026-10-31 对端父目录准备修正

实际rkey_log_review_g8q_c8y3已完成1MiB数据/maps/WR/AM路径/完整日志补验，
随后prepare_helpers在node97创建remaining_am时因父目录不存在中断，尚未启动剩余MPI任务。
共享prepare_helpers现先创建本轮结果父目录，再保留原test ! -e与独占mkdir目标目录检查。
不覆盖旧目标目录、不改设备或CASE/source/install。

新工具严格核验g8q_c8y3的成功补验报告、原检查点哈希、1MiB路径证据和空的remaining_am记录，
另建结果目录并复用成功补验；不重读大日志、不重跑1MiB、正常/边界/窗口/计时。
只运行尚未完成的2项AM及10项集合，最高PPN4。
CPU包含旧缺父目录真实bash复现、真实bash创建缺失父目录及拒绝既有目标、补验复用正反检查。
共71项工具测试通过（窗口12/rkey17/AM26/集合12/pending4）；真实节点剩余12项仍待验。
这两次控制器/目录中断是工具缺陷，不算新增READ/WRITE进展。下一项产品开发是原生WRITE路径；
既有回归不再扩展矩阵，不以完整B或C作为WRITE代码开工门禁。
