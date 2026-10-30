# RM pending顺序修复与正常回归

## 已复现的问题

节点产品80b28a25在原版OSU `osu_bw`、16385B、窗口8、AM rendezvous路径中异常退出。
接收端调用链为 `ucp_tag_rndv_process_rts` → `ucp_proto_progress_rndv_rtr` →
`ucp_do_am_single` → `uct_rm_urma_ep_am_short`，触发原pending顺序断言。
前四项（RC/RM 8B、RM 16383B eager、RM 16384B AM rendezvous）通过，整个10项矩阵未通过。
原始证据在旧CASE的`b_am_rndv_30dr4_kh`，不修改该目录或已测安装。

断言说明同EP有未完成的pending队列，当前调用却不在pending回调中；不等同于超4KB报长或网络丢包。
源码确认iface模式收到grant后增加共享额度、调度EP；在arbiter真正处理之前，同批RX可以触发新的RTR。
节点栈没有精确证明是哪一笔grant/SQ完成恢复了额度，因此具体恢复事件仍属推断，不把它写成已测事实。

## 最小修复

- AM short/bcopy入口保留原顺序断言，并增加实际运行的排队检查：同EP有pending时，新AM返回NO_RESOURCE。
- pending_add在已有队列时接受新请求，不能仅凭额度已恢复返回BUSY，否则UCP可能立即重试同一阻塞发送。
- iface记录当前pending回调所属的group。仅该group的回调可以继续自己的队首；其他EP不能借用此权限越过自己的队列。
- 回调前后保存/恢复group，初始化为空；行为不依赖调试断言。共享Jetty、独立FC控制发送和现有GC不改。

RC保留其原实现；它同样有顺序断言，但其旧FC额度恢复分支立即推进pending。
UM用HAS_PENDING及当前回调状态控制新发送/入队，本修复借鉴其排队责任边界，不移植UM序号或重发。
UCP/HMPI/原版OSU/URMA/provider均不改。对不同EP的总顺序、底层CTP保序不作新增承诺。

## 检查与节点操作

本补丁修改产品，须重新隔离构建，不能把旧工具ZIP用于旧80b安装就称修复通过。
中转机按新GitHub提交与tree归档；节点校验上传SHA后复用`r1/build_isolated.sh`，实验根保持B目录。
新CASE创建完成后执行：

```bash
python3 -u "$case_dir/source/contrib/rm_ctp/pending_order/run_candidate.py" "$case_dir" --run
```

默认不带`--run`只打印计划。执行时校验本CASE源码manifest/模块/来源，向对端只部署新CASE且不覆盖已存在的不匹配安装。
依次复用既有28项正常矩阵、18项边界、原版OSU 8B五轮对照（旧RM为80b已测4KB产品），
然后窗口10项与AM rendezvous10项。最高PPN4、最大1MiB，遇失败停止；每个新窗口/AM任务240秒。
输出独立`pending_order_accept_*`与完整JSON；校验/协议/逐SEND证据单独保存，原生RMA/AM_ZCOPY保持关闭。
新10项窗口/10项AM开启观察和校验，不用作性能结论；五轮8B原始样本须审阅后才能判断回退。
沿用原runner的launcher/nonce限定清理，不全局pkill、不重启/重置设备。C延期、192暂停，完整B不宣称完成。

CPU实际C回归使用真实RM代码、mock URMA：4项资源恢复+两种AM入口的FIFO/入队/排空检查，以及跨EP group隔离。
另有原有全套C异常/生命周期回归。开启断言与--disable-assertions两种独立x86构建及ASan/UBSan回归均通过；旧代码用同一资源恢复场景实际复现原断言。CPU结果不是ARM或真实网络验证；节点状态仍待执行。
