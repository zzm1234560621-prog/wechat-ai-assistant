# 可选组件（语音转文字 / 网上搜索）——为什么这么装、坑在哪

> 状态：**已落地**（2026-10-05）。它落的是 2026-10-03 用户拍板的那条倡议
> 「安装包自带 SearXNG + 一键部署全部可选功能（可关闭）」，本轮范围是**四项**：
> 语音转文字、网上搜索后端、**文件格式增强包**（视频 / 邮件 / 压缩包 / 老 Office /
> PDF 内嵌图）、**本地语义检索**。剩下没进菜单的只有 `Pillow` 之外那些**不属于 pip**
> 的东西（比如 `.rar` 要的外部解压器）——它们只能如实提示，装不了。

## 1. 要解决的问题不是「功能没写」，是「包发出去了用不了」

代码早就在：`audio_read.py`（本地转写）、`voice_mem.py` + `pilk`（语音条从内存取 SILK）、
`web_read.py`（联网搜索）。开发机上也都好用，因为**那台机器上什么都装好了**。

但发布包里不是：

| | 开发机 | 别人拿到包 |
|---|---|---|
| `faster-whisper` / `pilk` | 已装 | **没装**——`requirements.txt` 里它们**只能是注释行**（见下） |
| 语音模型（`data/models/`，实测 tiny 74.6 / base 141 / small 463.7 MB） | 已下 | **没有**——`data\` 是打包明令排除的 |
| SearXNG 源码 | 在项目上一级 | 2026-10-05 之前**包里根本没有** |
| SearXNG 的 `.venv`（约 91MB） | 已建 | **不能拷**（venv 里记的是绝对路径，跨机器必坏） |
| 安装入口 | 命令行 `pip install …` / `audio_read.py --setup` | **没有任何入口**（`console.py` 里连「语音」两个字都没有） |

结果：README 把「语音条转文字」写在功能卖点里，而朋友装完问它语音，只会得到一句
「转写要装 faster-whisper」（诚实，但他没路可走）。

## 2. 为什么可选依赖**必须**留在 requirements.txt 的注释里

`envsetup.required_pkgs()` 是从 `requirements.txt` 的**非注释行**派生的，install.bat 装完
按它校验。把 `faster-whisper` 写成正式行 → 校验要求它 → 没装的人「装完还是起不来」死循环，
installer 还会去拖重包。这条 2026-10-01 踩过，`selftest_audio` 钉着。

所以规矩是：**清单保持注释（唯一真源不变），安装入口另开一处**——
`envsetup.OPTIONAL_PIP`（bot 自己 venv 里的）+ `botctl.search_install()`（SearXNG 自己的 venv）。
`selftest_install.T6` 有一条专门盯「可选依赖不许泄漏成正式行」。

## 3. 两项各自的 owner（**别另开第二份实现**）

| 组件 | 装依赖 | 下模型/建 venv | 菜单 |
|---|---|---|---|
| 语音转文字 | `envsetup.install_optional("voice")`（pip） | `audio_read.py --setup`（模型到 `data/models/`） | 一键部署第 ③ 步 / `可选组件.bat` |
| 网上搜索 | `botctl.search_install()`（在它的目录里 `python -m venv` + pip） | 同上（venv 就是它的「模型」） | 同上，另加 [8]→[9]→[4] |
| 文件格式增强包 | `envsetup.install_optional("formats")`（一条 pip 装七个：av / extract-msg / py7zr / rarfile / xlrd / olefile / Pillow） | 无（都很小，装完立刻能用） | 同上 |
| 本地语义检索 | `envsetup.install_optional("semantic")`（sentence-transformers，拖进 torch） | `semantic.py --setup` 下模型 + `semantic.py --build --days 90` 建索引 | 同上 |

三条与「重 / 轻」有关的规矩：

- **语义检索是唯一「重」项**（`console._HEAVY` 就它一个）：**一键部署里默认不装**
  （回车=跳过，要手打 `y`）。它比别的项多两步，其中**建索引必须先停 bot**（遍历历史是重活，
  挂在轮询线程上会把 hook 拖住）——`_install_semantic` 把「停助手 → 建索引 → 起回来」串成
  一次确认；用户拒了就**只留命令、不硬来**。
- **`.rar` 不能只看 `rarfile` 装没装**：它只是个**壳**，真正解压要外部程序（unrar / 7z / bsdtar）。
  判据只有一份：`archive_read.find_rar_tool()` —— 先看 PATH，再看**厂商默认安装位置**
  （`%ProgramFiles%\7-Zip\7z.exe` 等）。这条当场纠了一个错判：只看 PATH 的 `7z`/`WinRAR` 时
  结论是「本机一个都没有」，实际 **7-Zip 装了、只是不在 PATH**。所以「不在 PATH」≠「没装」。
- **菜单按键不能写死**：开关那一项恒取「项数 + 1」。加到第 4 项时踩过——`[3]` 同时是
  「格式包」和「自动装开关」，按 3 会去切开关、装不了东西。`selftest_install.T6` 会真跑一遍
  菜单数按键（只数**可选的**那几行；状态文案里引用的「用 [3] 装」不算）。

- **判据不是 pip 的退出码**：装完**再查一次 import**（语音）/ **再查一次解释器在不在**
  （搜索）。pip 说成功而实际不可用是真会发生的，所以 `install_optional` / `search_install`
  都可能报失败。
- **`search.home` 的解析只有一份**（`botctl.search_home`）：配置优先 → **能用的那份优先**
  （有 `.venv\Scripts\python.exe`）→ 都没装好就按「项目内 → 上一级」挑。
  `web_read._searxng_hint()` 以前自己抄了一份路径算法，2026-10-05 改成**调它**。
  开发机上两份都在（上一级那份装好了、包里那份只是源码），**必须选装好的那份**，
  否则正在跑的搜索服务会被判成「没装」——`selftest_botctl.T8` 钉着这条。
- 开关写 **`settings.json` 的 `optional`**（`{"voice": true, "search": true}`），不写
  `config.yaml`：程序从不回写带注释的 config.yaml。「关」= 以后不再自动装，**已装的不动**；
  没写过 = 装（用户 10-03 的决定就是「一键部署自动装齐、每项可关」）。

## 4. 随包携带 SearXNG：进包的是源码，不是它的 venv

- 源码进仓库：`searxng\`（1000 个文件 / 约 20.4MB，**不含** `.venv` 与 `sxng_cache_*.db`）；
  `tools/build_package.ps1` 的目录清单加了它，并且复制后**剪掉** `.venv` / 缓存 / `__pycache__`；
  打包自检会**因为**它们存在而失败（和「包里不许有 bot 的 .venv」同一条规矩）。
- 打包自检还钉了 `searxng\searx\data\engine_traits.json` **必须在**——见下一个坑。
- 新机器上第 ③ 步现建 venv（`python -m venv` + `pip install -r searxng\requirements.txt`）。

## 5. 这一轮真踩到的两个坑（都是**静默**的）

0. **发布前验收必须在「解压出来的包」里再跑一遍自测**（这条是本次又验证了一遍的老规矩）。
   仓库里 30/30 全绿，解压出来的包里却红了 1 份：`selftest_botctl` 的 T7 把约定写死成
   「上一级 searxng」——开发机上**碰巧**对（上一级那份存在），包里必然错。已改成
   「落在两个约定位置之一」（精确判据交给 T8 用注入的假 `search_ready` 钉）。
   教训：**只在本机跑自测，会漏掉一整类「只有别人的目录布局才暴露」的断言**。
   另外包里有 **1 个 0 字节文件**是合法的：`searxng\tests\unit\settings\empty_settings.yml`
   —— SearXNG 自己的测试夹具，名字就叫 empty。所以「包里无 0 字节文件」这条只说**我们的**
   文件，别把它当成 searxng 的验收项。

1. **仓库根的 `.gitignore` 里那条 `data/` 把 `searxng/searx/data/` 整个吃掉了。**
   没锚定的 `data/` 匹配**任意层级**，于是那 17 个文件 / 约 15MB 的必需运行期数据
   （`engine_traits.json`、`currencies.json`、语言检测模型 `lid.176.ftz`…）**磁盘上有、
   git 里没有**。打包脚本 copy 的是磁盘，所以当时那个包是对的——但**从 git 克隆出来
   重打包就会少这一块，而且不报错**。修法是把那一条锚定成 `/data/`（改根因，不是给
   单个目录开后门），并让打包自检盯住 `engine_traits.json` 在不在。
   顺带产出：`selftest_portable.py` 把 `searxng` 加进了「第三方源码整棵跳过」的名单，
   否则拿我们的「不许写死本机路径」去查别人 1000 个 .py，只会在用户机器上报假失败。
2. **编辑工具会把 `tools/build_package.ps1` 的 UTF-8 BOM 写掉**（改这个脚本的老坑，
   这次又踩）。没 BOM 时 PowerShell 5.1 按系统代码页读 → 中文全乱码，`selftest_portable`
   的「13 个 .ps1 必须带 BOM」会直接判失败。**改完必须查前 3 字节是不是 `EF BB BF`**，
   掉了就用 `UTF8Encoding($true)` 写回。

## 6. 怎么验（不用真微信、不碰 hook）

```
.venv/Scripts/python.exe selftest_install.py    # T6：注册表 / 不许泄漏成正式行 / 开关
.venv/Scripts/python.exe selftest_botctl.py     # T8：search_home 判据 + 装 venv 不报假成功
.venv/Scripts/python.exe selftest_portable.py   # 打包清单含 searxng；.ps1 BOM；.bat 纯 ASCII
```

真机（只读，不装东西）：

```
.venv/Scripts/python.exe audio_read.py --status         # 语音那一侧行不行
.venv/Scripts/python.exe -c "import botctl,console;print(botctl.search_home());print(console._search_state())"
```
