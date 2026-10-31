# 原生WRITE首条路径候选

起点：GitHub `1c9bae2e7b7351feab4b83d0eb8150880c3d0d4c`，产品前置修复已在AtomGit `d1156b6b753f8392448a82aae6fdd5091cb3ff6c`交付。
本轮实际修改RM的PUT_ZCOPY入口及其等待队列适配；不是继续扩写AM测试。READ、PUT_SHORT/BCOPY、AM_ZCOPY和重传不开放。

## 为什么先设开关

`UCX_RM_URMA_NATIVE_WRITE=n`为默认值，保留原有能力边界。设置为y才上报和接受PUT_ZCOPY。
本候选只接受一个连续IOV、单次1～4096B，不将4096解释成设备通用WRITE上限。
先验证当前节点能否实际执行WRITE，再决定默认能力与UCP/MPI RMA集成；不能仅凭URMA有WRITE枚举便宣称设备支持。
这不是完整B4交付，也不表示性能无回退。节点验收前不推AtomGit。

## 相对RC派生入口的实际修正

- 同EP已有等待请求时，新WRITE不能越过FIFO；WRITE不用远端接收WQE，不扣AM信用。
- 在设备提交前获取完成记录，缺记录返回NO_RESOURCE；不允许DMA提交成功后才依赖分配。
- 每条WRITE请求CQE，继续复用共享SQ、JFC及按EP序号的完成归属。
- ENOMEM/EAGAIN返回NO_RESOURCE，其余提交错误返回IO_ERROR，不fatal。提交失败不推进pi或扣SQ/JFC，归还完成记录并恢复fence状态。
- WR使用独立零初始化栈对象，避免继承上一条SEND/WRITE的元数据；源与目标注册区间均做不溢出的边界检查。
- 单IOV/count=1、注册句柄、长度及远端范围检查在release构建仍执行。零长不提交DMA，也不触发完成回调。
- 可选WRITE模式下，pending回调不透明，因此保持FIFO头可轮转重试，不能只因AM信用为0就永久取消调度。这一轮转仅在开关y时启用，混合负载的CPU及性能影响待测。

源缓冲及注册、远端rkey由调用者持有至完成或本地flush；成功本地完成不代替接收端数据可见性检查。
沿用共用rkey解包/导入和释放。本轮不宣称并发worker、跨context复用、重注册或unimport失败恢复已闭合。

## 已做的本地验证

完整x86构建通过，使用SDK链接桩，不代表硬件支持。
ASan/UBSan下执行真实RM C源码的既有共享发送/流控/生命周期测试及地址编解码回归，通过。
新增WRITE场景覆盖：默认关闭、实际mock写入和CQE完成、本地flush、无AM信用、EAGAIN/终态提交失败回滚、无完成槽、源/目标边界与非连续参数、零长、FIFO、无AM信用的pending重试、导入失败及取消后等待CQE。
URMA为mock，内存复制仅用于CPU检查，不作为节点WRITE或性能证据。
节点C探针已在本地编译；Python入口的默认计划和语法检查通过。Windows、ARM构建、双节点WRITE及新版本MPI回归待验。

## 节点步骤

按既有流程从新GitHub提交git archive，校验后调用`r1/build_isolated.sh`构建新的B实验CASE，保留ac414可用安装。
在新CASE内执行：

```bash
python3 -u "$case_dir/source/contrib/rm_ctp/native_write/run_candidate.py" "$case_dir" --run
```

默认不加--run仅列计划，无SSH/MPI。--run仅root node89，校验新CASE源清单和候选模块，复用已有部署逻辑核对双端库。
用原UCT API注册本地缓冲、打包/解包rkey、按iface地址创建逻辑EP，经PUT_ZCOPY原生WRITE提交8/64/4096B两轮共6次。
接收端不收SEND业务数据，而是在已注册内存中逐字节检查；TCP只交换地址、key和检查回执。
双方完成后核对候选maps及注销/释放返回值，保留命令、模块/探针SHA、环境、编译与两端日志和结果JSON。
这是定向功能检查，不是替代标准OSU的性能基准。原生WRITE通过后再做必要正常回归和标准RMA基准，不把MPI软件模拟当原生WRITE证据。

最高仅两进程；GNU timeout限定client80秒/server90秒（5秒kill-after），SSH还受连接及控制器超时限制。
只终止本轮本地SSH进程组；远端命令由本轮timeout收敛，不扫描或杀其他任务，不重置设备。
脚本失败保留结果，不自动重试、改写旧报告或重新扩大矩阵。首次失败先定位实际WRITE返回/CQE/数据，不能预判是环境或实现。
