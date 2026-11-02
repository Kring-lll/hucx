# 本地检查（2026-11-02）

- 12项Python检查通过：精确轮次/顺序、缺失/重复、guard失败、原版OSU列/单位/版本、非有限数字、日志类型、原生WR边界、实际业务rkey关联、旧MPI_UCX配置清除、MCA保留和远端采集脚本语法。
- mpi_probe.c以真实Open MPI 4.1.6公共头文件，C11、Wall/Wextra/Werror编译并链接通过。
- 现有identity.c及新mpi_path.c以真实MPI头文件编译通过；mpi_path.c使用当前HUCX公共UCP头文件及既有生成版本头。
- 诊断拦截器以独立mock提供者做实际动态链接转发测试：MPI_Put/Get内的UCP NBI调用成功转发、实际key/长度/目标和返回码正确记录。该测试无URMA设备，不是通信或原子语义验证。
- 原始脚本与ZIP的默认计划入口通过；不启动MPI。
- 本地通用MPI运行尝试因隔离依赖缺少libmunge而未能启动，没有计入通过，更不能替代RM节点验收。
- 补丁起点、完整tree、产品src不变、根README不变、manifest、LF、ZIP CRC和应用后检查见外包校验记录。

未执行：Windows PowerShell真实运行、ARM探针编译、HMPI OSC功能、RM软件AMO、双节点MPI读写、原版OSU路径及计时。
本轮无产品修改，既有989节点通过记录保持原范围；不能将工具CPU检查记为MPI或完整B4通过。
