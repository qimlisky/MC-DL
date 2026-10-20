# Minecraft 完整下载器（Python）

命令行工具，把任意版本的 Minecraft **连同加载器一起**下载到指定 `.minecraft` 目录，**只负责下载，不启动游戏**。

实现方式：基于成熟的 [`minecraft-launcher-lib`](https://github.com/JakobDev/minecraft-launcher-lib)，
它已经完整实现了 Mojang 的下载协议和各家加载器的安装流程，这里只做中文 CLI 封装，
所以不会出现"文件下了一半、Forge processors 没跑"这种半成品。

默认还会把下载地址改写成 **BMCLAPI 镜像**。
> 下载时自行搜索Minecraft版本是否支持加载器
  *免责声明*
  本工具为纯技术下载工具。
  用户需自行确保其下载和使用行为，符合所在国家/地区的法律法规以及游戏发行商的区域政策。
  请确保您已拥有该游戏的正版授权。
  
## 支持范围

| 加载器 | 说明 |
| --- | --- |
| `vanilla` | 原版：版本 Json、client jar、libraries、natives、assets、logging |
| `forge` | 自动挑推荐版本，下载 installer 并执行其中的 processors |
| `neoforge` | 按 MC 版本推断 NeoForge 版本号并安装 |
| `fabric` | 官方 meta 安装流程 |
| `quilt` | 官方 meta 安装流程 |
| `optifine` | 走 OptiFine 官方安装器（需显式指定版本号） |

Forge / NeoForge / OptiFine 的安装器本身需要 Java，请先装好 JDK（8/17/21 按 MC 版本选）。

## 安装与运行

```bash
pip install -r requirements.txt

# 交互向导（不带参数直接运行，推荐）
python mc_download.py

# 看看有哪些版本
python mc_download.py --list
python mc_download.py --list 1.20

# 原版
python mc_download.py --version 1.20.4

# Forge（自动选推荐版）
python mc_download.py --version 1.20.1 --loader forge

# Forge（指定版本）
python mc_download.py --version 1.20.1 --loader forge --loader-version 47.2.0

# Fabric / Quilt
python mc_download.py --version 1.20.4 --loader fabric
python mc_download.py --version 1.20.4 --loader quilt

# NeoForge
python mc_download.py --version 1.20.4 --loader neoforge

# OptiFine（版本号必须写）
python mc_download.py --version 1.20.1 --loader optifine --loader-version 1.20.1_HD_U_I6

# 指定 .minecraft 目录
python mc_download.py --version 1.20.4 --dir "D:\MC\.minecraft"
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--version / -v` | Minecraft 版本，如 `1.20.4` |
| `--loader / -l` | `vanilla`（默认）/ `forge` / `neoforge` / `fabric` / `quilt` / `optifine` |
| `--loader-version` | 加载器版本；Forge 不填用推荐版，Fabric/Quilt 不填用最新版 |
| `--dir / -d` | `.minecraft` 目录；默认 **当前目录下的 `.minecraft`**，支持相对路径和 `~` |
| `--list [关键字]` | 列出版本清单 |
| `--source` | 下载源列表，逗号分隔、靠前优先，如 `bmclapi,official`；默认 `auto` |
| `--mirror` | 追加自定义镜像源，`NAME=URL`，可重复 |
| `-i / --interactive` | 强制进入交互向导 |
| `--no-assets` | 跳过资源文件（assets） |
| `--threads` | 并行下载线程数，默认 48 |
| `--engine` | `auto`（默认）/ `fast`（原版/Fabric/Quilt 强制多线程）/ `legacy`（全部走库） |
| `--java` | 指定 `java.exe`，Forge / NeoForge 安装时用 |
| `--accept-disclaimer` | 非交互环境（CI）下确认免责声明 |

## 交互向导

> 默认目录是**当前工作目录下的 `.minecraft`**：在 `D:\MC` 里运行就装到 `D:\MC\.minecraft`，
> 不会动 `%APPDATA%`。想放别处用 `--dir`，支持相对路径、`~` 和带引号的路径。

不带 `--version` 直接运行（或加 `-i`）会进入分步菜单：

```text
【1/6】选择下载源（可多选，逗号分隔；靠前的优先，失败自动换下一个）
  1. official   官方（Mojang）
  2. bmclapi    https://bmclapi2.bangbang93.com
  直接回车 = 镜像优先（bmclapi → official）

【2/6】选择版本类型
  1. 正式版（Release）
  2. 快照（Snapshot）
  3. 远古版本（Alpha / Beta）
  4. 手动输入版本号

【3/6】选择具体版本（最新在前，输入序号或版本号）

【4/6】选择加载器
  1. 原版（Vanilla） 2. Forge 3. NeoForge 4. Fabric 5. Quilt 6. OptiFine

【5/6】选择下载内容
  1. 完整安装（游戏 + 支持库 + 资源文件）
  2. 游戏 + 支持库（跳过资源文件 assets）

【6/6】目标 .minecraft 目录 [默认路径]

即将开始：
  下载源：bmclapi → official
  版本：1.20.4   加载器：forge 47.2.0
  资源文件：下载
  目录：D:\MC\.minecraft
确认下载？(Y/n)：
```

下载失败时还会问「换个下载源重试？(y/N)」，会按 `--source` 的顺序轮换。

## 多线程下载

原版、Fabric、Quilt 走自带的线程池下载器（`--threads`，默认 48），不再由库逐个串行下载：

- 按版本 Json 生成完整文件清单：主 Jar、`libraries[]`（含 natives）、`assetIndex`、全部 assets objects；
- **下载前先校验**：逐个检查已有文件（大小 + SHA1，空文件视为无效），通过就跳过，不通过才重新下载；
  启动时会打印「X 个已存在且校验通过（跳过），Y 个需要下载」；
- 线程池并发下载，下完的文件同样要过一遍 SHA1 + 大小校验，通过后才从 `.tmp` 改名为正式文件；
- **每个线程一行实时进度条**：当前文件名 + 本文件进度 + 已下载/总大小，最后一行是总文件进度；
  （终端放不下时自动只显示前几行，其余文件仍然在后台下）；
- **Ctrl+C 立即退出**：按下后通知所有线程停止、丢弃临时文件并直接结束进程，不会卡在「等待连接超时」；
- 单个文件失败会按 `--source` 顺序尝试其它源，最终仍失败的会列出路径并报错；
- Forge / NeoForge / OptiFine 仍走 `minecraft-launcher-lib`（要执行 processors / 安装器），这部分是串行。

终端里的样子（`--threads 4`）：

```text
[############------]  66.7%      12.4MB/18.6MB assets/objects/3f/3f4a...
[######------------]  33.3%       4.1MB/12.3MB libraries/org/lwjgl/lwjgl/...
[##################] 100.0%       2.0MB/2.0MB  versions/1.20.4/1.20.4.jar
[##----------------]  11.1%       0.2MB/1.8MB  assets/objects/9a/9a12...
总进度 128/3021 个文件
```

输出被重定向（不是终端）时，会自动退回成 `下载中 128/3021` 的单行文本进度。

```bash
python mc_download.py --version 1.20.4 --threads 16
python mc_download.py --version 1.20.4 --engine legacy   # 回到库的串行实现
```

线程数默认 48；网络差或镜像限流时可以降到 8~16，反之可以继续调高（对大量小文件收益明显）。

## 多个下载源（BMCLAPI，规则照搬 PCL2）

`--source` 接受逗号分隔的源列表，按优先级依次尝试，前一个源 404 / 超时就换下一个：

```bash
# 默认：镜像优先，失败回退官方
python mc_download.py --version 1.20.4

# 官方优先，失败再走镜像
python mc_download.py --version 1.20.4 --source official,bmclapi

# 只用 BMCLAPI（镜像缺文件就报错）
python mc_download.py --version 1.20.4 --source bmclapi

# 自建/第三方镜像（BMCLAPI 路径约定），可叠加多个
python mc_download.py --version 1.20.4 --mirror my=https://mc.example.com --source my,bmclapi,official
```

镜像规则（`mirror_url`）：

| 官方地址 | 镜像 |
| --- | --- |
| `piston-data / piston-meta / launcher / launchermeta.mojang.com` | 镜像根（路径照搬） |
| `resources.download.minecraft.net` | `<镜像>/assets` |
| `libraries.minecraft.net` | `<镜像>/maven` |
| `maven.minecraftforge.net` / `maven.fabricmc.net` | `<镜像>/maven` |
| `maven.neoforged.net/releases` | `<镜像>/maven` |
| `files.minecraftforge.net/.../promotions_slim.json` | `<镜像>/forge/promotions_slim.json` |
| `meta.fabricmc.net` | `<镜像>/fabric-meta` |

实现方式：给 `requests` 的 `Session.request` 打一层包装，只改 GET 的 URL，按源列表逐个尝试；
镜像不覆盖的域名（例如 Quilt meta）会直连。如果库没有用 `requests`，会退回官方源并在启动时提示。

## 下载完成后目录长什么样

```text
<.minecraft>\
├── versions\
│   ├── 1.20.1\                     原版版本
│   │   ├── 1.20.1.json
│   │   └── 1.20.1.jar
│   └── 1.20.1-forge-47.2.0\        Forge 版本（继承 1.20.1）
│       └── 1.20.1-forge-47.2.0.json
├── libraries\                      全部支持库（含 natives）
├── assets\
│   ├── indexes\<索引>.json
│   └── objects\<hash前2位>\<hash>
└── logging\                        日志配置（新版 MC 需要）
```


## 说明与常见问题


- **加载器版本支持**：Fabric 从 Minecraft 1.14 才开始支持，1.13 及更早没有 Fabric。
  选到不兼容的组合时，工具会先提示，并可直接改选原版；例如 **1.11.1 只能用原版或 Forge**，
  装 Fabric 会得到 `Version 1.11.1 is not supported`（现在会在开始下载前就拦下来）。
- **Java 版本**：原版 1.17+ 需要 Java 17，1.20.5+ 需要 Java 21，老版本用 Java 8。
  Forge / NeoForge 的安装器需要本机已有 Java，工具本身不会安装 JDK。
- **不启动游戏**：本工具没有任何启动游戏的代码路径（不调用 `command`、不启动 Java 进程运行游戏），
  下载完就退出，启动请用你自己的启动器。
- **版本号写法**：NeoForge 的版本号是按 MC 版本推出的（1.20.1 → `47.x`，1.20.2+ → `20.2.x` 这种）；
  如果推断不出来，会提示你用 `--loader-version` 明确指定。
- **库版本差异**：`minecraft-launcher-lib` 各版本函数名略有不同，脚本里已经做了探测
  （`submodule()` / `function()`），遇到不支持的功能会给出明确提示，而不是直接崩。
