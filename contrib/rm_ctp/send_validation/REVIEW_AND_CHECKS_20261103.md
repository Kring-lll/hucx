# 可靠SEND下一阶段工具审查

2026-11-03，起点为GitHub rm-c-send-reliability-dev 的0fbae80e467ac56668882b7320bb9ed3241d3f32。
独立checkout准备。本次不改产品src，不修改此前已测CASE，也不把工具准备标为节点验收。

已执行：

- 新增16项CPU检查：OSU原版行/单位/版本/Validation/完整列和迭代拒错；
  异常值保留；不完整或失败组拒绝比较；三模式绑定和恢复；RC不混用RM4K观察器；
  计时不泄漏故障/资源观察器；预检无双节点任务；完整155任务调度与首错停止。
- 实际GCC编译drop_all.c并dlopen局部加载provider模拟，分别验证无故障转发、
  DATA丢弃和36字节ACK丢弃。这里的provider是CPU测试模拟，不是真实设备。
- failure_probe.c 使用本仓库公共UCT/ucs头执行GCC -Wall -Wextra -Werror语法检查。
  version.h仅按模板生成CPU语法所需版本宏；不伪造UCT函数声明、不链接假设备。
  它不是ARM编译/链路/回调运行通过。
- 既有send_integration的25项CPU回归通过；其2/4/8进程CPU接口模拟、
  真实C探针与动态加载注入器继续通过。这些也不是真实HMPI/URMA验收。
- python编译检查、默认计划和git diff --check通过。

复用已有OSU启动解析、固定MPI/MCA、双端身份检查、进程绑定、maps、Finalize、
单归档收集、Jetty释放核算及nonce/PID启动时间清理，不复制另一套MPI launcher。
直接UCT退出探针复用已测node_probe.c的公共UCT建链和数据检查；只补错误回调与终态出口断言。
ACK仍按36B控制消息解析，不能套用104B DATA头长度。
失败试验的RTO=TIMEOUT=800ms满足当前初始化约束；绝对期限判断先于重传，预期0次重传。

正式性能只比较同一产品库上的RC、iface_v2和reliable_v3，不能称为改造前RM对照。
内存保留五轮RSS原值和范围；Jetty来自独立资源轮；没有新数据前不输出任何性能优势数字。
所有原失败、环境异常及历史阶段记录保留；不因本包准备完成而勾选完整C/D。

待节点：双端编译新退出探针、22集合＋1原OSU软件注入、6退出，以及显式请求的126性能/资源MPI任务。
没有执行硬件丢包/设备故障、OOM/迟到CQE/全部取消/唤醒与规模验收。
交付脚本的补丁/源码树、PS语法与本地私有远端模拟结果另随下载包DELIVERY_CHECKS.log保存；
不会把本地模拟记为Windows原生执行或线上推送。
