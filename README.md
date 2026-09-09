# 音乐数据刮削 (Music Meta Scraper)

fnOS 原生应用：为本地音乐库批量补齐元数据的专业工具。支持 flac / mp3 / ogg / ape 等格式，从多个数据源检索并写入封面、歌手、年份、专辑、流派、唱片公司、语言、曲目号与内嵌歌词等标签。

本项目是一个 **fnOS `.fpk` 原生应用**，仓库结构即飞牛 `fnpack` 工程结构，可直接用 `fnpack build` 打包。

## 功能特性

- 按「歌曲名-歌手」自动识别曲目，多数据源检索写入完整元数据
- 支持 AcoustID 音频指纹识别（需自行申请 Key）
- 匹配达到阈值自动写入并按文件哈希永久记忆，后续扫描自动跳过
- 低于阈值的进入人工辅助队列：查看候选、对比试听/时长、勾选字段合并写入
- 插件化数据源：QQ 音乐 / 网易云 / 酷狗 / 酷我 / LRC Lib / TheAudioDB / iTunes
- 全局共享限速与随机间隔防风控；同曲目本地缓存零重复请求
- 真实写入开关（默认仅学习预览）、目录级写保护、中文错误提示

## 目录结构

```
music-meta-web/
├── app/                     # 应用资源
│   ├── server/              # 后端 Python 服务
│   │   ├── musicmeta/       # 核心刮削逻辑（缓存/限速/多源插件）
│   │   └── webapp/          # Web 服务与静态页面
│   └── ui/                  # 桌面入口（图标、配置）
├── cmd/                     # 生命周期脚本（install/upgrade/config/uninstall/main）
├── config/                  # 权限与资源声明（privilege / resource）
├── wizard/                  # 安装向导
├── manifest                 # 应用清单（appname / version / desc 等）
├── ICON.PNG                 # 应用图标 64x64
├── ICON_256.PNG             # 应用图标 256x256
└── LICENSE                  # GPL-3.0
```

## 本地打包

前置：下载 [fnpack](https://developer.fnnas.com/docs/cli/fnpack/) 并放入 PATH（Linux amd64 示例）：

```bash
curl -L -o fnpack https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64
chmod +x fnpack
sudo mv fnpack /usr/local/bin/
```

在工程根目录打包：

```bash
cd music-meta-web
fnpack build .
# 产物: music-meta-web.fpk，按需重命名为 music-meta-web-<version>.fpk
```

## GitHub Actions 自动打包与发布

本仓库含 `.github/workflows/build-release.yml` 工作流：

- 推送形如 `v1.2.1` 的 git tag 时自动触发
- 在线下载官方 `fnpack`，执行 `fnpack build`
- 按 `manifest` 中的版本号把产物命名为 `music-meta-web-<version>.fpk`
- 自动创建 GitHub Release 并上传该 fpk 附件

发布新版本流程：

```bash
# 1. 修改 manifest 中 version（例：1.2.1 -> 1.2.2），提交
git add manifest && git commit -m "v1.2.2"

# 2. 打 tag 并推送（触发 Actions）
git tag v1.2.2
git push origin v1.2.2
```

## 在 fnOS 上安装

1. 从 GitHub Releases 下载 `music-meta-web-<version>.fpk`
2. 打开飞牛 fnOS 应用中心 → 右上角「手动安装」→ 选择该 fpk
3. 完成向导即可使用

> 提示：手动安装的应用升级/重装会保留数据卷数据；改 manifest / 图标后需重装才在应用中心生效。

## 许可

GPL-3.0 License — 见 [LICENSE](LICENSE)。
