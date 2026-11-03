# 配置预检日志路径修正

用户已确认GitHub接入c9991ae7ff3023f4e02e52fc383050b152574f82，节点ZIP哈希一致。
本轮失败目录为固定97b CASE下send_validation_7xu3twa4；错误发生于模式配置预检，
远端ucx_info在ucs_log_init初始化退出，双节点集合、注入、退出和性能任务均未开始。
旧失败结果完整保留，不改产品、不以环境重置处理此问题。

代码中的确定错误：profile_environment给配置查询传入应用UCX_LOG_FILE，
config_rc目录仅在node89创建；远端结果根也在全部模式配置查询后才创建。
ucx_info加载libucs即初始化日志，打不开该路径时会走ucs_open_output_stream的FATAL路径。
该缺陷与用户返回的调用栈一致；原脚本只保留stderr末400字符，丢失了原始错误首行，
所以不补造节点未返回的完整errno或断言环境已恢复。

修正仅在send_validation工具内：

- 配置查询用独立环境，UCX_LOG_FILE为空，使用UCX默认控制台日志；不依赖远端应用目录。
- 只保留当前candidate的四个core库preload，去除WR、故障、资源和身份探针，
  清除注入/观察器变量；传输选择、片长、额度、RTO、迭代等业务参数不调整。
- 不修改调用者的应用环境；MPI任务继续按原路径保存各实际进程日志。
- 双端配置/能力查询保存完整stdout/stderr、命令、退出码与查询环境；失败报告指向原始文件，
  不再只取回溯尾部。node89证据先落盘，远端SSH失败也不会丢掉它。
- 原40项检查点、22集合＋1OSU注入＋6UCT退出、126性能/资源MPI的范围与首错停止保持不变。

CPU验证：新补5项回归，累计21项send_validation检查通过，既有send_integration25项通过。
真实gcc编译的子进程测试在constructor中打开日志：旧环境的缺失父目录使其在main前退出，
修正后RC/iface_v2/reliable_v3均可在目录不存在时完成配置/能力查询；应用环境不被改变。
另验证只保留candidate core preload，完整5000字符错误首尾均保存。
这只是CPU模拟日志初始化依赖，不是实际ARM/libucs/URMA通信或性能验收。

无需传输库重编、旧源码覆盖或设备配置调整。新ZIP在原CASE内创建新的工具/结果目录，
用户手动接入GitHub并上传后重新执行。节点复验仍待；不据本修正勾选完整C/D。
