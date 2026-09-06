# Public usage

## 1. 准备输入

只处理你有权检查的客户端。Android Bundle 放在
`inputs/android/bundles/`；PC 静态文件放在 `inputs/pc/`。不要把输入、导出
资源或报告提交到 Git，因为这些目录已被忽略。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r third_party/unitypy_requirements.lock.txt
```

## 2. 构建 AnimeStudio 适配器

```powershell
dotnet build third_party/AnimeStudio-src/AnimeStudio.CLI/AnimeStudio.CLI.csproj -c Release
```

然后确认 `targets/arknights/profile.json` 的 `assets_adapter.tool_path` 指向
生成的 CLI。该 CLI 的源代码和第三方许可随仓库提供；构建产物不会提交。

## 3. 常用入口

```powershell
# 健康检查与帮助
powershell -File scripts/toolchain.ps1 health
powershell -File scripts/toolchain.ps1 help

# 单 Bundle 导出（路径必须在 profile 的允许根内）
powershell -File scripts/toolchain.ps1 assets `
  -InputBundle inputs/android/bundles/chararts/example.ab `
  -Types Texture2D,Sprite,TextAsset,AnimationClip,AudioClip `
  -Output outputs/assets/example

# LZ4AK 对象探针
powershell -File scripts/toolchain.ps1 assets-lz4ak `
  -InputBundle inputs/android/bundles/example.ab `
  -Output outputs/lz4ak/example

# 干员资源闭包（要求先有 Unity inventory sqlite）
python targets/arknights/operator_resource_closure.py `
  --profile targets/arknights/profile.json `
  --operator-id char_501_durin `
  --output outputs/operators/durin

# PC 资源探针
powershell -File scripts/toolchain.ps1 pc-assets-probe -Profile arknights_pc
```

所有路由都默认只读；输出目录若已存在会拒绝覆盖。将每次命令的输入哈希、
工具版本和报告保存在 `outputs/`，便于复核。

## 4. 结果如何解读

`completed_exact_static_closure` 只代表当前输入在静态对象图上没有未解析
引用、TypeTree 失败或导出失败；它不等于游戏运行时可用。`partial_*`、
`blocked_*` 和 `runtime_unverified` 必须保留，不能被脚本自动晋升为完成。
