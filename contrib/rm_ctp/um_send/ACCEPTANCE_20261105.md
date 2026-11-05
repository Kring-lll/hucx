# UM框架RM SEND：所选节点结果

证据来源：用户node89真实终端及只读断言回执；原始完整JSON未下载。
产品a4d07599bd5b66b84c5de8257e5f7fe15e3a6f63；模块48dcd30a30f29a39d8f9b63e0c2afd7279bdd3da23b1757a610c828b77b04c2f。
目录/home/y00993710/urma_rm_ctp_lab/experiments/hucx-rm-um-send-dev/r1_shared_a4d07599b_BaJAce/um_send_accept_ftoq07pj。
results.json SHA256=d6fc0dba364e1befa36d428fd0773dedc3e4b992eb618b670e033d5a8178e515。

前13项只读复核通过；本轮6项新跑通过：1MiB PPN1 multi_lat；PPN4 multi_lat 8B/64KiB；PPN4 alltoall 8B/64KiB；PPN1 multi_lat 64KiB软件注入。
新6项36份实际rank WR观察全部<=4096，软件传输头8B，最终数据/退出通过。
软件注入DATA丢2、ACK丢4、重复2；按固定UM实际重发SGE布局观察到provider成功的DATA重发2次。
不是硬件链路丢包或恢复性能验收，注入数与重发数不要求一一对应。

正式8B PPN1五轮（无WR/故障探针）：RC [2.29,2.29,2.28,2.29,2.29]us，中位2.29；RM [2.11,2.09,2.07,2.07,2.08]us，中位2.08。
本批RM相对RC降低9.17%；没有UM控制组，没有大消息带宽/资源/全规模结论。不可与旧方案跨批次样本当严格同轮对照。

期间旧8B RC/RM及原生perftest异常，用户确认环境问题后恢复；同一产品模块通过新6项。
此前status4/8是访问类完成错误，内核释放/分组日志不够定位底层根因；不将%m打印的Cannot allocate memory当内存不足证据。
保留timefix旧探针溢出、环境故障期间失败记录。第一次审计把计时总报告追加profile与原trial整条相等比较，断言过严；
已仅规范化这一已确认的标签差异，其他字段仍严格核对，用户只读复核通过。

下一步六组RC/RM/UM原版OSU性能/资源工具已准备，节点尚待执行。完整C1/C2/C3、严格交替、规模/硬件故障和READ/WRITE不勾完成。
