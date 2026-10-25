# A4 控制发送子步：RM实测记录与RC回归阻塞（2026-10-25）

## 阶段结论

本记录保存固定638产品的实际通过范围及失败现场，**不关闭完整验收或A4**。
RM正常8B三轮、四逻辑EP目标复用及显式小窗口流控检查通过；RC回归仍OPEN。
这是开发过程记录，不是新的正式AtomGit产品交付。此前目标复用的正式交付仍为990398f。
结果依据用户回贴的终端摘要和已审查的固定runner；节点原始JSON/日志未下载，
审查者没有直接连接节点运行测试。保留全部失败，不用后来一次通过覆盖先前失败。

## 固定版本

- 已测产品：`6386751d32c66fe22a75efe596dfcecff6adf911`，父提交5d61752d。
- 完整源码树：`e686a7bee5ff82a9e1a8632ff17027c8bb75d6b4`。
- 源码包：`hucx-a4-control-6386751d3-source.tar.gz`。
- 包SHA256：`a3db8e14223e9b4f504fe3517af76ba7ad119a0d2cd5065969d4b0241ec3a3e8`。
- CASE：`/home/y00993710/urma_rm_ctp_lab/experiments/hmpi_rm_resources_20261019/r1_shared_6386751d3_NnF7hE`。
- ARM64隔离构建通过；安装复制到node97，双方模块相同。
- 模块：`CASE/install/lib/ucx/libuct_ub.so.0.0.0`。
- 模块SHA256：`fb06f57ac44689b238da64590cfdf5883a106347aac2491493dff935fa8c3def`。
- 节点：node89/9.88.43.89与node97/9.88.43.97，按已授权root运行。
- 本记录及清单更新不修改产品C/H，不重新构建或替换上述安装。

## 实现边界

本轮产品只改三个RM文件：rm_urma_ep.c、rm_urma_iface.c、rm_urma_iface.h。
hard grant使用iface持有的一个惰性内部软件EP管理pending、目标引用及CQE，
继续借用已有共享Jetty，复用EP构造、arbiter及注册池。已post回复持有目标/非inline描述符
直到完成或停Jetty后的清理；旧格式未post回复仍随原业务连接取消。
RC、UM、公共URMA和UCP源码未改，数据AM short/bcopy及旧线格式保持。

旧FC接收入口仍需业务EP取得回信身份。完整无反向业务EP协议、实例/请求身份、peer信用、
共享RX总预算、提前发布iface地址及CONNECT_TO_IFACE建链没有在本轮完成。
新增软件对象有内存成本，不能从硬件Jetty数量直接推出总内存收益。

## RM节点证据

| 检查 | 用户实际结果 | 节点记录（相对CASE） |
| --- | --- | --- |
| 双端模块和加载诊断 | 模块一致；RM 8B带校验、exit0 | accept_20261025_155105 |
| 四EP同一远端 | 两端载荷通过；每端import1、reuse至少3、release1 | accept_20261025_155105/results.json |
| 正常RM 8B三轮 | 1.96、2.04、2.04微秒；每轮Pass、exit0 | rm8_normal_1uzctp_5/results.json |
| 显式流控RM 1/2/4远端rank | 三例均Pass；双方FC=y/W8/TX8/RX64 | rm_only_fc_window8_uznhq632/results.json |
| 控制发送状态 | 创建数分别1/2/4，每个出现的测试进程最多一个 | 同上 |
| 数据、实际加载、资源回收 | MAPS通过、目标模式Jetty退出live0 | 同上 |

正常三轮中位数2.04微秒，仅描述这三轮；没有同条件完整RC重复样本，
不能宣称稳定性能优势或性能无回退。显式FC检查是正常软件流控推进，
不证明硬件丢包重传、终态恢复、任意规模接收预算安全或所有资源无泄漏。
软件控制owner数不是硬件Jetty数，也不是网络消息数。

RM-only FC使用留档脚本
[rm_only_fc_diagnostic_6386751_20261025.py](recorded_helpers_20261025/rm_only_fc_diagnostic_6386751_20261025.py)，
其SHA256为`298c8779e0a3b52fde38fce5e4433bd7ee4967b32c42f22b6bfb81a8bf723e4d`。
该脚本实际执行副本也在节点FC目录；C突发源字节与固定fc_window.py完全相同，
SHA256为`502048a73533151fccba7cde6b77c24a84ae26e7e1c6ec7b6f9a6291d44d3b59`。
它明确保留normal INCOMPLETE与RC OPEN，不修改原accept.py/fc_window.py门禁。
身份凭据accept_20261025_155105/results.json只是原runner写下的部分结果，不是完整normal成功。

正常RM三轮和RC的ABA对照均直接import固定638的accept.saved_spec/run，
复用原osu_minimal8_ff_14ns8命令与校验参数；移除LD_DEBUG，不加新WR插桩。
节点每轮同名JSON保存具体command/environment，保存日志和阶段results/comparison.json。

## RC失败与限定对照

此前真实失败保留：

- 候选库四EP：accept_20261025_155105/target_rc_client.log，abort，status2/Local length。
- 旧库同四EP：old_rc4_trace_vx6c3imj，旧模块实际加载，status2/Local length；
  trace中的opcode64与错误user_ctx关联为SEND，实际SGE长度和inline仍未知。
- 候选MPI8B：accept_20261025_162520/rc_00.log，exit134，status2/Local length。
  其已保存命令经安装前缀归一化与健康旧控制相同，已记录关键环境差异0；
  这不代表完整进程环境、实际加载组合、设备状态或时序完全相同。

随后进行无WR插桩RC8B的旧→新→旧对照：
`rc8_aba_8q3n5oil/comparison.json`。

| 轮次 | 安装 | 实际结果 |
| --- | --- | --- |
| OLD_BEFORE | 原d58安装 | 2.49微秒，Pass，exit0 |
| NEW | 当前638安装 | 2.24微秒，Pass，exit0 |
| OLD_AFTER | 原d58安装 | exit134，send CR status2，失败 |

三轮都是RC，不是RM/RC性能对照。旧模块SHA256为
`58c4efdf20773d0a60bd23594ca579392e968d04d0198933e3f47343ea2185ca`。
失败没有稳定跟随新库，不能认定候选特有回归；也不能据此排除运行顺序或间接影响，
或直接断言是硬件故障。RC正常/多对端/显式FC整轮没有完成，根因保留OPEN。
不继续扩大当前RC排查，不修改RC/common/provider或inline参数以取得通过。

## 诊断工具失败单独归类

old_rc_wr_metadata__2zi8v81中的WR记录工具已加载，但寻找真实URMA函数失败后主动exit127，
客户端打印RC_WR_TRACE_RESOLVE_FAILED，远端等待超时；WR记录0。
这轮不能计作RC通信失败，也没有提供实际发送长度/inline证据。该工具已停用，
不纳入可运行验收方法；它只作用于当次子进程，未替换产品安装或系统SDK。

## 尚未关闭的门槛

- 正常RM8B三轮及限定RM FC检查通过，不等于原完整RM/RC验收通过。
- 当前控制sender子步的完整回归/正式交付仍待审查；本记录不自动授予正式验收。
- A4整体、A5/A6、建链交换减少、性能/总内存收益和PPN规模验收仍未完成。
- 后续按A清单处理身份、预算及建链方案；当前不扩展到B/C或新的产品功能。
