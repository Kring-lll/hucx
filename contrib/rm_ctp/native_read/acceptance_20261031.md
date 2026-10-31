# 原生READ及读写正常阶段验收

产品：`9e75fa386fdfe430399ac7d27b71088ed2baf1e6`，源码树`6834a58e90463d33366129f41e59cdd15b3aefe1`。
ARM模块SHA256：`3caa5f9e4eaeccf3dc99c2a28e1fda73ba600d1f82849fd978223c56d7a52dcf`。
源码归档SHA256：`9f317d5b1d5d81317482030849a595dc89b1d10e3d0f50edae6f4ec874ff977a`。
本记录根据用户回传的节点终端输出整理，原始JSON、命令和日志仍在节点；未把终端摘要冒充下载的原始报告。
结构化摘要及准确路径见[节点记录](acceptance_20261031.json)。

## 实现与结果

READ复用既有注册/rkey、共享Jetty和EP完成归属，增加独立NATIVE_READ开关，仅开放单连续IOV的GET_ZCOPY。
入口在提交前预留完成记录，检查源/目标区间和读字节预算，提交失败回滚；取消及错误回收复用已有机制。
最后CQE也及时归还读字节额度，无用户回调时同样保留完成记录。pending继续按FIFO和下一轮progress重试。

| 工作项 | 本轮节点结果 | 范围 |
| --- | --- | --- |
| 归档校验和ARM隔离构建 | PASS | 新CASE，原安装保留 |
| 原生UCT READ | 6/6 PASS | 8/64/4096B各两轮，本地目标、远端源、完整guard、双端maps、注册释放 |
| 新库原生UCT WRITE回归 | 6/6 PASS | 8/64/4096B各两轮，接收内存、双端maps、注册释放 |
| READ/WRITE双开关正常矩阵 | 28/28 PASS | 最高PPN4，实际命令开关及产品身份核查 |
| 单边OSU文件与OSC组件核查 | 三程序各1份、OSC UCX存在 | 没有运行单边OSU或MPI RMA测试 |

READ-only和WRITE-only分开验证，正常矩阵同时开启两个开关。WRITE及28项均在本次新库执行，没有用旧版本通过记录代替新产品回归。
READ探针通过TCP交换地址、key和同步标志，业务数据经GET_ZCOPY搬运；源内容和本地目标guard分别检查。
软件注入的错误CQE、取消、在途销毁和两个EP完成归属属于CPU证据，不是本次设备故障注入。

## 8B样本和提示说明

本轮同库legacy/iface/RC的8B中位数为2.25/2.21/2.54μs。
legacy是同一新库的旧建链模式；完整样本在节点。该功能回归批次不外推所有尺寸、负载和性能无回退。
WRITE-only子工具仍打印旧固定文案“READ未验收”，它未汇总另一个READ专项；本轮READ是否通过以独立READ结果和最终READ_6_WRITE_6_NORMAL_28汇总为准。
normal阶段所谓“证据复用”只复用本次新库刚跑的六轮WRITE，没有再次执行第三遍WRITE。

## 尚未验收

两个开关默认仍为n。已通过范围为单IOV、单次1～4096B，不把软件限制写成硬件通用上限。
PUT_SHORT/BCOPY、GET_BCOPY、AM_ZCOPY和原子操作未开放，原生MPI RMA及正式单边OSU未验收；完整B4仍保留□。
28正常矩阵保持原有协议阈值，不证明GET型rendezvous或所有大消息协议已通。
历史大消息矩阵和PPN扩展结果归属于各自已测产品，本轮未重新执行全部历史项目。
后续补齐当前UCP普通RMA选路所需基础读写入口，验证UCP Put/Get与MPI窗口、完成语义，再运行原版OSU；不虚报能力，不修改OSU以获得数字。
C重传延期，PPN192定位暂停。
