# Architecture

```text
inputs/ (用户授权输入)
        |
        +--> Unity/LZ4AK adapters ----> object inventory + TypeTree
        |                                  |
        |                                  +--> operator_resource_closure
        |
        +--> PC/IL2CPP static adapters --> symbols, signatures, call edges
        |
        +--> optional Unity effect adapter --> offline frame/timeline evidence

outputs/ (哈希、报告、导出；默认不进 Git)
```

`targets/arknights` 和 `targets/arknights_pc` 是两个隔离的目标层；
`scripts/toolchain.ps1` 只是路由器，不共享客户端路径。资源闭包使用对象
类型、序列化 PPtr、外部 CAB 和配置边界进行归属；名称相似但无边的共享对象
保持候选状态。代码层只记录当前输入版本的结构和调用关系，不把旧 RVA 当作
当前地址。
