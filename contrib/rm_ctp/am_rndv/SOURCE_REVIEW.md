# AM rendezvous源码审查与实现边界

固定HUCX源码9e1a3a6b5ed4074ac162258397065fd0f54831e0，src与已测80b28a25相同。以下结论来自固定源码；真实RM/CTP结果仍待节点。

## 为什么先走这条路径

RM与RC都向UCP提供AM操作。原生PUT/GET能力与AM软件协议是两层问题：RM当前屏蔽原生RMA/AM_ZCOPY，但UCP仍可通过AM bcopy传输rendezvous数据。
UM有额外软件保序/重传机制，本轮不复制它；AM rendezvous本身也不是丢包重传方案。
继续复用现有HMPI启动和OSU入口，不修改MPI层、不引入第二套地址或大消息协议；不依赖HMPI使用nbx/nbr的猜测，两类阈值同时设置，最终检查实际UCP请求。

## 关键调用链

1. ucp_context.c的ucp_rndv_modes枚举包含am；有效UCX日志级别为REQ，运行环境使用UCX_LOG_LEVEL=req。
2. ucp_tag_send_req计算有效rndv_thresh，并记录请求指针、长度、阈值；ucp_request_send_start返回NO_PROGRESS后进入已有ucp_tag_send_start_rndv。
3. RTS公布请求ID和消息长度。am模式不走is_get_zcopy对应的发送缓冲注册路径；不能据此说整个协议完全没有rkey元数据。
4. 接收方匹配后发RTR；ucp_rndv_rtr_handler在不能采用原生PUT时转到rndv_data协议。仅设置RNDV_SCHEME=am不作为无RMA证明，仍核对真实能力和实际请求路径。
5. AM_ZCOPY关闭且ZCOPY阈值inf，正常host缓冲继续走ucp_rndv_progress_am_bcopy；ucp_rndv_pack_data按max_bcopy减数据头计算每片长度，带接收请求ID及offset。
6. ucp_rndv_data_handler依据接收请求ID和offset交给现有ucp_request_process_recv_data；最终请求及ID释放继续使用UCP逻辑。
7. UCT RM am_bcopy继续借用共享Jetty、mpool描述符、完成队列和pending；产品不改。节点同时检查OSU数据正确、实际库映射和每SEND长度，不能只看post成功。

## 实际协议证据

把host/PID和最新一次请求选择关联，不跨进程或跨请求生命周期推断。16KB以下及显式inf控制应为eager；16KB及以上测试必须见到对应业务长度请求的using rndv_data protocol。
单向bw要求真正发送大消息的rank0；bibw/multi_lat要求全部实际发送rank。不把接收方小ack或其他控制请求当作大消息证据。
集合通信可能改变实际请求长度，本步不把MPI集合每对尺寸直接套成UCP请求长度，后续按实际集合算法单列。

## 本地证据与限制

使用已有CPU构建，在TCP回环运行真实ucx_perftest tag_lat，16383/16384/65536/1048576B四项请求路径取证通过。
CPU TCP显式PUT_ENABLE=n以匹配RM无原生RMA能力；不加该条件时本地TCP会选择PUT，说明不能只相信scheme配置。
这是实际UCP/TCP协议检查，不是原版OSU、URMA或ARM验收，也不报告其数值作为性能收益。
节点计划只注入已有UCX配置，不更改产品默认、不打开原生READ/WRITE。未知的CTP失败/交付/顺序契约仍留C；本步不解答或实现它们。

## 固定文件哈希

| 路径 | SHA256 |
| --- | --- |
| `src/ucp/core/ucp_context.c` | `fff4cb378351e862adc4fce231d6e2fa5f493b962d84252ae43216960d03f44c` |
| `src/ucp/core/ucp_ep.c` | `b54128e9600a12bc42b138f2da372f0665f675c9ddcae09e99f22b0e512cec17` |
| `src/ucp/core/ucp_request.h` | `9d15d01702d3918897c836e8cda67b0c84310efb5430c89039d901986cfd7d5f` |
| `src/ucp/tag/tag_send.c` | `4206cb25c454140990f138bca8b6526b2815cd427efef58c28528240adfeff12` |
| `src/ucp/rndv/rndv.c` | `d5bf9e85a366a490ad32fd83bab5ae3529cb2ea064f8023a76b9941230879ab0` |
| `src/uct/urma/rm_urma/rm_urma_iface.c` | `bb66912890a591c4e75963ce96b0a9a4645a2bae1593fd65440fae5e5cd94f8e` |
| `src/uct/urma/rm_urma/rm_urma_ep.c` | `3e9af37b2cf883bc0d1eb3b7b6b1c684d3f70692967ae7eb5159248523e9deca` |
| `src/uct/urma/rc_urma/rc_urma_ep.c` | `82a96b6668b35608c3bc937d78a60093171aeb1edbec46814a2258f189ae4cbc` |
| `src/uct/urma/um_urma/um_urma_ep.c` | `65bf450e235ca0abe6129112e7652f79c9fb43975fb664faf35b9af774d4e518` |


## 日志隔离源码依据（2026-10-30）

`src/ucs/config/global_opts.c`的LOG_FILE为现有只读配置，明确支持%h主机和%p进程替换；`src/ucs/debug/log.c`负责打开各进程文件。
只使用已有环境配置，不修改UCX logger。每任务目录独立，避免PID重用串入其他任务；LOG_FILE_SIZE=inf、LOG_FILE_ROTATE=0保留完整日志。
OSU解析器仍为原`window_matrix/matrix.py`严格尺寸行/版本/列数/Pass检查。错误扫描扩大到每个实际应用日志，未通过删除或过滤未知数字残片来放行。
目前node原日志证明退出0和明确65536 Pass行，未单凭该行宣称8rank协议/maps/逐SEND全验收；这些门禁由单项补验继续执行。
