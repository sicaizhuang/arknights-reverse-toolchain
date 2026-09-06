# Restore and update

## 从 GitHub 恢复

```powershell
git clone https://github.com/sicaizhuang/arknights-reverse-toolchain.git
Set-Location arknights-reverse-toolchain
powershell -ExecutionPolicy Bypass -File scripts/restore_public.ps1
```

`restore_public.ps1` 只创建本地 Python 虚拟环境并安装锁定依赖，不下载
游戏或私有证据。外部 .NET、Unity、IL2CPP 工具需按当地许可自行
安装。配置文件中的 `inputs/` 路径是模板，必须由用户改成自己的授权输入。

## 日常更新

```powershell
powershell -ExecutionPolicy Bypass -File scripts/update_public.ps1
```

脚本执行 `git pull --ff-only`、公开边界审计和 Python 语法检查；它不会碰
`inputs/`、`outputs/` 或用户安装的外部工具。需要发布自己修改的版本时，先
人工检查 `git diff`，再执行：

```powershell
git add .
git commit -m "更新工具链"
git push
```

不要在命令行、配置文件或报告中写入令牌、密码或客户端原始资源。
