# B1 RM SEND WR长度专项（待节点）

产品仍为已测2a8e，不改变默认片长或传输协议。本工具仅诊断，OSU仍是原版7.5.2；有观察开销，不用于性能表。

## 做什么

- 固定两节点PPN1，仅`osu_multi_lat`的8/4093/4094/8192/16384字节，校验100次、预热10次；每任务120秒，复用已测身份/配置/绑核/限定nonce清理。
- 双端先验证4K配置、4093 AM bcopy能力、固定产品/OSU/HMPI。观察器在每台节点用该机URMA头文件独立编译；不带预编译.so、不重建产品。
- 观察器排在预加载liburma前面，用RTLD_NEXT原样转发，校验实际提供者属于liburma。只统计公开jetty配置为RM且opcode为SEND的WR，UM及其他操作不纳入。
- 每次下发前只读取WR/SGE元数据，累加所有SGE.len。直方图按总字节、SGE数、inline标志聚合，输出提交数和整次调用成功对应的WR数。不读取业务payload，不记录地址/密钥，不改WR或返回值。
- 失败调用不猜测链中哪些WR已接受；错误/不完整记录/未截获/身份不符/任何RM SEND>4096均停止并保留结果，不能给通过结论。
- 远端只回收实际OSU PID对应摘要，核对rank/PID/nonce和after_init/before_finalize实际加载路径。原runner的超时和只清理本轮进程的规则不变。

## 能证明与不能证明

通过时只能说明本组场景、配置下实际观察到的RM SEND长度均不大于4096且OSU校验通过。总WR计数包含RM流控、建链、MPI同步/统计；不等于单条MPI消息片数。`success`是post API整次调用返回成功，不是SEND CQE或远端交付证明。WR不是网络物理包；不能据此确定硬件极限、内部切片、保序或故障恢复。C重传继续延期。

## 使用

节点上传zip，默认无参数仅打印计划、不SSH或运行MPI。显式`--run`才运行，root/node89限定；输出独立`b_wr_probe_*`目录。无gdb、无全局pkill、无节点重置。预估数分钟，节点负载和预检会影响时长。

本地7项检查使用实际URMA头文件和模拟provider：SGE累加、链、inline、原WR/返回/坏WR透传、错误不算成功、RM/UM过滤、元数据/长链/身份/越界拒绝。未在ARM/HMPI实际运行，Windows脚本未在本环境执行。库无法解析是工具失败，不应认定产品故障。

## 2026-10-29 观察器符号解析修正

真实节点旧包在首个8B任务报WR_PROBE_RESOLVE_ERROR，两端退出127，无逐WR证据；不是数据校验失败。原PPN4正常矩阵证据不撤销。旧RTLD_NEXT无法在本地RTLD_LOCAL模拟中找到提供者，已复现同样127；不能仅据此断言节点具体加载命名空间。

修正版在RTLD_NEXT失败后枚举进程已加载的liburma.so或liburma.so.*，只以RTLD_NOLOAD取得句柄，解析实际函数并保留句柄；不加载另一份URMA、不修改产品。摘要新增loaded_handle_used表示是否采用回退。节点启动MPI前双端先编译运行局部加载的模拟provider，失败停止；此门禁不访问设备、不能代替实际URMA发送验收。新增symbol_smoke.py与local_client.c。

本地10项通过（含局部加载成功、错误透传、无provider报127），既有OSU23项通过。已用旧源码在同一局部加载场景实际复现127。ARM节点与PowerShell未执行；修正版仍待节点验证，未宣称一次通过。
