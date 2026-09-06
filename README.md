# Arknights Reverse Toolchain

这是一个面向**获授权客户端副本**的只读资源与静态分析工具链。它把
Unity Bundle/TypeTree、Spine、Texture/Sprite/TextAsset/AudioClip、LZ4AK、
IL2CPP 结构、PC/Android 逻辑关联和有限的特效时间线导出组织成可复核的
脚本，而不是发布游戏资源或运行时注入器。

## 能做什么

- 从用户提供的 Bundle 建立 Unity 对象清单、PPtr/外部 CAB 依赖和哈希。
- 以一个精确干员 ID 生成分类闭包：战斗骨骼、皮肤、立绘、头像、语音、
  模块、配置、投射物和依赖，并对缺失引用/TypeTree 失败/零字节导出降级。
- 解析 Arknights LZ4AK Bundle，保留原始对象与 TypeTree 证据。
- 对标准 IL2CPP 元数据和当前 PC `GameAssembly.dll` 做结构/调用关系索引；
  历史地址只作参考，不会自动导入当前版本。
- 在拥有 Unity、shader 和依赖资源时，对指定特效做离线单帧/时间线实验。

## 明确边界

本仓库不包含任何游戏客户端、私有聊天/群文件、真实角色资源、运行时
注入、进程内存读取、反作弊规避或“完整源码”承诺。输出只表示静态证据；
方法体、运行时地址、真实游戏内调用顺序和视觉等价性仍需用户自行验证。

## 快速开始

需要 Windows PowerShell 7、Python 3.11+、.NET 9 SDK（构建 AnimeStudio
适配器）以及用户自己安装的可选外部工具。完整恢复步骤见
[`docs/RESTORE_PUBLIC.md`](docs/RESTORE_PUBLIC.md)，命令索引见
[`docs/USAGE_PUBLIC.md`](docs/USAGE_PUBLIC.md)。

```powershell
git clone https://github.com/<account>/arknights-reverse-toolchain.git
Set-Location arknights-reverse-toolchain
powershell -ExecutionPolicy Bypass -File scripts/restore_public.ps1
powershell -ExecutionPolicy Bypass -File scripts/toolchain.ps1 health
```

所有输入放在被 `.gitignore` 忽略的 `inputs/` 下，结果写入 `outputs/`。
先编辑 `targets/arknights/profile.json` 或 `targets/arknights_pc/profile.json`，
再运行对应的只读路由。

## 开发与验证

```powershell
python scripts/public_audit.py .
python -m compileall targets scripts
python scripts/fixtures/generate_fixtures.py --output fixtures/generated
powershell -ExecutionPolicy Bypass -File scripts/verify_toolchain.ps1
```

提交前请运行 `scripts/public_audit.py`；它会阻止私有路径、聊天记录标记、
凭据模式、过大的构建产物和不应进入仓库的二进制文件。

## 许可证

本项目代码使用 MIT；第三方组件和用户提供的客户端资源遵循各自许可证。
参见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
