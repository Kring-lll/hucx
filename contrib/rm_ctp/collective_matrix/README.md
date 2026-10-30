# B5 原版OSU集合大消息正常回归

起点：GitHub `e272050e17bfcc8923eceaf08f1f3a2747849d12`。
固定节点已测产品：c7ef02b51，`r1_shared_c7ef02b51_vkSZ69`；模块SHA256为
`cc12a6ed9299f87f36f927b9d4f8f8043810ec3273a96a55734a4cc89b2848f2`。
新增工具和检查，不修改src、原版OSU二进制或既有验收算法，不需要重编译传输库。

## 本轮矩阵

| 原版OSU7.5.2 | 模式 | PPN | 每rank业务尺寸 | 配置 | 任务数 |
| --- | --- | ---: | --- | --- | ---: |
| alltoall、allgather | RC/RM | 1 | 8B | 16KiB阈值；小包门禁 | 4 |
| alltoall、allgather | RM | 4 | 8B～64KiB，乘2递增 | 16KiB阈值、AM scheme | 2 |
| alltoall、allgather | RM | 4 | 16385B | 16KiB阈值、非整片大消息 | 2 |
| alltoall、allgather | RM | 4 | 64KiB | 阈值inf，对照配置 | 2 |

共10任务、36个尺寸结果行；20迭代/2预热，数据校验开启，默认每任务240秒。
最高每节点4进程，8个rank，使用同一已测库；RC只跑本表小包门禁，不是大消息性能基线。
Alltoall尺寸指每个目标块，Allgather尺寸指每rank贡献；集合内部请求可聚合/拆分，不能直接把OSU尺寸当SEND或UCP请求长度。

## 验收与边界

- 原版OSU版本、二进制/HMPI哈希、命令参数及依赖先核对；每行必须为精确尺寸、正确顺序、完整full列、20迭代和Pass。
- 所有真实rank的数据、Finalize、绑核、候选maps及原nonce限定清理复用既有运行器；错误和全部日志保留。
- 双端默认RM4096/RC和UM8192、无片长环境覆盖、AM bcopy4093、原生RMA/AM_ZCOPY关闭均要求成立。
- 8rank实际SEND观察不超过4096B；post成功不代表交付，OSU独立数据校验不能省略。
- UCX INFO日志按实际host/PID分文件保存并检查ERROR/FATAL/断言，不混入OSU结果行。避免REQ级逐片日志放大集合任务。
- 固定AM scheme和阈值属于配置证据；本轮不声称各集合请求实际路径已逐条证明。之前点对点AM路径证据仍独立保留。
- 只做正常功能回归，不把观察器开启的时延作为性能结论，不新增Jetty资源观察，不宣称所有资源泄漏检查。
- 大小递增不是反复大小交替。原版7.5.2的相关用例没有任意尺寸序列接口，此项保持待补；不改OSU算法，也不另写demo冒充正式验收。
- C重传继续延期，原生READ/WRITE和完整B未验收，PPN192暂停。

## 执行

`python3 build_zip.py OUTPUT.zip`生成独立节点工具包。直接运行ZIP默认只输出计划，无SSH或MPI；
`--preflight-only`核对双端依赖并编译身份探针，但不启动双节点矩阵；`--run`明确启动本表10项任务。
首次节点使用建议先预检，再在可用窗口运行。失败立即停止，保留`B_COLLECTIVE_STOPPED`和`RESULTS_JSON`。
不覆盖CASE/source/install，不重置设备，不全局pkill，只使用既有launcher/nonce限定清理。

节点验证前本项保持PENDING；CPU解析/模拟不等于ARM/HMPI/URMA设备验收。
