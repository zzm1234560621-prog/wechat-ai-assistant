#include <cstdint>
#include <cstdio>
#include <string>
#include <Windows.h>
#include <winternl.h>
#include <cstring>
#include <sstream>

#include "http_server.h"
#include "Hook_Method.h"

#include "global.h"
#include <MinHook.h>
#include "tools.h"
#include "HookManager.h"
#include "json.hpp"
#include "wx_ini_reader.h"
#include "inline_weixin_dll_load.h"
#include "hook_xlog.h"

using json = nlohmann::json;

typedef NTSTATUS(NTAPI* PFN_NtQueryInformationProcess)(
    HANDLE,
    PROCESSINFOCLASS,
    PVOID,
    ULONG,
    PULONG
    );



DWORD GetParentProcessId()
{
    PFN_NtQueryInformationProcess NtQueryInformationProcess =
        (PFN_NtQueryInformationProcess)GetProcAddress(
            GetModuleHandleW(L"ntdll.dll"),
            "NtQueryInformationProcess"
        );

    if (!NtQueryInformationProcess)
        return 0;

    PROCESS_BASIC_INFORMATION pbi = { 0 };

    NTSTATUS status = NtQueryInformationProcess(
        GetCurrentProcess(),
        ProcessBasicInformation,
        &pbi,
        sizeof(pbi),
        nullptr
    );

    if (status != 0)
        return 0;

    return (DWORD)(ULONG_PTR)pbi.Reserved3;
}


DWORD WINAPI AfterLoginInitThread(LPVOID)
{
    // 等待登录成功
    while (g_IsLogin != 1)
    {
        Sleep(300);
    }

    return 0;
}

// 过低版本
void Patch_Low_Version()
{
    // XWECHAT_MAIN_CLAZZ_OFFSET 4.1.8.67
    DWORD_PTR baseAddress = (DWORD_PTR)g_hWeixinDll + reinterpret_cast<uintptr_t>(XWECHAT_MAIN_CLAZZ_OFFSET);

    DWORD_PTR* pPointer = (DWORD_PTR*)baseAddress;
    if (*pPointer == NULL) {
        // 处理空指针情况
        return;
    }

    DWORD_PTR targetAddress = (*pPointer) + 0xB8 + 0x90;        //80  = 00000000F2541843



    // 直接通过指针修改
    BYTE* pTarget = (BYTE*)targetAddress;

    // 修改内存保护属性
    DWORD oldProtect;
    VirtualProtect(pTarget, 4, PAGE_EXECUTE_READWRITE, &oldProtect);

    *(pTarget) = 0x43;      // 低位字节 = 67
    *(pTarget + 1) = 0x18;
    *(pTarget + 2) = 0x54;  // 高位字节
    *(pTarget + 3) = 0xF2;  

    // 恢复保护属性
    VirtualProtect(pTarget, 4, oldProtect, &oldProtect);
}


void Patch_Low_Version_m2()
{
    // 4.1.8.67 addresses
    struct PatchInfo {
        DWORD_PTR addr;
        BYTE bytes[4];
    };

    // F2510201
    // F2541843
    PatchInfo patches[] = {
        { (DWORD_PTR)g_hWeixinExe + 0x36E2, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x18EB, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x1B0E, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x204B, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0xDE4C33, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x2AC9481, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x3248CE9, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x379D66E, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinExe + 0x204FA0, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0xDE540C, { 0x43, 0x19, 0x6C, 0xF2 } }, 
        { (DWORD_PTR)g_hWeixinDll + 0xA3518D0, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0xA3518D4, { 0x43, 0x19, 0x6C, 0xF2 } }
    };

    DWORD oldProtect;
    for (int i = 0; i < 3; i++) {
        VirtualProtect((LPVOID)patches[i].addr, 4, PAGE_EXECUTE_READWRITE, &oldProtect);
        memcpy((void*)patches[i].addr, patches[i].bytes, 4);
        VirtualProtect((LPVOID)patches[i].addr, 4, oldProtect, &oldProtect);
    }

}


//启用防撤回
void Patch_Revoke()
{
    // 计算目标地址
    DWORD_PTR targetAddress = (DWORD_PTR)g_hWeixinDll + g_Patch_Revoke;

    // 直接通过指针修改
    BYTE* pTarget = (BYTE*)targetAddress;

    // 修改内存保护属性
    DWORD oldProtect;
    VirtualProtect(pTarget, 2, PAGE_EXECUTE_READWRITE, &oldProtect);

    *pTarget = 0x90;           // 第一个字节改为 nop
    *(pTarget + 1) = 0xE9;     // 第二个字节改为 jmp 操作码

    // 恢复保护属性
    VirtualProtect(pTarget, 2, oldProtect, &oldProtect);
}


// ===== 本地补丁：等「登录就绪」再放行 =====
// 原版靠一个「登录检测 hook」把 g_IsLogin 置 1，那段代码在开源快照里被移除了
// （README: main 分支已移除 ...Hook 安装与处理代码），所以 g_IsLogin 恒为 0，
// db_mgr.cpp 里 if (!g_IsLogin) 的门禁永远成立，QueryDB 永远返回空。
//
// 但不能在 Weixin.dll 一加载就无条件置 1：那时账号还没登录、db_storage 里的
// 库句柄还没建立，QueryDB 会拿垃圾句柄去调 sqlite，把微信搞崩——实测三次崩溃：
//   0xC0000005 读 0x10000              @ Weixin.dll+0x12BB489
//   0xC0000005 读 0xFFFFFFFFFFFFFFFF   @ Weixin.dll+0x12BB50C
//   0xC0000374 堆损坏
//
// 改成等一个可信的就绪信号：微信登录后会往
//   %USERPROFILE%\Documents\xwechat_files\<wxid>\db_storage\*\*.db
// 写数据。发现有 .db 的修改时间晚于「本 DLL 加载时刻」，就说明这次进程已经
// 登录完成、DB 层在活动。等不到就一直是 0，查询返回空，但**不会崩**。

static ULARGE_INTEGER g_loadTime{};
static volatile LONG  g_readyThreadStarted = 0;

// 注意：FindFirstFileW **不支持路径中间的 `*`**（会直接返回 ERROR_INVALID_NAME=123），
// 所以只能一层层往下枚举，通配符永远放在最后一段。
static bool IsDotDir(const wchar_t* name)
{
    return name[0] == L'.' &&
           (name[1] == 0 || (name[1] == L'.' && name[2] == 0));
}

static bool DirHasFreshDb(const std::wstring& dir)
{
    WIN32_FIND_DATAW fd = {};
    HANDLE h = FindFirstFileW((dir + L"\\*.db").c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE)
        return false;

    bool ok = false;
    do {
        ULARGE_INTEGER w{};
        w.LowPart  = fd.ftLastWriteTime.dwLowDateTime;
        w.HighPart = fd.ftLastWriteTime.dwHighDateTime;
        // 留 5 秒时钟误差
        if (w.QuadPart != 0 && w.QuadPart + 5ULL * 10000000ULL >= g_loadTime.QuadPart) {
            ok = true;
            break;
        }
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return ok;
}

static bool WxDbWrittenSinceLoad()
{
    wchar_t profile[MAX_PATH] = {};
    if (!GetEnvironmentVariableW(L"USERPROFILE", profile, MAX_PATH))
        return false;

    const std::wstring root = std::wstring(profile) + L"\\Documents\\xwechat_files";

    WIN32_FIND_DATAW fd = {};
    HANDLE h = FindFirstFileW((root + L"\\*").c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE)
        return false;

    bool ok = false;
    do {
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || IsDotDir(fd.cFileName))
            continue;

        const std::wstring storage = root + L"\\" + fd.cFileName + L"\\db_storage";

        WIN32_FIND_DATAW sd = {};
        HANDLE sh = FindFirstFileW((storage + L"\\*").c_str(), &sd);
        if (sh == INVALID_HANDLE_VALUE)
            continue;
        do {
            if (!(sd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || IsDotDir(sd.cFileName))
                continue;
            if (DirHasFreshDb(storage + L"\\" + sd.cFileName)) {
                ok = true;
                break;
            }
        } while (FindNextFileW(sh, &sd));
        FindClose(sh);
        if (ok)
            break;
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return ok;
}

static DWORD WINAPI LoginReadyThread(LPVOID)
{
    for (int i = 0; i < 3600; i++) {          // 最多等 30 分钟
        if (WxDbWrittenSinceLoad()) {
            Sleep(3000);                      // 登录瞬间句柄和索引还在建，缓一缓
            if (WxDbWrittenSinceLoad()) {
                g_IsLogin = 1;                // 放行 db_mgr 门禁
                return 0;
            }
        }
        Sleep(500);
    }
    return 0;
}

void Evt_WeixinLoad()
{
    g_hWeixinDll = GetModuleHandleW(L"Weixin.dll");
    if (!g_hWeixinDll)
    {
        return;
    }

    if (InterlockedCompareExchange(&g_readyThreadStarted, 1, 0) == 0) {
        FILETIME ft{};
        GetSystemTimeAsFileTime(&ft);
        g_loadTime.LowPart  = ft.dwLowDateTime;
        g_loadTime.HighPart = ft.dwHighDateTime;
        CreateThread(nullptr, 0, LoginReadyThread, nullptr, 0, nullptr);
    }

#ifdef _DEBUG
    char debugMsg[256];
    snprintf(debugMsg, sizeof(debugMsg),"[Evt_WeixinLoad] Weixin.dll: 0x%p\n",g_hWeixinDll);
    OutputDebugStringA(debugMsg);
#endif


    进程PID = GetCurrentProcessId();
    父进程PID = GetParentProcessId();

    //过低版本 4.1.8.67
    //Patch_Low_Version_m2();

    //get base DirPath
    //InitStandardPaths();
    
    Patch_Revoke();


    // 创建并启动HTTP服务器
    if (!g_httpServer)
    {
        g_httpServer = new HttpServer();
        g_httpServer->Start("0.0.0.0", g_StartPort);
    }
#ifdef _DEBUG
    //xLog 日志
    //Hook_Call(WeixinDll_Offset(0xF22C1), 5, hook::MyCallHandler_xLog);
#endif
    

    
     

    
    //取回调URL
    GetWxRecvUrl();         
}

