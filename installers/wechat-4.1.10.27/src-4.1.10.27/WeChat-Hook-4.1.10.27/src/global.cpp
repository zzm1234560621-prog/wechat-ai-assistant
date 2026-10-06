#include "global.h"

HMODULE g_hModule = NULL;
uint64_t g_MyModuleBase = 0;
uint64_t g_MyModuleSize = 0;
uint64_t g_MyModuleEnd = 0;
DWORD   进程PID = 0;
DWORD   父进程PID = 0;
DWORD   g_MainThreadId = 0;
HANDLE g_hLoginMonitor = nullptr;
HANDLE g_hAfterLoginInit = nullptr;

HMODULE g_hWeixinDll = nullptr;
HMODULE g_hWeixinExe = nullptr;
HWND    g_WeixinMainHwnd = nullptr;
volatile uint64_t g_IsLogin = 0;
// db 层门禁：只有就绪判据线程「验过句柄真能查」之后才置 1（见 inline_weixin_dll_load.cpp）。
volatile uint64_t g_DbLayerReady = 0;
// 句柄表扫描许可：判据线程起来后就置 1。**与门禁分开**，否则判据自己会被门禁挡住（循环依赖）。
volatile uint64_t g_HandleScanAllowed = 0;
volatile uint64_t g_getprofile = 0;
CRITICAL_SECTION g_dbMgrCriticalSection;

volatile bool g_LoginMonitorRunning = true;
std::string g_CallBack_Url;

std::wstring g_AppDataDir;
std::wstring g_DocumentDir;
std::wstring g_UsersDir;

SelfInfo_t SelfInfo;

