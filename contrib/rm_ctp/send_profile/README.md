# 可靠SEND定向诊断

本工具只定位97b已测产品的正常路径开销，不修改产品、不重建UCX、不改变RTO、不启用READ/WRITE、不做AtomGit交付。未测部分不能写成验收通过。

## 固定依据

- 产品提交97b50328718c3c7c1b613c2ee09a90c198666142，模块3245bcca9bb5319230ed1d3e153191501fba4602a1529f405944992beb6eeec7。
- 复用40项集成结果和149项MPI/6项UCT结果，分别用2f41dd86ca79136b38c993dd38921703f5f09fd73e5fb61e84852c629847ae3a和5a88cb94e89e1d8494a9382aae8b86684ef53e753ecbd056f10a7da97aff6a13核对；不重跑整批历史任务。
- 六组正式OSU的所有五轮样本、28.1us异常样本及资源证据保留。本工具不是正式性能表更新。
- 优先调查8B multi_lat PPN1的约0.50us额外开销、8B Alltoall PPN4的约2.49us典型额外开销。对照iface_v2和reliable_v3，不把二者已有的大包RC差距都归因于可靠层。

## 执行范围

默认只输出计划，不SSH、不编译、不启动MPI。`--preflight-only`会验证双端产品/OSU/HMPI/SDK头文件，编译两个小型观察DSO，做一次短CPU进程的perf附加能力检查，但不运行双节点MPI。`--run`包含上述预检。

正常情况下最多12个新MPI任务：两个负载乘两个模式，各有一次原版OSU数据校验、一次20000轮计数、一次2000000轮采样。计数和采样分开；每rank限180秒，单次采样3秒、99Hz，每节点仅选一个真实应用PID。节点无perf或权限不够时，跳过4个采样任务，计数仍可完成，状态明确为采样不完整。通常预期数分钟，最坏受每项超时限制，不能保证固定用时。

计数器只在MPI_Init成功之后至MPI_Finalize调用之前开启：包括OSU预热、屏障和控制操作，不代表纯MPI_Wtime区间，也不能直接用SEND总数除以迭代数当一条消息的分片数。计数轮有互斥锁等开销，其耗时不用于性能比较。采样轮不加载WR/路径计数器，只加MPI边界标记和既有相同身份探针。延迟Init后250ms附加，样本再按单调时钟阶段过滤；这不是精确标记OSU正式计时循环。所有新OSU耗时都只作诊断。

## 证据及限制

- RM SEND提交：inline/complete_enable四种组合、尝试数与整链成功数、字节数和最大WR长度。失败链不推断已接受前缀。非RM SEND不混入统计。
- 可靠DATA/ACK：严格固定97b的3字节UB头及36字节可靠前缀，读头不改包。PSN按源/目标实例分组；成功DATA PSN不大于该流已见最高值，记录为重复成功提交。固定97b正常单worker发送路径中可用于检查重发；不是网络丢包位置或硬件恢复证明。
- SHORT/BCOPY入口计数：只观察导出符号拦截到的实际调用。若为0，不能断言该路径没走，可能是本地绑定绕过拦截；还需结合WR标志和采样调用栈。不会为了得到计数去改产品构建或私有对象布局。
- JFC轮询/CQE：进程所有JFC总计，可能含RM、辅助UM、收发方向；不能解释为RM发送CQE数。上下文切换为阶段前后getrusage差值。
- perf原始data、逐样本调用栈、实际PID/start_ticks/nonce和JSON均保留。平坦热点按leaf计数；包含类别可重叠，不能相加为100%。至少50个区间内样本、有UCX/URMA叶帧且未知叶帧不超过25%才记作样本已采集；仍需人工审阅，不能直接宣称根因定位。
- 两节点只比较当前SDK头文件一致性，硬件、驱动、provider兼容性仍由真机门禁及实际maps/运行判断。本地CPU fixture不是URMA真实ABI或硬件验收。
- perf不允许时不改sysctl、不修改内核或设备配置。清理复用nonce/目录/PID/start_ticks约束，仅处理本轮进程。

输出`results.json`和`diagnostic_summary.md`，每次使用新目录，不覆盖原安装、源码或历史结果。若遇数据/退出/maps/计数错误，立即停止后续任务并保留原始证据。诊断后再决定是否做ACK合并、进度扫描、对象分配等产品优化；完成优化后回跑原版OSU正式计时及功能回归。

## 本地验证

`python3 -B -m unittest discover -s contrib/rm_ctp/send_profile/tests -v`

CPU测试包含实际编译的DSO、RTLD_LOCAL模拟provider、MPI边界、errno/调用次数/标志/PSN分流，以及采样解析、身份拒绝和启动环境隔离。SDK fixture只放在tests，节点编译显式使用`/usr/include/ub/umdk/urma`。本地没有ARM设备和真实MPI/URMA库，节点待验。
