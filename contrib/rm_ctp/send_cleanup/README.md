# 可靠 SEND 去重候选

起点为已在双节点验证的 bdc1e36e9ee75d97dee2d3e93b9e83b6bd10b49f。
本次只整理重复实现和模块职责，不继续调性能，也不恢复 READ/WRITE。

## 实际修改

* 三处控制 EP 创建收拢为 `uct_rm_urma_iface_control_ep_get()`。
  它借用已有 Jetty，仅作为控制报文 CQE 的软件归属；失败后恢复创建标志。
  调用者保留 `ctrl_ep == NULL` 快路径判断，已经创建时不额外调用该函数。
* `addr` 和 `reliable` 重复的小端 32/64 位读写合并到已有 addr.h，仍使用
  memcpy 处理未对齐报文，不用结构体指针强转代替。
* v3 地址编解码从 reliable 移到 addr，保持原函数名、48B布局、版本/标志、
  返回状态和失败不发布输出的规则；link 和 reliable 调用同一份实现。
* 删除没有实现、没有调用的 `uct_rm_urma_available_set()` 和
  `uct_rm_urma_ep_am_short_iov()` 声明。
* 更新 pending_mp 注释，说明它也承载可靠重放记录。

产品共改8个已有文件，107行增加、137行删除，净减少30行；没有新增产品文件、
结构体、字段、内存池、配置项、协议报文或工作线程。文件数量未减少，因为搬到
一个大文件不能消除运行时职责。逐文件说明见 SOURCE_REVIEW.md。

## 保留的边界

不改变 DATA/ACK 字节、PSN 分配、接收去重/保序、累计 ACK、窗口和接收债务、
inline 长度判断、100ms正常RTO、重试/失败出口、本地CQE+远端确认回收条件。
旧 legacy_ep/iface_v2 仍用于兼容和对照；暂停的原生RMA保留原开关，默认关闭。
删除旧模式或原生RMA会改变现有功能范围，本次没有这样做。

## 验证与使用

本地执行以下三组检查，结果随交付记录，不把CPU模型当设备证据：

```
python3 -m unittest discover -s contrib/rm_ctp/send_cleanup/tests -v
python3 -m unittest discover -s contrib/rm_ctp/send_inline/tests -v
python3 -m unittest discover -s contrib/rm_ctp/send_pool/tests -v
```

新旧真实地址codec做字节/返回值差分，原5组golden/未对齐/错误原子性检查带
ASAN/UBSAN；控制EP创建失败、恢复、复用及实际可靠post的104组边界/错误模型
带ASAN/UBSAN；记录池所有权/ACK与CQE先后/回滚/隔离清理模型沿用原检查。
容器LSAN不可用，使用detect_leaks=0及显式所有权检查；未宣称LSAN通过。

候选须在ARM独立重建。入口复用现有send_pool验收引擎，新增薄封装而非复制
整套控制器。默认不测性能，运行 `run_candidate.py CASE --run`，覆盖28项旧模式
正常回归、11项原版OSU功能（至1MiB）、2项大小交替、1项软件注入及6项有界退出。
即14项专项MPI、28项正常回归和6项UCT退出；软件注入不等于硬件故障验收。
已测bdc安装及结果作为固定检查点保留，不覆盖旧source/install/results。

本地没有完整URMA SDK，整库构建和真实节点回归待执行。旧性能结果只属于旧模块，
不能自动作为精简版性能结论。完整C/D及硬件故障未验收，AtomGit本次不更新。
