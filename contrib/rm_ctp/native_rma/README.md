# 基础原生RMA与普通UCP读写

起点为个人GitHub B开发分支 `6ba7250595a58266b8276e0f5c7a9d35c116996b`。
该起点的READ六轮、WRITE六轮及双开关28正常已在节点通过，阶段交付为AtomGit B分支 `f1bcd0103713d887b0e29c0b108960e28039f5a5`。
当前989fd7186已完成本目录所列ARM节点验收，见[阶段记录](acceptance_20261031.md)；MPI单边OSU、完整B4和性能仍待验。

## flush修复与节点结果

基于b1895ec77继续，只改RM ep_flush的一处pending顺序检查；[定位与修复记录](flush_fix_20261031.md)。
b189节点READ6/WRITE6/基础UCT8已通过，但UCP BCOPY的64KiB GET缺少提交片段并校验失败。
旧实现提前flush已用实际源码CPU复现；修复后CPU回调须等16片读回及unpack。989fd7186已重新隔离构建，READ6/WRITE6/UCT8/UCP16×2/开启后28正常全部通过；b189失败记录保留。

## 本次产品改动

只改RM的ep.c、iface.c、iface.h。保留原READ/WRITE开关及默认关闭行为，新增默认关闭的 `UCX_RM_URMA_NATIVE_RMA`。
设为y时同时提供PUT_SHORT、PUT_BCOPY、GET_BCOPY及先前的PUT/GET_ZCOPY。普通UCP host RMA选路需要前三项，本次不修改UCP选路规则。

- SHORT最大为设备inline限制与4096B的较小值；inline为0时不上报SHORT。
- BCOPY最大为配置片长与4096B的较小值；片长默认仍4096B，无环境覆盖。
- ZCOPY仍为单连续IOV、单次1～4096B及已有设备/配置限制；大于单次限制的UCP消息由现有UCP分段。
- BCOPY使用现有注册描述符池；GET完成前不调用unpack，沿用公共完成/取消回收处理，准确归还读字节预算。
- 独立WR/SGE，检查远端rkey范围、共享SQ及pending顺序；临时post失败返回NO_RESOURCE，永久post错误返回IO_ERROR；失败不推进序号、不消费完成额度，恢复fence状态并释放描述符。
- WRITE不消耗AM接收信用，GET使用独立读字节额度。未新增网络超时重传。
- AM_ZCOPY、硬件原子操作、故障恢复仍未开放。本轮只接受阶段记录列出的正常节点范围。

## 验证顺序

新库先跑原独立READ6/WRITE6，然后基础UCT8，再普通UCP16轮各两种阈值配置，最后NATIVE_RMA=y下跑已有28正常矩阵。任一阶段失败停止并保存结果。

基础UCT覆盖SHORT 8/64B、BCOPY WRITE 8/4096B、BCOPY READ 8/64/4096/8B及偏移/完整guard。
UCP使用未经改动的公开ucp_put_nbi/ucp_get_nbi与ep_flush，按8/65536/64/16385/4096/8193/8/65536B反复交替，PUT八轮、GET八轮。两轮分别让大消息采用BCOPY和ZCOPY，小PUT仍可使用SHORT；以实际成功post日志核实路径。
TCP只交换地址/rkey和阶段标记，业务数据来自原生读写；两端都推进worker，flush后独立校验目标数据及整个guard。每轮单次WR不超过4096B。默认仅计划，无SSH、MPI或编译；--run最高占用正常矩阵PPN4。

日志按进程角色分离并流式扫描全文件；保留配置、命令、模块/程序哈希、maps、全部错误和退出码。远端进程有150秒timeout与5秒kill-after；本地控制器只终止本轮SSH进程组，其他任务不受清理命令影响。本轮不是性能测试。

## MPI单边OSU为何尚未启动

已审查公开HMPI `52d943345a85efc10de2d59311eb4bd4fa94444e` 的osc_ucx。
其context初始化同时请求RMA、AMO32、AMO64；即使应用只做Put/Get，普通UCP端点仍需为所申请原子能力选择通道。
fence使用worker_flush和collective barrier，但PSCW和lock等路径还直接使用原子操作。
当前RM没有上报原子能力，不能为了跑OSU虚报这些能力，也不能把改走其他OSC后端的成功记为原生RM验收。
公开HMPI源码是参考，未声称与节点已装HMPI二进制逐字一致。后续应先核对该构建的OSC能力需求，再确定最小接入范围。本候选不修改HMPI，不开启未验原子操作。

原版OSU单边测试与MPI_Win同步仍单列待验，不能用本目录UCP正确性程序代替正式性能套件或将完整B4勾选。
见[源码审查记录](source_review_20261031.json)。

## 本地与节点边界

本地执行真实RM源码的ASan/UBSan测试，覆盖断言/参数检查开关、4096B边界、描述符不足、pending FIFO、回调、取消/销毁、错误与回滚；完整x86构建只使用URMA链接桩，不代表硬件工作。
基础UCT与UCP程序已用当前头文件和库实际编译；本环境双进程共享内存运行遇到AF_UNIX socket权限限制，未计入通过项目。
989fd7186的ARM新库与所列节点检查已通过；本次仅归档记录，不要求重新编译或重跑。其他版本仍须隔离构建并按本目录入口验收，不据此宣称性能无回退或B整体完成。
