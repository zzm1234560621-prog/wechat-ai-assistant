@echo off
REM ============================================================================
REM 启动本机搜索后端（SearXNG）—— 微信助手 web_search 工具连的就是它。
REM
REM   * 只绑 127.0.0.1:8888（见同目录 settings.yml），外面访问不到；
REM   * 只开 html + json（json 是 bot 用的，html 是给人排错看的）；
REM   * 这个窗口**要一直开着**：关掉 = 网上搜索停用。
REM     关掉之后再让助手搜，它会如实回「连不上搜索服务」并告诉你来跑这个脚本 ——
REM     不会把「服务没起来」说成「网上没有这条信息」。
REM
REM 想确认它在不在：浏览器打开 http://127.0.0.1:8888 能出搜索页就是好的。
REM ============================================================================
setlocal
set "SEARXNG_SETTINGS_PATH=%~dp0settings.yml"
set "SEARXNG_DISABLE_ETC_SETTINGS=1"
REM Windows 兼容层：SearXNG 的 searx/valkeydb.py 里有 `import pwd`（Unix 专有模块），
REM Windows 上没有它，整个服务起不来。win_shims\pwd.py 补的就是这一个模块
REM （详见那个文件顶部的说明；**不是**改了 SearXNG 源码）。
set "PYTHONPATH=%~dp0win_shims"
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [x] 没找到 .venv —— 依赖还没装。先跑一次安装：
    echo     见 README「网上搜索」一节
    pause
    exit /b 1
)

echo 启动 SearXNG（127.0.0.1:8888）… 这个窗口别关。
REM 必须用 `-m searx.webapp`（**不是** `python searx\webapp.py`）：
REM 后者的 sys.path[0] 是 searx\ 目录，import searx 会 ModuleNotFoundError。
".venv\Scripts\python.exe" -m searx.webapp
echo.
echo SearXNG 已退出（exit=%ERRORLEVEL%）。上面几行通常写着原因。
pause
