# MPI OSC依赖与现成软件原子路径审查

HUCX固定在d2f2dd6b0ca1194be98e780d879868e3de7fe062；公开HMPI参考固定在52d943345a85efc10de2d59311eb4bd4fa94444e。
公开参考尚未与节点所装HMPI源码逐字对齐；新工具核对实际组件、库、CLI及执行路径，不宣称公开源码就是节点源码。

| 层次 | 已读函数 | 实现选择 |
| --- | --- | --- |
| HMPI | ucp_context_init / component_select | 申请RMA和AMO32/64；worker/EP/窗口注册由OSC管理 |
| HMPI | ompi_osc_ucx_fence | worker flush后集合barrier；所选OSU用例原生支持fence/create |
| HMPI | lock_all / flush / unlock_all | 包含状态字和原子依赖；单列功能门禁，不假定fence证明所有模式 |
| HUCX wireup | ucp_wireup_add_amo_lanes / ucp_wireup_add_memaccess_lanes | 无原子设备通道且允许AM模拟时，创建AM lane而非直接要求硬件AMO |
| HUCX rkey | ucp_rkey_resolve_inner | RMA和AMO分开解析；AMO lane缺失时选择sw_amo，RMA可保留basic_rma |
| HUCX AMO | ucp_amo_sw_progress / ucp_atomic_req_handler / ucp_atomic_rep_handler | 请求和回复走AM；对端CPU执行32/64位原子操作并返回结果 |
| HUCX config | ucp_config_read | env_prefix为MPI时构造MPI_UCX_；两种前缀均应明确固定 |

软件原子回退依赖对端worker推进。它解决此处的操作实现与同步接入，不提供新增丢包重传保证。
RC/UM能力不作修改，也不复制其硬件原子入口给RM。基础RMA与UCP已验产品保持不变，先做真实MPI正常路径验证。

## 已读文件SHA256

- HUCX wireup `src/ucp/wireup/select.c`：`def0a4ee7d937fb4c5871cc56fd3da2e7c0d2ba9ce1f480b6e150735326f6478`。
- HUCX rkey `src/ucp/core/ucp_rkey.c`：`a7dad1bc076fc9f2b84cfbc713d93d22cf23e40ca401d02392e7be2dbc1666b4`。
- HUCX software atomics `src/ucp/rma/amo_sw.c`：`185a817eb40a4c4d12006a25e74bb62ed0baec70acc7fe8f1c6a773cf8f38ec4`。
- HUCX config prefix `src/ucp/core/ucp_context.c`：`fff4cb378351e862adc4fce231d6e2fa5f493b962d84252ae43216960d03f44c`。
- HMPI OSC component `osc_ucx_component.c`：`3eaee0901bf24534a8c5d95b36a5b104241cf2914dcfec127e2d1efae631f07e`。
- HMPI active target `osc_ucx_active_target.c`：`8d976605466e061649456e352e589ce6be6a4a52341b2a06b2e24c8f86f71584`。
- HMPI passive target `osc_ucx_passive_target.c`：`5c8cc55bcf67c477a1cb6b4ae9d3cfdd85c03ef0303be17c38f7b7495d93a8b5`。

OSU来源为已留存的原版7.5.2源码。四个单边用例检查options.validate后直接报不支持校验；均接受-s fence和-w create。
新工具保持原版二进制，先用MPI guard专项校验数据，再做原版OSU路径取证及可选独立计时。
